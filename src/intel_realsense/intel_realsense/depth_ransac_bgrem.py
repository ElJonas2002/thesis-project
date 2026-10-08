"""
This module provides the implementation for the Intel RealSense IRS BGREM V1.1 node.
"""
import cv2 as cv
import numpy as np
import message_filters
import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from sensor_msgs.msg import Image, CameraInfo, PointCloud2
from std_msgs.msg import Float64MultiArray
from intel_realsense.bgrem_utils import *


class IRSNode(Node):
    def __init__(self):
        super().__init__("irs_bgrem_v1_1_node")

        # === DEPTH AND PLANE PARAMETERS ===
        self.declare_parameter('min_depth', 0.20)
        self.declare_parameter('max_depth', 1.20)
        self.declare_parameter('plane_threshold', 0.01)
        self.declare_parameter('plane_clearance', 0.01)

        # === EDGE DETECTION PARAMETERS ===
        self.declare_parameter('edge_threshold', 0.001)
        self.declare_parameter('edge_reference', 0.1)

        self.declare_parameter('cloud_erode', 0)

        # === COLOR GUIDE PARAMETERS ===
        self.declare_parameter('guide_radius', 16)
        self.declare_parameter('guide_eps', 1e-2)

        # === RANSAC PARAMETERS ===
        self.declare_parameter('ransac_iterations', 128)
        self.declare_parameter('ransac_samples', 4000)
        self.declare_parameter('min_blob_area', 300)
        self.declare_parameter('freeze_plane', False)
        self.declare_parameter('show_window', True)

        self.min_depth = self.get_parameter('min_depth').value
        self.max_depth = self.get_parameter('max_depth').value
        self.plane_threshold = self.get_parameter('plane_threshold').value
        self.plane_clearance = self.get_parameter('plane_clearance').value
        self.edge_threshold = self.get_parameter('edge_threshold').value
        self.ransac_iterations = self.get_parameter('ransac_iterations').value
        self.ransac_samples = self.get_parameter('ransac_samples').value
        self.min_blob_area = self.get_parameter('min_blob_area').value
        self.freeze_plane = self.get_parameter('freeze_plane').value
        self.show_window = self.get_parameter('show_window').value
        self.edge_reference = self.get_parameter('edge_reference').value
        self.guide_radius = self.get_parameter('guide_radius').value
        self.guide_eps = self.get_parameter('guide_eps').value
        self.cloud_erode = self.get_parameter('cloud_erode').value

        self.rng = np.random.default_rng(0)
        self.kernel = cv.getStructuringElement(cv.MORPH_RECT, (5, 5))
        self.edge_kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
        self.rays = None      # (H, W, 2) normalized camera rays
        self.plane = None     # last valid (normal, offset)
        self.last_cloud = None   # (points, instance ids) of the most recent frame

        self.info_sub = self.create_subscription(
            CameraInfo, INFO_TOPIC, self.info_callback, 10)

        color_sub = message_filters.Subscriber(self, Image, RGB_TOPIC)
        depth_sub = message_filters.Subscriber(self, Image, DEPTH_TOPIC)

        # Synchronize color and depth topics so that the callbacks receive corresponding frames
        self.sync = message_filters.ApproximateTimeSynchronizer([color_sub, depth_sub], 10, 0.05)
        self.sync.registerCallback(self.image_callback)

        # Publishers for the foreground mask, foreground image, and point cloud
        self.mask_pub = self.create_publisher(Image, '~/foreground/mask', 10)
        self.fg_pub = self.create_publisher(Image, '~/foreground/image_raw', 10)
        self.cloud_pub = self.create_publisher(PointCloud2, '~/foreground/points', 10)

        # Latched: superdec_node needs the plane even if it starts after the first frame.
        self.plane_pub = self.create_publisher(
            Float64MultiArray, '~/foreground/plane',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        self.get_logger().info("Intel RealSense node initialized.")

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
    
    def publish(self, header, mask, foreground):
            self.mask_pub.publish(array_to_image(mask, 'mono8', header))
            self.fg_pub.publish(array_to_image(foreground, 'bgr8', header))
    
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
        msg.row_step = 16 * cloud.shape[0]  # Point step X number of points
        msg.is_dense = True
        msg.data = cloud.tobytes()  # Convert the point cloud to bytes for ROS message
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
        if depth.shape[:2] != self.rays.shape[:2]:
            self.get_logger().warn(
                'Depth and camera_info resolutions differ; subscribe to the aligned depth topic.')
            return

        # Determine which points are within the valid depth range
        in_volume = (depth > self.min_depth) & (depth < self.max_depth)
        in_volume = reject_flying_pixels(depth, self.edge_kernel, self.edge_threshold, self.edge_reference, self.min_depth, self.max_depth)
        #in_volume = self.reject_grazing(depth, in_volume)
        if np.count_nonzero(in_volume) < 3:
            in_volume[:] = False

        # Initialize the mask, labels, and points arrays for the current frame.
        mask = np.zeros(depth.shape, dtype=np.uint8)
        labels = np.zeros(depth.shape, dtype=np.int32)
        points = np.empty((0, 3))
        above = np.zeros(depth.shape, dtype=bool)

        if in_volume.any():

            # Convert the valid depth points to 3D coordinates using the camera rays
            z = depth[in_volume]
            rays = self.rays[in_volume]
            points = np.column_stack((rays[:,0] * z, rays[:,1] * z, z))

            # If there are enough points, randomly sample a subset for RANSAC plane fitting.
            # Otherwise, use all available points for RANSAC plane fitting.
            if self.plane is None or not self.freeze_plane:
                if points.shape[0] > self.ransac_samples:
                    sample = points[self.rng.choice(points.shape[0], self.ransac_samples, replace=False)]
                else:
                    sample = points
                    
                self.plane = fit_plane_ransac(
                    sample, self.plane_threshold, self.ransac_iterations, self.rng) or self.plane

            # Build the mask for the foreground based on the fitted plane and the plane clearance.
            if self.plane is None:
                above = in_volume.copy()
            else:
                normal, offset = self.plane
                above[in_volume] = (points @ normal + offset) > self.plane_clearance
            mask[above] = 255

            # TODO: test different filters to improve mask quality
            # Apply morphological operations to clean up the mask.
            #mask = cv.morphologyEx(mask, cv.MORPH_OPEN, self.kernel)
            mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, self.kernel)
            mask = snap_to_colour(mask, color, self.guide_radius, self.guide_eps)

            # Label the connected components in the mask.
            mask, labels = label_blobs(self.min_blob_area, mask)

        # Extract the foreground by applying the mask to the color image.
        foreground = cv.bitwise_and(color, color, mask=mask)
        self.publish(color_msg.header, mask, foreground)

        # The refined mask may grow onto the table; only geometrically raised pixels become points.
        cloud_labels = np.where(above, labels, 0)
        if self.cloud_erode > 0:
            core = cv.erode(mask, self.edge_kernel, iterations=self.cloud_erode)
            cloud_labels[core == 0] = 0

        self.publish_cloud(color_msg.header, cloud_labels, in_volume, points)
        if self.plane is not None:
            normal, offset = self.plane
            self.plane_pub.publish(Float64MultiArray(data=[*map(float, normal), float(offset)]))

        if self.show_window:
            cv.imshow("Intel RealSense Foreground", foreground)
            cv.imshow("Intel RealSense Depth", depth)
            if cv.waitKey(1) & 0xFF == ord('q'):
                raise KeyboardInterrupt

def main(args=None):
    rclpy.init(args=args)
    node = IRSNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info("Intel RealSense node is shutting down.")
        cv.destroyAllWindows()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()

if __name__ == "__main__":
    main()
