"""U2Net + RANSAC background removal module.

Main execution commands
    - Run: ros2 run intel_realsense u2net_bgrm ros-args -p model <model_name> -p version <version_number>
"""
import os
import time

import cv2 as cv
import message_filters
import numpy as np
import rclpy
import torch
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, Image, PointCloud2
from std_msgs.msg import Float64MultiArray
from intel_realsense.u2net import U2Net, U2NetP
from intel_realsense.bgrem_utils import *

POINT_STEP = 16

# Must match the normalisation used during training, or the predictions are meaningless.
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


class U2NetBGRM(Node):
    def __init__(self):
        super().__init__('u2net_bgrm')

        self.declare_parameter('color_topic', 'camera/camera/color/image_raw')
        self.declare_parameter('depth_topic', 'camera/camera/aligned_depth_to_color/image_raw')
        self.declare_parameter('info_topic', 'camera/camera/color/camera_info')
        self.declare_parameter('model', 'u2net')
        self.declare_parameter('version', '1')
        self.declare_parameter('device', '')
        self.declare_parameter('amp', True)
        self.declare_parameter('inference_size', 320)
        self.declare_parameter('mask_threshold', 0.5)
        self.declare_parameter('min_depth', 0.20)
        self.declare_parameter('max_depth', 1.20)
        self.declare_parameter('plane_threshold', 0.010)
        self.declare_parameter('plane_clearance', 0.010)
        self.declare_parameter('ransac_iterations', 128)
        self.declare_parameter('ransac_samples', 4000)
        self.declare_parameter('min_blob_area', 300)
        self.declare_parameter('freeze_plane', False)

        self.model = self.get_parameter('model').value
        self.version = self.get_parameter('version').value
        self.inference_size = self.get_parameter('inference_size').value
        self.mask_threshold = self.get_parameter('mask_threshold').value
        self.min_depth = self.get_parameter('min_depth').value
        self.max_depth = self.get_parameter('max_depth').value
        self.plane_threshold = self.get_parameter('plane_threshold').value
        self.plane_clearance = self.get_parameter('plane_clearance').value
        self.ransac_iterations = self.get_parameter('ransac_iterations').value
        self.ransac_samples = self.get_parameter('ransac_samples').value
        self.min_blob_area = self.get_parameter('min_blob_area').value
        self.freeze_plane = self.get_parameter('freeze_plane').value
        self.amp = self.get_parameter('amp').value

        self.rng = np.random.default_rng(0)
        self.kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (5, 5))
        self.rays = None        # (H, W, 2) normalized camera rays
        self.plane = None       # last valid (normal, offset)
        self.latency = None     # smoothed inference time, in seconds
        self.frames = 0

        # === Model ===
        weights = os.path.join(os.getcwd(), 'runs', self.model, 'v'+ self.version, 'best.pth')
        name = self.get_parameter('device').value
        self.device = torch.device(name or ('cuda' if torch.cuda.is_available() else 'cpu'))
        self.mean = torch.tensor(IMAGENET_MEAN, device=self.device).view(3, 1, 1)
        self.std = torch.tensor(IMAGENET_STD, device=self.device).view(3, 1, 1)
        self.model = self.load_model(weights)

        # === Inputs ===
        self.info_sub = self.create_subscription(
            CameraInfo, self.get_parameter('info_topic').value, self.info_callback, 10)

        color_sub = message_filters.Subscriber(self, Image, self.get_parameter('color_topic').value,
                                               qos_profile=qos_profile_sensor_data)
        depth_sub = message_filters.Subscriber(self, Image, self.get_parameter('depth_topic').value,
                                               qos_profile=qos_profile_sensor_data)

        # Synchronize color and depth topics so that the callbacks receive corresponding frames
        self.sync = message_filters.ApproximateTimeSynchronizer([color_sub, depth_sub], 10, 0.05)
        self.sync.registerCallback(self.image_callback)

        # === Outputs ===
        self.mask_pub = self.create_publisher(Image, '~/foreground/mask', 10)
        self.fg_pub = self.create_publisher(Image, '~/foreground/image_raw', 10)
        self.cloud_pub = self.create_publisher(PointCloud2, '~/foreground/points', 10)

        # Latched: superdec_node needs the plane even if it starts after the first frame.
        self.plane_pub = self.create_publisher(
            Float64MultiArray, '~/foreground/plane',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self.get_logger().info('U2Net background removal node initialized.')

    def load_model(self, weights):
        """
        Build U2Net and restore the trained weights.

        Accepts both a checkpoint written by `u2net_train.py` and a bare state dict.
        """
        if not os.path.isfile(weights):
            raise FileNotFoundError(f'Weights not found: {weights}')

        model = U2Net() if self.model=='u2net' else U2NetP()
        checkpoint = torch.load(weights, map_location='cpu')
        model.load_state_dict(checkpoint.get('model', checkpoint))
        model.to(self.device).eval()

        origin = f", epoch {checkpoint['epoch']}" if 'epoch' in checkpoint else ''
        self.get_logger().info(f'{'U2Net' if self.model=='u2net' else 'U2NetP'} loaded on {self.device}{origin}: {weights}')

        # Warm-up, so cuDNN picks its algorithms before the first camera frame arrives.
        with torch.inference_mode():
            model(torch.zeros(1, 3, self.inference_size, self.inference_size, device=self.device))
        return model

    def info_callback(self, msg):
        """
        Process the camera info message to extract intrinsic parameters and compute camera rays.
        """
        fx, fy, cx, cy = msg.k[0], msg.k[4], msg.k[2], msg.k[5]
        if fx == 0.0 or fy == 0.0:
            return
        u, v = np.meshgrid(np.arange(msg.width, dtype=np.float32),
                           np.arange(msg.height, dtype=np.float32))

        # Compute the normalized camera rays for each pixel.
        self.rays = np.dstack(((u - cx) / fx, (v - cy) / fy))
        self.destroy_subscription(self.info_sub)

    def infer_mask(self, rgb):
        """
        Run U2Net on an RGB image and return a full resolution ``uint8`` mask of 0 and 255.

        Only the fused output is used; the six side outputs exist for training supervision.
        """
        height, width = rgb.shape[:2]
        resized = cv.resize(rgb, (self.inference_size, self.inference_size),
                            interpolation=cv.INTER_LINEAR)

        tensor = torch.from_numpy(np.ascontiguousarray(resized)).to(self.device)
        tensor = tensor.permute(2, 0, 1).float().div_(255.0).sub_(self.mean).div_(self.std).unsqueeze(0)

        use_amp = self.amp and self.device.type == 'cuda'
        with torch.inference_mode(), torch.autocast(self.device.type, dtype=torch.bfloat16,
                                                    enabled=use_amp):
            probability = torch.sigmoid(self.model(tensor)[0])
        probability = probability[0, 0].float().cpu().numpy()

        # Upsample the probability and threshold afterwards, or the outlines come out jagged.
        probability = cv.resize(probability, (width, height), interpolation=cv.INTER_LINEAR)
        return ((probability > self.mask_threshold) * 255).astype(np.uint8)

    def update_plane(self, points, is_background):
        """
        Refit the table plane using only the points the network called background.

        Leaving the objects out keeps a large item from tilting the fit, which is what
        happens when the plane is estimated from every point in the volume.
        """
        if self.plane is not None and self.freeze_plane:
            return

        candidates = points[is_background] if np.count_nonzero(is_background) >= 3 else points
        if candidates.shape[0] > self.ransac_samples:
            candidates = candidates[self.rng.choice(candidates.shape[0], self.ransac_samples,
                                                    replace=False)]

        self.plane = fit_plane_ransac(candidates, self.plane_threshold,
                                      self.ransac_iterations, self.rng) or self.plane

    def record_latency(self, seconds):
        """
        Keep a smoothed inference time and report it once in a while.
        """
        self.latency = seconds if self.latency is None else 0.9 * self.latency + 0.1 * seconds
        self.frames += 1
        if self.frames % 60 == 0:
            self.get_logger().info(f'inference {self.latency * 1e3:.1f} ms '
                                   f'({1.0 / self.latency:.1f} FPS)')

    def image_callback(self, color_msg, depth_msg):
        """
        Segment one colour frame and publish the mask, the foreground, the cloud and the plane.
        """
        if self.rays is None:
            return

        # The RealSense publishes rgb8 and the network expects RGB, so nothing is converted here.
        rgb = image_to_array(color_msg)
        if color_msg.encoding.startswith('bgr'):
            rgb = cv.cvtColor(rgb, cv.COLOR_BGR2RGB)
        depth = to_meters(depth_msg)

        if rgb.shape[:2] != depth.shape[:2] or depth.shape[:2] != self.rays.shape[:2]:
            self.get_logger().warn('colour, depth and camera_info resolutions differ',
                                   throttle_duration_sec=5.0)
            return

        started = time.perf_counter()
        mask = self.infer_mask(rgb)
        self.record_latency(time.perf_counter() - started)

        # Only pixels with usable depth can become 3D points.
        in_volume = (depth > self.min_depth) & (depth < self.max_depth)
        z = depth[in_volume]
        rays = self.rays[in_volume]
        points = np.column_stack((rays[:, 0] * z, rays[:, 1] * z, z))

        self.update_plane(points, mask[in_volume] == 0)

        mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, self.kernel)
        mask[~in_volume] = 0

        if self.plane is not None and self.plane_clearance > 0.0:
            # Optional geometric gate: an object must also stand above the table.
            normal, offset = self.plane
            above = np.zeros(mask.shape, dtype=bool)
            above[in_volume] = (points @ normal + offset) > self.plane_clearance
            mask[~above] = 0

        mask, labels = label_blobs(self.min_blob_area, mask)
        bgr = cv.cvtColor(rgb, cv.COLOR_RGB2BGR)
        foreground = cv.bitwise_and(bgr, bgr, mask=mask)

        self.mask_pub.publish(array_to_image(mask, 'mono8', color_msg.header))
        self.fg_pub.publish(array_to_image(foreground, 'bgr8', color_msg.header))
        self.publish_cloud(color_msg.header, labels, in_volume, points)

        if self.plane is not None:
            normal, offset = self.plane
            self.plane_pub.publish(Float64MultiArray(data=[*map(float, normal), float(offset)]))

    def publish_cloud(self, header, labels, in_volume, points):
        """
        Publish the foreground points as ``[x, y, z, instance]``, the layout superdec_node reads.
        """
        ids = labels[in_volume].astype(np.float32)
        keep = ids > 0
        cloud = np.column_stack((points[keep], ids[keep])).astype(np.float32)

        msg = PointCloud2()
        msg.header = header
        msg.height = 1
        msg.width = cloud.shape[0]
        msg.fields = FIELDS
        msg.is_bigendian = False
        msg.point_step = POINT_STEP
        msg.row_step = POINT_STEP * cloud.shape[0]
        msg.is_dense = True
        msg.data = cloud.tobytes()
        self.cloud_pub.publish(msg)


def main(args=None):
    rclpy.init(args=args)
    node = U2NetBGRM()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info("U2Net background removal node is shutting down.")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()