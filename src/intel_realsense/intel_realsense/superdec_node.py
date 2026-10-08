"""
Turns the per-instance clouds published by `fastsam_node` into superquadrics.

Kept as a separate node on purpose: loading torch/CUDA and running inference takes
hundreds of milliseconds, which would stall the 30 Hz image pipeline if it lived inside
the synchronised image callback. This node works off the latest cloud at its own rate.

    ros2 run intel_realsense superdec_node
    ros2 service call /superdec_node/decompose std_srvs/srv/Trigger
"""
import os
import time
import numpy as np
import rclpy
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile
from geometry_msgs.msg import Point
from sensor_msgs.msg import PointCloud2
from std_msgs.msg import ColorRGBA, Float64MultiArray
from std_srvs.srv import Trigger
from visualization_msgs.msg import Marker, MarkerArray
from intel_realsense.superdec_utils import SuperDecRunner

POINT_STEP = 16
POINTCLOUD_TOPIC = '/fastsam_node/foreground/points'
PLANE_TOPIC = '/fastsam_node/foreground/plane'


def decode_cloud(msg):
    """Read the `[x, y, z, instance]` layout written by `irs_node` straight out of the buffer."""
    if msg.point_step != POINT_STEP or not msg.data:
        return None
    data = np.frombuffer(msg.data, dtype=np.float32).reshape(-1, 4)
    return data[:, :3].astype(np.float64), data[:, 3].astype(np.int32)


def surface_topology(resolution):
    """Parameter grid and triangle indices, built once and reused by every primitive."""
    eta, omega = np.meshgrid(np.linspace(-np.pi / 2, np.pi / 2, resolution),
                             np.linspace(-np.pi, np.pi, 2 * resolution), indexing='ij')
    index = np.arange(eta.size).reshape(eta.shape)
    quads = np.stack([index[:-1, :-1], index[1:, :-1], index[1:, 1:], index[:-1, 1:]],
                     axis=-1).reshape(-1, 4)
    triangles = np.concatenate([quads[:, [0, 1, 2]], quads[:, [0, 2, 3]]]).ravel()
    return eta.ravel(), omega.ravel(), triangles


def signed_pow(values, exponent):
    """Real-valued power that keeps the sign, as the superquadric parameterisation needs."""
    return np.sign(values) * np.abs(values) ** exponent


def surface_points(scale, shape, eta, omega, triangles):
    """
    Superquadric surface in its own frame, expanded into a triangle soup.

    x and y carry the cross-section exponent and z the profile one, matching the
    inside-outside function the fitter minimises.
    """
    e_prof, e_sect = float(shape[0]), float(shape[1])
    profile_c, profile_s = signed_pow(np.cos(eta), e_prof), signed_pow(np.sin(eta), e_prof)
    section_c, section_s = signed_pow(np.cos(omega), e_sect), signed_pow(np.sin(omega), e_sect)
    vertices = np.stack([scale[0] * profile_c * section_c,
                         scale[1] * profile_c * section_s,
                         scale[2] * profile_s], axis=1)
    return vertices[triangles]


def quaternion_from_matrix(rotation):
    """Shepperd's method: pick the largest component first to avoid a near-zero divisor."""
    trace = np.trace(rotation)
    if trace > 0.0:
        s = 0.5 / np.sqrt(trace + 1.0)
        return np.array([(rotation[2, 1] - rotation[1, 2]) * s,
                         (rotation[0, 2] - rotation[2, 0]) * s,
                         (rotation[1, 0] - rotation[0, 1]) * s, 0.25 / s])
    i = int(np.argmax(np.diag(rotation)))
    j, k = (i + 1) % 3, (i + 2) % 3
    s = 2.0 * np.sqrt(1.0 + rotation[i, i] - rotation[j, j] - rotation[k, k])
    q = np.empty(4)
    q[3] = (rotation[k, j] - rotation[j, k]) / s
    q[i] = 0.25 * s
    q[j] = (rotation[j, i] + rotation[i, j]) / s
    q[k] = (rotation[k, i] + rotation[i, k]) / s
    return q


def instance_color(instance):
    """Golden-angle hue steps keep neighbouring instance ids visually far apart."""
    hue = (instance * 0.618033988749895) % 1.0
    sector, frac = divmod(hue * 6.0, 1.0)
    ramp = [(1, frac, 0), (1 - frac, 1, 0), (0, 1, frac),
            (0, 1 - frac, 1), (frac, 0, 1), (1, 0, 1 - frac)][int(sector) % 6]
    return ColorRGBA(r=float(ramp[0]), g=float(ramp[1]), b=float(ramp[2]), a=0.65)


class SuperDecNode(Node):
    def __init__(self):
        super().__init__('superdec_node')
        self.declare_parameter('device', '')
        self.declare_parameter('rate', 1.0)
        self.declare_parameter('min_points', 512)
        self.declare_parameter('max_instances', 8)
        self.declare_parameter('resolution', 12)
        self.declare_parameter('denoise', True)
        self.declare_parameter('complete', True)
        self.declare_parameter('canonical', True)
        self.declare_parameter('uniform', True)
        self.declare_parameter('merge', True)
        self.declare_parameter('merge_tol', 1.15)
        self.declare_parameter('merge_grid', 5)
        self.declare_parameter('merge_cap', 192)
        self.declare_parameter('verbose', False)

        self.verbose = self.get_parameter('verbose').value

        self.min_points = self.get_parameter('min_points').value
        self.max_instances = self.get_parameter('max_instances').value
        self.topology = surface_topology(self.get_parameter('resolution').value)

        self.cloud = None
        self.header = None
        self.plane = None
        self.busy = False

        self.runner = SuperDecRunner(
            os.path.abspath('superdec'),                        # superdec root
            os.path.abspath('superdec/checkpoints/normalized'), # checkpoint directory
            device=self.get_parameter('device').value or None,
            denoise=self.get_parameter('denoise').value,
            complete=self.get_parameter('complete').value,
            canonical=self.get_parameter('canonical').value,
            uniform=self.get_parameter('uniform').value,
            merge=self.get_parameter('merge').value,
            merge_tol=self.get_parameter('merge_tol').value,
            merge_grid=self.get_parameter('merge_grid').value,
            merge_cap=self.get_parameter('merge_cap').value)
        self.get_logger().info(f'SuperDec loaded on {self.runner.device}.')

        inputs = ReentrantCallbackGroup()
        self.create_subscription(PointCloud2, POINTCLOUD_TOPIC,
                                 self.cloud_callback, 1, callback_group=inputs)
        self.create_subscription(
            Float64MultiArray, PLANE_TOPIC, self.plane_callback,
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
            callback_group=inputs)

        self.marker_pub = self.create_publisher(MarkerArray, '~/primitives', 1)

        work = MutuallyExclusiveCallbackGroup()
        self.create_service(Trigger, '~/decompose', self.decompose_callback, callback_group=work)
        rate = self.get_parameter('rate').value
        if rate > 0.0:
            self.create_timer(1.0 / rate, self.timer_callback, callback_group=work)

    def cloud_callback(self, msg):
        decoded = decode_cloud(msg)
        if decoded is None:
            self.get_logger().warn('Unexpected cloud layout; expected x, y, z, instance.')
            return
        self.cloud, self.header = decoded, msg.header

    def plane_callback(self, msg):
        if len(msg.data) == 4:
            self.plane = (np.array(msg.data[:3]), float(msg.data[3]))

    def timer_callback(self):
        self.decompose()

    def decompose_callback(self, request, response):
        response.message = self.decompose()
        response.success = response.message.startswith('Published')
        return response

    def decompose(self):
        if self.busy:
            return 'Still processing the previous frame.'
        if self.cloud is None:
            return 'No cloud received yet.'

        points, ids = self.cloud
        header = self.header
        labels = [i for i in np.unique(ids) if i > 0]
        clouds, kept = [], []
        for label in labels[:self.max_instances]:
            cluster = points[ids == label]
            if cluster.shape[0] >= self.min_points:
                clouds.append(cluster)
                kept.append(int(label))
        if not clouds:
            self.publish_markers(header, [], [])
            return f'No instance reached {self.min_points} points.'

        self.busy = True
        try:
            start = time.perf_counter()
            results = self.runner(clouds, self.plane)
            elapsed = time.perf_counter() - start
        except Exception as error:                      # keep the node alive on a bad frame
            self.get_logger().error(f'SuperDec failed: {error}')
            return f'SuperDec failed: {error}'
        finally:
            self.busy = False

        self.publish_markers(header, kept, results)

        total = sum(r['scale'].shape[0] for r in results)
        message = (f'Published {total} superquadrics for {len(results)} instances '
                f'in {elapsed:.2f} s.')
        if self.verbose:
            self.get_logger().info(message)
        return message

    def publish_markers(self, header, labels, results):
        markers = MarkerArray()
        clear = Marker()
        clear.header = header
        clear.action = Marker.DELETEALL
        markers.markers.append(clear)

        for label, result in zip(labels, results):
            color = instance_color(label)
            for i in range(result['scale'].shape[0]):
                quaternion = quaternion_from_matrix(result['rotate'][i])
                marker = Marker()
                marker.header = header
                marker.ns = f'instance_{label}'
                marker.id = i
                marker.type = Marker.TRIANGLE_LIST
                marker.action = Marker.ADD
                marker.pose.position.x = float(result['trans'][i, 0])
                marker.pose.position.y = float(result['trans'][i, 1])
                marker.pose.position.z = float(result['trans'][i, 2])
                marker.pose.orientation.x = float(quaternion[0])
                marker.pose.orientation.y = float(quaternion[1])
                marker.pose.orientation.z = float(quaternion[2])
                marker.pose.orientation.w = float(quaternion[3])
                marker.scale.x = marker.scale.y = marker.scale.z = 1.0
                marker.color = color
                marker.points = [Point(x=float(p[0]), y=float(p[1]), z=float(p[2]))
                                 for p in surface_points(result['scale'][i], result['shape'][i],
                                                         *self.topology)]
                markers.markers.append(marker)
        self.marker_pub.publish(markers)


def main(args=None):
    rclpy.init(args=args)
    node = SuperDecNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.get_logger().info('SuperDec node is shutting down.')
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
