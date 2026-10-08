"""Background removal utility functions for Intel RealSense."""

import cv2 as cv
import numpy as np
from sensor_msgs.msg import Image, PointField, PointCloud2

# Each point in the point cloud consists of x, y, z as float32 plus the connected-component id the point belongs to.
POINT_STEP = 16
FIELDS = [
    PointField(name='x', offset=0,  datatype=PointField.FLOAT32, count=1),
    PointField(name='y', offset=4,  datatype=PointField.FLOAT32, count=1),
    PointField(name='z', offset=8,  datatype=PointField.FLOAT32, count=1),
    PointField(name='instance', offset=12, datatype=PointField.FLOAT32, count=1),
]

# Decoding images here instead of through cv_bridge
ENCODINGS = {
    'rgb8': (np.uint8, 3), 'bgr8': (np.uint8, 3),
    'rgba8': (np.uint8, 4), 'bgra8': (np.uint8, 4),
    'mono8': (np.uint8, 1), 'mono16': (np.uint16, 1),
    '16UC1': (np.uint16, 1), '32FC1': (np.float32, 1),
}

RGB_TOPIC = 'camera/camera/color/image_raw'
DEPTH_TOPIC = 'camera/camera/aligned_depth_to_color/image_raw'
INFO_TOPIC = 'camera/camera/color/camera_info'


def image_to_array(img_msg):
    """
    Decode a sensor_msgs/Image into a NumPy array of shape ``(H, W)`` or ``(H, W, C)``.
    """
    if img_msg.encoding not in ENCODINGS:
        raise ValueError(f'Unsupported image encoding: {img_msg.encoding}')
    
    base, channels = ENCODINGS[img_msg.encoding]
    dtype = np.dtype(base).newbyteorder('>' if img_msg.is_bigendian else '<')

    # step is the row stride in bytes and may exceed width * channels when rows are padded.
    rows = np.frombuffer(img_msg.data, dtype=dtype).reshape(img_msg.height, img_msg.step // dtype.itemsize)
    pixels = rows[:, :img_msg.width * channels]
    return pixels.reshape(img_msg.height, img_msg.width, channels) if channels > 1 else pixels


def array_to_image(array, encoding, header):
    """
    Convert a NumPy array into a sensor_msgs/Image message.

    Parameters:
    ----------
    - array : The NumPy array representing the image.
    - encoding : The encoding of the image (e.g., 'rgb8', 'bgr8').
    - header : The header for the Image message.

    Returns:
    -------
    - msg : The sensor_msgs/Image message.
    """
    array = np.ascontiguousarray(array)
    msg = Image()
    msg.header = header
    msg.height, msg.width = int(array.shape[0]), int(array.shape[1])
    msg.encoding = encoding
    msg.is_bigendian = False
    msg.step = int(array.strides[0])
    msg.data = array.tobytes()
    return msg


def to_meters(depth_msg, resolution=1e-3):
    """
    Converts the 16-bit depth image to meters.

    Parameters:
    ----------
    - depth_msg : The ROS Image message containing the depth image.
    - resolution : The conversion factor from the depth image units to meters (default: 1e-3 m/px).

    Returns:
    -------
    - depth : The depth image in meters as a NumPy array of type float32.
    """
    depth = image_to_array(depth_msg)
    depth = cv.medianBlur(depth, 5)

    if depth_msg.encoding in ('16UC1', 'mono16'):
        return depth.astype(np.float32) * resolution
    return np.nan_to_num(depth.astype(np.float32))


def reject_flying_pixels(depth, kernel, edge_threshold=0.001, edge_reference=1.2, min_depth=0.2, max_depth=1.20):
    """
    Discard flying/mixed pixels: stereo matching interpolates across depth
    discontinuities, producing points floating between object and background.

    Parameters:
    ----------
    - depth : The depth image as a distance NumPy array.
    - edge_threshold : The threshold for detecting flying pixels.
    - kernel : The kernel used for edge detection (e.g., a structuring element for dilation/erosion).
    - max_depth : The maximum depth value in the depth image.
    - edge_reference : The reference depth for scaling the edge threshold.
    - in_volume : A boolean mask indicating which pixels are within the valid volume.
    Returns:
    -------
    - in_volume : The updated boolean mask after rejecting flying pixels.
    """
    in_volume = (depth > min_depth) & (depth < max_depth)
    if edge_threshold <= 0.0:
        return in_volume
    depth = depth.astype(np.float32)

    # Invalid pixels must lose both extrema, or every sensor hole reads as a depth jump.
    high = cv.dilate(np.where(in_volume, depth, 0.0), kernel)
    low = cv.erode(np.where(in_volume, depth, 2.0 * max_depth), kernel)

    # Stereo depth error grows with z^2, so a fixed millimetre budget is wrong at both ends.
    budget = edge_threshold * (depth / edge_reference) ** 2
    return in_volume & ((high - low) < budget)


def fit_plane_ransac(points, threshold, iterations, rng):
    """
    Fit a plane to an ``(N, 3)`` array. Returns ``(normal, offset)`` so that
    ``normal . p + offset = 0``, with the normal oriented towards the camera.

    Parameters:
    ----------
    - points : An ``(N, 3)`` ``ndarray`` of 3D points.
    - threshold : Distance threshold for considering a point as an inlier.
    - iterations : Number of RANSAC iterations.
    - rng : Random number generator (`np.random.Generator`) for sampling points.

    Returns:
    -------
        Tuple of plane parameters ``(normal, offset)`` if a valid plane is found, otherwise ``None``.
    """
    if points.shape[0] < 3:
        return None

    # Randomly sample N triplets of points and compute their normals (cross product) for RANSAC vectorized plane fitting
    triplets = points[rng.integers(0, points.shape[0], size=(iterations, 3))]
    normals = np.cross(triplets[:, 1] - triplets[:, 0], triplets[:, 2] - triplets[:, 0])
    lengths = np.linalg.norm(normals, axis=1)

    # Discard degenerate triplets 
    valid = lengths > 1e-9
    if not np.any(valid):
        return None

    # Normalize the valid normals to unit length (meters).
    normals = normals[valid] / lengths[valid, None]

    # Get the offsets for the candidate planes based on the sampled points.
    offsets = -np.einsum('ij,ij->i', normals, triplets[valid, 0])

    # Count the number of inliers for each candidate plane.
    inlier_counts = (np.abs(points @ normals.T + offsets) < threshold).sum(axis=0)
    best = int(np.argmax(inlier_counts))
    normal, offset = normals[best], offsets[best]

    # Least-squares refinement over the consensus set.
    inliers = points[np.abs(points @ normal + offset) < threshold]
    if inliers.shape[0] >= 3:
        centroid = inliers.mean(axis=0)
        _, _, vh = np.linalg.svd(inliers - centroid, full_matrices=False)
        normal = vh[-1]
        offset = float(-normal @ centroid)

    # Ensure the plane normal is oriented towards the camera.
    if offset < 0.0:
        normal, offset = -normal, -offset
    return normal, offset


def guided_filter(guide, source, radius, eps):
    """
    O(1) colour guided filter (He et al., TPAMI 2013, eq. 14); hand-rolled because
    cv.ximgproc is not in this build. ``guide`` is ``(H, W, 3)`` float32, ``source`` ``(H, W)``.
    """
    ksize = (2 * radius + 1, 2 * radius + 1)
    box = lambda x: cv.boxFilter(x, -1, ksize)
    r, g, b = cv.split(guide)
    mr, mg, mb = box(r), box(g), box(b)
    mean_s = box(source)

    cr = box(r * source) - mr * mean_s
    cg = box(g * source) - mg * mean_s
    cb = box(b * source) - mb * mean_s

    # Per-pixel 3x3 covariance of the guide, regularised by eps on the diagonal.
    srr = box(r * r) - mr * mr + eps
    srg = box(r * g) - mr * mg
    srb = box(r * b) - mr * mb
    sgg = box(g * g) - mg * mg + eps
    sgb = box(g * b) - mg * mb
    sbb = box(b * b) - mb * mb + eps

    # Closed-form inverse of the symmetric 3x3 via its adjugate.
    irr = sgg * sbb - sgb * sgb
    irg = sgb * srb - srg * sbb
    irb = srg * sgb - sgg * srb
    igg = srr * sbb - srb * srb
    igb = srb * srg - srr * sgb
    ibb = srr * sgg - srg * srg
    det = srr * irr + srg * irg + srb * irb

    ar = (irr * cr + irg * cg + irb * cb) / det
    ag = (irg * cr + igg * cg + igb * cb) / det
    ab = (irb * cr + igb * cg + ibb * cb) / det
    bias = mean_s - ar * mr - ag * mg - ab * mb
    return box(ar) * r + box(ag) * g + box(ab) * b + box(bias)


def snap_to_colour(mask, colour, guide_radius, guide_eps):
    """
    Let the mask inherit the colour image's edges.

    Aligned depth is interpolated and its borders are soft, so the geometric mask cuts a
    pixel or two inside the real outline; the colour image has that outline sharp.
    """
    if guide_radius <= 0:
        return mask
    guide = colour.astype(np.float32) / 255.0
    soft = guided_filter(guide, mask.astype(np.float32) / 255.0,
                            guide_radius, guide_eps)
    return ((soft > 0.5) * 255).astype(np.uint8)

    
def label_blobs(min_blob_area, mask):
        """
        Drop connected components smaller than ``min_blob_area``; returns the cleaned
        mask together with the per-pixel instance labels.

        Parameters:
        ----------
        - min_blob_area : Minimum area for a connected component to be kept.
        - mask : Binary mask image where connected components are to be labeled.

        Returns:
        -------
        Tuple: (cleaned mask, per-pixel instance labels).
        """
        count, labels, stats, _ = cv.connectedComponentsWithStats(mask, connectivity=8)
        keep = np.zeros(count, dtype=np.int32)
        keep[1:] = np.where(stats[1:, cv.CC_STAT_AREA] >= min_blob_area, np.arange(1, count), 0)
        labels = keep[labels]
        return (labels > 0).astype(np.uint8) * 255, labels