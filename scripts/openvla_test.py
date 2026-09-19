from transformers import AutoModelForVision2Seq, AutoProcessor, BitsAndBytesConfig
from PIL import Image
import numpy as np
import functools
import mujoco
import torch
import transformers.modeling_utils as _hf_modeling_utils
from accelerate import dispatch_model as _accelerate_dispatch_model
from pathlib import Path

# transformers 4.40.1's from_pretrained calls accelerate.dispatch_model() unconditionally when a
# device_map is set; accelerate then takes a shortcut calling model.to(device) for single-device
# maps, which bitsandbytes 4-bit models reject. force_hooks=True keeps it on the safe hook-based path.
_hf_modeling_utils.dispatch_model = functools.partial(_accelerate_dispatch_model, force_hooks=True)

# ================ PARAMETERS ================
instruction = "pick up the green block"
prompt = f"In: What action should the robot take to {instruction}?\nOut:"

SCENE_PATH = Path(__file__).resolve().parent.parent / "robots/franka_emika_panda/scene.xml"
CAMERA_NAME = "scene_cam"
N_STEPS = 20
SUBSTEPS_PER_ACTION = 50  # sim steps between VLA queries, gives the position PD time to settle

def captureImage(data, renderer, camera_name=CAMERA_NAME) -> Image.Image:
    """
    Capture an image from the specified camera in the MuJoCo scene.

    Args:
        data: The MuJoCo simulation data.
        renderer: The MuJoCo renderer instance.
        camera_name: The name of the camera to capture from.

    Returns:
        An Image.Image object containing the rendered image.
    """
    renderer.update_scene(data, camera=camera_name)
    return Image.fromarray(renderer.render())

def computeJacobian(model, data, site_id, arm_dof_ids) -> np.ndarray:
    """
    Compute the Jacobian matrix for the robot's end-effector.

    Args:
        model: The MuJoCo model.
        data: The MuJoCo simulation data.
        site_id: The ID of the end-effector site.
        arm_dof_ids: The indices of the degrees of freedom for the robot's arm.

    Returns:
        A numpy array representing the Jacobian matrix.
    """
    jacp = np.zeros((3, model.nv))
    jacr = np.zeros((3, model.nv))
    mujoco.mj_jacSite(model, data, jacp, jacr, site_id)
    J = np.vstack([jacp, jacr])[:, arm_dof_ids]
    return J

def computePoseError(data, site_id, T_target, R_current, R_target, gain=1.0) -> np.ndarray:
    """
    Compute the pose error between the current end-effector pose and the target pose.

    Args:
        data: The MuJoCo simulation data.
        site_id: The ID of the end-effector site.
        T_target: The target position of the end-effector.
        R_current: The current rotation matrix of the end-effector.
        R_target: The target rotation matrix of the end-effector.
        gain: A scalar gain factor for the pose error.

    Returns:
        A numpy array representing the pose error.
    """
    T_err = T_target - data.site_xpos[site_id]

    # Rotation error as an "axis-angle" vector (approx. valid for small deltas)
    R_err = R_target @ R_current.T
    rot_err = 0.5 * np.array([
        R_err[2, 1] - R_err[1, 2],
        R_err[0, 2] - R_err[2, 0],
        R_err[1, 0] - R_err[0, 1],
    ])
    return gain * np.concatenate([T_err, rot_err])

def rotvecToMat(rotvec: np.ndarray) -> np.ndarray:
    """Axis-angle vector (world-frame small rotation) -> 3x3 rotation matrix."""
    angle = np.linalg.norm(rotvec)
    if angle < 1e-9:
        return np.eye(3)
    quat = np.zeros(4)
    mujoco.mju_axisAngle2Quat(quat, rotvec / angle, angle)
    mat = np.zeros(9)
    mujoco.mju_quat2Mat(mat, quat)
    return mat.reshape(3, 3)


def solveDLSIK(model, real_data, ik_data, site_id, arm_dof_ids, arm_qpos_ids, joint_lo, joint_hi,
               T_target, R_target, q_home, damping=0.05, k_null=0.1, n_iters=5, gain=1.0) -> np.ndarray:
    """
    Closed-loop damped-least-squares IK with a null-space posture bias.

    Solved on a scratch `ik_data` (seeded from `real_data.qpos`) so the actual simulation
    state is never touched here -- only the returned joint targets feed the position
    actuators, and mj_step's own PD dynamics do the real physical convergence.
    """
    ik_data.qpos[:] = real_data.qpos
    n = len(arm_dof_ids)
    for _ in range(n_iters):
        mujoco.mj_kinematics(model, ik_data)
        mujoco.mj_comPos(model, ik_data)  # populates cdof, which mj_jacSite needs for the Jacobian
        R_current = ik_data.site_xmat[site_id].reshape(3, 3)
        err = computePoseError(ik_data, site_id, T_target, R_current, R_target, gain=gain)

        J = computeJacobian(model, ik_data, site_id, arm_dof_ids)
        JJt = J @ J.T
        J_pinv = J.T @ np.linalg.solve(JJt + (damping ** 2) * np.eye(6), np.eye(6))

        q_arm = ik_data.qpos[arm_qpos_ids]
        null_task = k_null * (q_home - q_arm)
        dq = J_pinv @ err + (np.eye(n) - J_pinv @ J) @ null_task

        ik_data.qpos[arm_qpos_ids] += dq

    return np.clip(ik_data.qpos[arm_qpos_ids], joint_lo, joint_hi)


# Configure the 4-bit quantization settings
quantization_config = BitsAndBytesConfig(
    load_in_4bit=True,
    bnb_4bit_compute_dtype=torch.bfloat16,  # Keeps inference stable and fast
    bnb_4bit_quant_type="nf4",              # Normalized Float 4 (recommended for LLMs/VLMs)
    bnb_4bit_use_double_quant=True,         # Compresses the quantization constants for extra VRAM savings
)

# Load the MuJoCo scene and reset the arm to its "home" keyframe
mj_model = mujoco.MjModel.from_xml_path(str(SCENE_PATH))
mj_data = mujoco.MjData(mj_model)
key_id = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_KEY, "task_start")
mujoco.mj_resetDataKeyframe(mj_model, mj_data, key_id)
mujoco.mj_forward(mj_model, mj_data)
renderer = mujoco.Renderer(mj_model, height=224, width=224)

# --- Kinematic setup for the Jacobian IK controller ---
site_id = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_SITE, "ee_site")
ARM_JOINT_NAMES = [f"joint{i}" for i in range(1, 8)]
arm_joint_ids = np.array([mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_JOINT, j) for j in ARM_JOINT_NAMES])
arm_qpos_ids = mj_model.jnt_qposadr[arm_joint_ids]
arm_dof_ids = mj_model.jnt_dofadr[arm_joint_ids]
joint_lo, joint_hi = mj_model.jnt_range[arm_joint_ids].T
q_home = mj_model.key_qpos[key_id][arm_qpos_ids].copy()

ARM_ACTUATOR_IDS = np.array(
    [mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_ACTUATOR, f"actuator{i}") for i in range(1, 8)]
)
GRIPPER_ACTUATOR_ID = mujoco.mj_name2id(mj_model, mujoco.mjtObj.mjOBJ_ACTUATOR, "actuator8")
ik_data = mujoco.MjData(mj_model)  # scratch state for the CLIK solve, kept separate from mj_data

# Load Processor & VLA
processor = AutoProcessor.from_pretrained("openvla/openvla-7b", trust_remote_code=True)
vla = AutoModelForVision2Seq.from_pretrained(
    "openvla/openvla-7b",
    quantization_config=quantization_config,
    torch_dtype=torch.bfloat16, 
    low_cpu_mem_usage=True, 
    trust_remote_code=True,
    device_map={"": 0},
)

for step in range(N_STEPS):
    image: Image.Image = captureImage(mj_data, renderer)

    # Predict Action (7-DoF; un-normalize for BridgeData V2)
    inputs = processor(prompt, image).to("cuda:0", dtype=torch.bfloat16)
    action = vla.predict_action(**inputs, unnorm_key="bridge_orig", do_sample=False)
    print(f"step {step}: predicted action={np.round(action, 4)}")

    d_pos = np.asarray(action[:3])
    d_rot = np.asarray(action[3:6])
    gripper_action = float(action[6])

    # Guards against rare large-rotation outliers (e.g. OOD images) dominating the DLS solve,
    # since position (meters) and rotation (radians) errors are otherwise weighted 1:1 below.
    ROT_DELTA_CAP = 0.1  # rad, above the typical per-step magnitude seen in normal rollouts
    rot_norm = np.linalg.norm(d_rot)
    if rot_norm > ROT_DELTA_CAP:
        d_rot = d_rot * (ROT_DELTA_CAP / rot_norm)

    R_current = mj_data.site_xmat[site_id].reshape(3, 3).copy()
    T_target = mj_data.site_xpos[site_id] + d_pos
    R_target = rotvecToMat(d_rot) @ R_current  # delta applied in the world/base frame

    err0 = computePoseError(mj_data, site_id, T_target, R_current, R_target)
    print(f"  |pos_err|={np.linalg.norm(err0[:3]):.4f} |rot_err|={np.linalg.norm(err0[3:]):.4f}")

    q_target = solveDLSIK(mj_model, mj_data, ik_data, site_id, arm_dof_ids, arm_qpos_ids,
                           joint_lo, joint_hi, T_target, R_target, q_home)

    limit_margin = np.minimum(q_target - joint_lo, joint_hi - q_target)
    print(f"  ee_pos={np.round(mj_data.site_xpos[site_id], 3)} T_target={np.round(T_target, 3)} "
          f"min_limit_margin={limit_margin.min():.4f} (joint{np.argmin(limit_margin) + 1})")

    mj_data.ctrl[ARM_ACTUATOR_IDS] = q_target
    mj_data.ctrl[GRIPPER_ACTUATOR_ID] = np.clip(gripper_action, 0.0, 1.0) * 255.0

    for _ in range(SUBSTEPS_PER_ACTION):
        mujoco.mj_step(mj_model, mj_data)
