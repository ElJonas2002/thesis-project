import functools
import mujoco
import mujoco.viewer
import numpy as np
from transformers import AutoModelForVision2Seq, AutoProcessor, BitsAndBytesConfig
import torch
from PIL import Image
from pathlib import Path

ROBOT_PATH = str(Path(__file__).parent.parent/"robots/franka_emika_panda/scene.xml")
ARM_JOINT_NAMES = [f"joint{i}" for i in range(1, 8)]
SIM_CAMERA = "wrist_cam"
STEPS_PER_FRAME = 50

def key_callback(keycode):
    """
    Callback function to pause simulation when spacebar is pressed.
    """
    global paused, close
    if keycode == 32:  # Spacebar
        paused = not paused
        print("Paused" if paused else "Resumed")
    elif keycode == 67: # 'C' key
        close = True
        print("Closing viewer")

def euler2mat(euler, seq):
    quat = np.zeros(4)
    mujoco.mju_euler2Quat(quat, np.asarray(euler, dtype=np.float64), seq)
    mat = np.zeros(9)
    mujoco.mju_quat2Mat(mat, quat)
    return mat.reshape(3, 3)

def computeJacobian(model, data, site_id, arm_dof_ids):
    """
    Compute the Jacobian matrix for the robot's end-effector.

    Args:
        model: The MuJoCo model.
        data: The MuJoCo simulation data.
        site_id: The ID of the end-effector site.
        arm_dof_ids: The indices of the degrees of freedom for the robot's arm.
    """
    jacp = np.zeros((3, model.nv))
    jacr = np.zeros((3, model.nv))
    mujoco.mj_jacSite(model, data, jacp, jacr, site_id)
    return np.vstack([jacp, jacr])[:, arm_dof_ids]

def computePoseError(current_pos, target_pos, current_R, target_R, gain=1.0):
    """
    Compute the pose error between the current end-effector pose and the target pose.

    Args:
        current_pos: The current position of the end-effector.
        target_pos: The target position of the end-effector.
        current_R: The current rotation matrix of the end-effector.
        target_R: The target rotation matrix of the end-effector.
        gain: A scalar gain factor for the pose error.
    """
    T_error = target_pos - current_pos
    R_error = target_R @ current_R.T
    angle = np.clip((np.trace(R_error) - 1) / 2, -1.0, 1.0)
    angle_error = np.arccos(angle)
    axis_error = np.array([R_error[2, 1] - R_error[1, 2],
                           R_error[0, 2] - R_error[2, 0],
                           R_error[1, 0] - R_error[0, 1]])
    if angle_error < 1e-6:
        rot_error = np.zeros(3)
    else:
        rot_error = axis_error / (2 * np.sin(angle_error))
    return gain * np.concatenate([T_error, rot_error])

def solveDLSIK(data, joint_lo, joint_hi, arm_qpos_ids, J, pose_error, damping=0.1):
    """
    Solve the Damped Least Squares Inverse Kinematics problem.

    Args:
        J: The Jacobian matrix.
        pose_error: The pose error vector.
        damping: The damping factor for regularization.
        arm_qpos_ids: The indices of the degrees of freedom for the robot's arm.
        joint_lo: The lower limits for the joint positions.
        joint_hi: The upper limits for the joint positions.

    Returns:
        A numpy array representing the change in joint positions (delta_q).
    """
    JT = J.T
    JJt = J @ JT
    delta_q = JT @ np.linalg.inv(JJt + damping**2 * np.eye(JJt.shape[0])) @ pose_error
    q_target = data.qpos[arm_qpos_ids] + delta_q
    return np.clip(q_target, joint_lo, joint_hi)


if __name__ == "__main__":
    paused = False
    close = False
    step = 0
    instruction = "pick up the green cube"
    prompt = f"In: What action should the robot take to {instruction}?\nOut:"

    # Configure the 4-bit quantization settings
    quantization_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.bfloat16,  # Keeps inference stable and fast
        bnb_4bit_quant_type="nf4",              # Normalized Float 4 (recommended for LLMs/VLMs)
        bnb_4bit_use_double_quant=True,         # Compresses the quantization constants for extra VRAM savings
    )

    # Load Processor & VLA (to export model for production runtimes outside Python, switch to "torch.export")
    processor = AutoProcessor.from_pretrained("openvla/openvla-7b", trust_remote_code=True)
    vla = AutoModelForVision2Seq.from_pretrained(
        "openvla/openvla-7b",
        attn_implementation="flash_attention_2",
        quantization_config=quantization_config,
        torch_dtype=torch.bfloat16, 
        trust_remote_code=True,
        device_map="cuda"
    )

    # Load robot model and initialize simulation
    model = mujoco.MjModel.from_xml_path(ROBOT_PATH)
    data = mujoco.MjData(model)
    key_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "task_start")
    renderer = mujoco.Renderer(model, height=226, width=226)
    camera_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_CAMERA, SIM_CAMERA)

    # Kinematics setup for the Jacobian IK controller
    site_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_SITE, "ee_site")
    arm_joint_ids = np.array([mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, j) for j in ARM_JOINT_NAMES])
    arm_qpos_ids = model.jnt_qposadr[arm_joint_ids]
    arm_dof_ids = model.jnt_dofadr[arm_joint_ids]
    joint_lo, joint_hi = model.jnt_range[arm_joint_ids].T
    q_home = model.key_qpos[key_id][arm_qpos_ids].copy()

    ARM_ACTUATOR_IDS = np.array([mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, f"actuator{i}") for i in range(1, 8)])
    GRIPPER_ACTUATOR_ID = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, "actuator8")

    # Reset the simulation to the initial keyframe and perform a forward pass to initialize the state
    mujoco.mj_resetDataKeyframe(model, data, key_id)
    mujoco.mj_forward(model, data)
    
    # Initialize viewer to visualize the robot's state
    with mujoco.viewer.launch_passive(model, data, key_callback=key_callback) as viewer:
        # Change the type to fixed and set the specific target camera ID
        viewer.cam.type = mujoco.mjtCamera.mjCAMERA_FIXED
        viewer.cam.fixedcamid = camera_id
        while viewer.is_running():
            if close:
                break
            elif not paused:
                step += 1

                # Capture an image from the camera
                renderer.update_scene(data, camera=SIM_CAMERA)
                img: Image.Image = Image.fromarray(renderer.render())

                # Predict Action (7-DoF; un-normalize for BridgeData V2)
                inputs = processor(prompt, img).to("cuda:0", dtype=torch.bfloat16)
                action = vla.predict_action(**inputs, unnorm_key="bridge_orig", do_sample=False)

                # Get Deltas from the predicted action
                d_pos = np.asarray(action[:3])
                d_rot = np.asarray(action[3:6])
                gripper_action = float(action[6])

                print(f"step {step}: \n"
                        f"d_pos={np.round(d_pos, 4)}\n"
                        f"d_rot={np.round(d_rot, 4)}\n"
                        f"gripper_action={gripper_action}\n")

                # Convert the current and target poses to the appropriate formats for IK solving
                current_pos = data.site_xpos[site_id].copy()
                target_pos = current_pos + d_pos
                current_rot = data.site_xmat[site_id].reshape(3, 3).copy()
                target_rot = euler2mat(d_rot, seq="XYZ") @ current_rot

                # Compute the Jacobian and pose error, then solve for the joint updates using DLS IK
                jacobian = computeJacobian(model, data, site_id, arm_dof_ids)
                pose_error = computePoseError(current_pos, target_pos, current_rot, target_rot)
                delta_q = solveDLSIK(data, joint_lo, joint_hi, arm_qpos_ids, jacobian, pose_error)

                # Apply the joint updates
                data.ctrl[ARM_ACTUATOR_IDS] = delta_q
                data.ctrl[GRIPPER_ACTUATOR_ID] = np.clip(gripper_action, 0.0, 1.0) * 255.0

                # Step the simulation forward
                for _ in range(STEPS_PER_FRAME):
                    mujoco.mj_step(model, data)
                    # Pick up changes to the physics state, apply perturbations, update options from GUI.
                    viewer.sync()