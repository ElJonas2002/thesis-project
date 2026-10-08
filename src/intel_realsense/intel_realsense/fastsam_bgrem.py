from ultralytics import FastSAM
import time
import cv2 as cv
import numpy as np
import os
import torch

import rclpy
from rclpy.node import Node
import message_filters
from rclpy.qos import QoSProfile, DurabilityPolicy
from rcl_interfaces.msg import SetParametersResult
from sensor_msgs.msg import Image, CameraInfo, PointCloud2
from std_msgs.msg import String, Float64MultiArray
from ultralytics.utils.ops import scale_masks
from intel_realsense.bgrem_utils import *


def color_palette(n, seed=0):
    """Deterministic BGR colors so a given mask index keeps its color."""
    rng = np.random.default_rng(seed)
    return rng.integers(64, 255, size=(n, 3), dtype=np.uint8)

def overlay_masks(frame, masks, palette, alpha=0.5):
    """Blend ``(N, H, W)`` boolean masks onto ``frame`` and draw their contours."""
    if masks is None or len(masks) == 0:
        return frame

    canvas = np.zeros_like(frame)
    for i, mask in enumerate(masks):
        if mask.shape[:2] != frame.shape[:2]:
            mask = cv.resize(mask.astype(np.uint8), (frame.shape[1], frame.shape[0]),
                             interpolation=cv.INTER_NEAREST).astype(bool)
        color = palette[i % len(palette)]
        canvas[mask] = color
        contours, _ = cv.findContours(mask.astype(np.uint8), cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE)
        cv.drawContours(frame, contours, -1, color.tolist(), 1)

    painted = canvas.any(axis=2)
    frame[painted] = cv.addWeighted(frame, 1 - alpha, canvas, alpha, 0)[painted]
    return frame

def parse_prompt(text):
        """Turn a raw CLI/stdin string into the ``texts`` list FastSAM expects."""
        if not text:
            return None
        terms = [t.strip() for t in text.split(',') if t.strip()]
        return terms or None

class FastSAMNode(Node):
    def __init__(self):
        super().__init__('fastsam_node')
        self.declare_parameter('model', 'FastSAM-x.pt')
        self.declare_parameter('device', '')
        self.declare_parameter('img_size', 640)
        self.declare_parameter('conf', 0.7)
        self.declare_parameter('iou', 0.85)
        self.declare_parameter('prompt', '')

        # ==================== RANSAC PARAMETERS ====================
        self.declare_parameter('min_depth', 0.20)
        self.declare_parameter('max_depth', 1.20)
        self.declare_parameter('plane_threshold', 0.01)
        self.declare_parameter('plane_clearance', 0.01)
        self.declare_parameter('edge_threshold', 0.001) # previously 0.01
        self.declare_parameter('edge_reference', 0.1) # previously 0.1
        self.declare_parameter('ransac_iterations', 128)
        self.declare_parameter('ransac_samples', 4000)
        self.declare_parameter('min_blob_area', 300)
        self.declare_parameter('freeze_plane', False)
        self.declare_parameter('show_window', True)

        # ==================== MASK FILTER PARAMETERS ====================
        self.declare_parameter('max_mask_ratio', 0.3)    # masks larger than this image fraction are table/background
        self.declare_parameter('min_above_ratio', 0.5)   # fraction of a mask's valid depth that must be above the table
        self.max_mask_ratio = self.get_parameter('max_mask_ratio').value
        self.min_above_ratio = self.get_parameter('min_above_ratio').value

        self.min_depth = self.get_parameter('min_depth').value
        self.max_depth = self.get_parameter('max_depth').value
        self.plane_threshold = self.get_parameter('plane_threshold').value
        self.plane_clearance = self.get_parameter('plane_clearance').value
        self.edge_threshold = self.get_parameter('edge_threshold').value
        self.edge_reference = self.get_parameter('edge_reference').value
        self.ransac_iterations = self.get_parameter('ransac_iterations').value
        self.ransac_samples = self.get_parameter('ransac_samples').value
        self.min_blob_area = self.get_parameter('min_blob_area').value
        self.freeze_plane = self.get_parameter('freeze_plane').value
        self.show_window = self.get_parameter('show_window').value

        self.rng = np.random.default_rng(0)
        self.kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (5, 5))
        self.edge_kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
        self.rays = None      # (H, W, 2) normalized camera rays
        self.plane = None     # last valid (normal, offset)
        self.last_cloud = None   # (points, instance ids) of the most recent frame

        name = self.get_parameter('device').value
        model_file = self.get_parameter('model').value

        self.model = FastSAM(os.path.join(os.getcwd(), 'models', model_file))
        self.device = torch.device(name or torch.device('cuda' if torch.cuda.is_available() else 'cpu')) 
        self.img_size = self.get_parameter('img_size').value
        self.conf = self.get_parameter('conf').value
        self.iou = self.get_parameter('iou').value
        self.prompt = self.get_parameter('prompt').value

        self.info_sub = self.create_subscription(CameraInfo, INFO_TOPIC, self.info_callback, 10)
        color_sub = message_filters.Subscriber(self, Image, RGB_TOPIC)
        depth_sub = message_filters.Subscriber(self, Image, DEPTH_TOPIC)
        
        # Synchronize color and depth topics so that the callbacks receive corresponding frames
        self.ts = message_filters.ApproximateTimeSynchronizer([color_sub, depth_sub], 10, 0.1)
        self.ts.registerCallback(self.image_callback)

        self.mask_pub = self.create_publisher(Image, '~/foreground/mask', 10)
        self.cloud_pub = self.create_publisher(PointCloud2, '~/foreground/points', 10)
        # Latched: superdec_node needs the plane even if it starts after the first frame.
        self.plane_pub = self.create_publisher(
            Float64MultiArray, '~/foreground/plane',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self.texts = parse_prompt(self.prompt)
        self.palette = color_palette(32)
        self.fps = 0.0
        self.saved = 0

        # Runtime prompt changes: `ros2 param set` or a String on ~/prompt.
        self.add_on_set_parameters_callback(self.on_parameters)
        self.prompt_sub = self.create_subscription(String, '~/prompt', self.prompt_callback, 10)

        self.get_logger().info(f"FastSAM initialized with model={model_file}, device={self.device}, conf={self.conf}, iou={self.iou}, prompt={self.texts}")

    def set_prompt(self, text):
        self.prompt = text
        self.texts = parse_prompt(text)
        self.get_logger().info(f"Prompt: {self.texts if self.texts else '(segment everything)'}")

    def on_parameters(self, params):
        for param in params:
            if param.name == 'prompt':
                if param.type_ != param.Type.STRING:
                    return SetParametersResult(successful=False, reason='prompt must be a string')
                self.set_prompt(param.value)
        return SetParametersResult(successful=True)

    def prompt_callback(self, msg):
        self.set_prompt(msg.data)

    def info_callback(self, msg):
            """
            Process the camera info message to extract intrinsic parameters and compute camera rays.
            """
            self.fx = msg.k[0]
            self.fy = msg.k[4]
            self.cx = msg.k[2]
            self.cy = msg.k[5]
            if self.fx == 0.0 or self.fy == 0.0:
                return
            u, v = np.meshgrid(np.arange(msg.width, dtype=np.float32),
                               np.arange(msg.height, dtype=np.float32))
            self.rays = np.dstack(((u - self.cx) / self.fx, (v - self.cy) / self.fy))
            self.destroy_subscription(self.info_sub)
    
    def fit_table(self, depth):
        """
        Returns ``(in_volume, above, points)``: valid-depth pixels, pixels raised above the
        table plane, and the 3D points of ``in_volume`` in row-major order.
        """
        in_volume = reject_flying_pixels(depth, self.edge_kernel, self.edge_threshold, self.edge_reference, self.min_depth, self.max_depth)
        above = np.zeros(depth.shape, dtype=bool)
        if np.count_nonzero(in_volume) < 3:
            return np.zeros_like(in_volume), above, np.empty((0, 3), np.float32)

        z = depth[in_volume]
        rays = self.rays[in_volume]
        points = np.column_stack((rays[:, 0] * z, rays[:, 1] * z, z))

        if self.plane is None or not self.freeze_plane:
            sample = points
            if points.shape[0] > self.ransac_samples:
                sample = points[self.rng.choice(points.shape[0], self.ransac_samples, replace=False)]
            self.plane = fit_plane_ransac(
                sample, self.plane_threshold, self.ransac_iterations, self.rng) or self.plane

        if self.plane is None:
            above[in_volume] = True
        else:
            normal, offset = self.plane
            above[in_volume] = (points @ normal + offset) > self.plane_clearance
        return in_volume, above, points

    @staticmethod
    def full_masks(result):
        """``(N, H, W)`` boolean mask tensor at the original image resolution."""
        if result.masks is None or len(result) == 0:
            return torch.zeros((0, *result.orig_shape), dtype=torch.bool)
        masks = result.masks.data
        if masks.shape[1:] != result.orig_shape:
            masks = scale_masks(masks[None].float(), result.orig_shape)[0]
        return masks > 0.5

    def on_table(self, masks, in_volume, above):
        """Boolean index of the masks that stand on the table plane."""
        h, w = in_volume.shape
        above_t = torch.from_numpy(above).to(masks.device)
        valid_t = torch.from_numpy(in_volume).to(masks.device)
        area = masks.sum((1, 2))
        raised = (masks & above_t).sum((1, 2))
        valid = (masks & valid_t).sum((1, 2))
        return ((area < self.max_mask_ratio * h * w)
                & (raised >= self.min_blob_area)
                & (raised > self.min_above_ratio * valid))

    def publish_cloud(self, header, labels, in_volume, points):
        ids = labels[in_volume].astype(np.float32)
        keep = ids > 0
        cloud = np.column_stack((points[keep], ids[keep])).astype(np.float32)
        self.last_cloud = (points[keep], ids[keep])

        msg = PointCloud2()
        msg.header = header
        msg.height = 1
        msg.width = cloud.shape[0]
        msg.fields = FIELDS
        msg.is_bigendian = False
        msg.point_step = 16
        msg.row_step = 16 * cloud.shape[0]
        msg.is_dense = True
        msg.data = cloud.tobytes()
        self.cloud_pub.publish(msg)

    def image_callback(self, color_msg, depth_msg):
        """
        Process synchronized color and depth images.
        """
        if self.rays is None:
            return

        # Convert the incoming ROS image messages (color and depth) to OpenCV images
        color = image_to_array(color_msg)
        if color_msg.encoding.startswith('rgb'):
            color = cv.cvtColor(color, cv.COLOR_RGB2BGR)

        depth = to_meters(depth_msg)
        if depth.shape[:2] != self.rays.shape[:2] or depth.shape[:2] != color.shape[:2]:
            self.get_logger().warn(
                'Depth, colour and camera_info resolutions differ; subscribe to the aligned depth topic.',
                throttle_duration_sec=5.0)
            return

        start = time.perf_counter()
        in_volume, above, points = self.fit_table(depth)

        # Segment everything first so CLIP only ranks masks that survived the table filter.
        result = self.model.predict(color, device=self.device, conf=self.conf, iou=self.iou,
                                    verbose=False, imgsz=self.img_size)[0]
        masks = self.full_masks(result)
        if len(masks):
            keep = self.on_table(masks, in_volume, above)
            result, masks = result[keep], masks[keep]
        if self.texts and len(result):
            result = self.model.predictor.prompt(result, texts=self.texts)[0]
            masks = self.full_masks(result)

        masks = masks.cpu().numpy()
        labels = np.zeros(depth.shape, dtype=np.int32)

        # Paint largest first so nested (smaller) masks win the overlap.
        for i in np.argsort(-masks.sum((1, 2))):
            labels[masks[i]] = i + 1
        labels[~above] = 0

        # Overlap resolution can leave slivers of the larger masks behind.
        ids, counts = np.unique(labels, return_counts=True)
        labels[np.isin(labels, ids[counts < self.min_blob_area])] = 0

        elapsed = time.perf_counter() - start
        self.fps = 0.9 * self.fps + 0.1 / elapsed if self.fps else 1.0 / elapsed

        # Publish results
        self.mask_pub.publish(array_to_image(((labels > 0) * 255).astype(np.uint8), 'mono8', color_msg.header))
        self.publish_cloud(color_msg.header, labels, in_volume, points)
        if self.plane is not None:
            normal, offset = self.plane
            self.plane_pub.publish(Float64MultiArray(data=[*map(float, normal), float(offset)]))

        if self.show_window:
            annotated = overlay_masks(color.copy(), masks, self.palette)
            count = len(masks)
            cv.putText(annotated, f"{self.fps:5.1f} FPS | {count} masks", (10, 25),
                        cv.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2, cv.LINE_AA)
            label = f"prompt: {', '.join(self.texts)}" if self.texts else "prompt: (segment everything)"
            cv.putText(annotated, label, (10, 50),
                       cv.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2, cv.LINE_AA)
            cv.imshow("YOLOv8Seg", annotated)

            key = cv.waitKey(1) & 0xFF
            if key == ord('q'):
                raise KeyboardInterrupt
            if key == ord('s'):
                path = f"yolov8seg_frame_{self.saved:03d}.png"
                cv.imwrite(path, annotated)
                print(f"Saved {path}")
                self.saved += 1
            if key == ord('t'):
                # Blocks spinning until the prompt is typed in the node's terminal.
                self.set_prompt(input("Text prompt (empty = segment everything): "))
            if key == ord('c'):
                self.set_prompt('')

def main(args=None):
    rclpy.init(args=args)
    node = FastSAMNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info("FastSAMNode is shutting down.")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()
