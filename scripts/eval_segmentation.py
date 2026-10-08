"""
Offline benchmark of the background-removal / instance-segmentation front-ends on GraspNet-1Billion.

Each pipeline is re-implemented here as a ROS-free copy of its node's `image_callback`
(irs_bgrem_v1_1, u2net_bgrm, fastsam_bgrem) with the node defaults, so the nodes stay untouched.
If a node changes, mirror the change in the matching segmenter below.

Metrics (per frame, then aggregated per model):
    - Foreground: IoU, precision, recall, F-beta (beta^2 = 0.3), MAE, boundary F.
    - Instances (UOIS-Net protocol): overlap P/R/F, boundary P/R/F, %objects F>=0.75,
      under/over-segmentation counts, PQ/SQ/RQ.
    - Point cloud vs. GT visible cloud: purity, outlier ratio, one-sided Chamfer, completeness,
      SuperDec normalisation-scale error, points per instance.
    - Table plane: angle and offset error against cam0_wrt_table @ camera_poses.
    - Efficiency: latency, peak VRAM, parameter count.
    - Optional (--superdec): radial distance of GT / predicted clouds to the superquadrics.

Run (ROS sourced for sensor_msgs, plus the venv):
    python scripts/eval_segmentation.py
    python scripts/eval_segmentation.py --models oracle --max-frames 8
    python scripts/eval_segmentation.py --superdec --superdec-every 4
"""
import argparse
import gc
import os
import sys
import time
from pathlib import Path

import cv2 as cv
import numpy as np
import pandas as pd
import torch
from scipy.optimize import linear_sum_assignment
from scipy.spatial import cKDTree

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'src' / 'intel_realsense'))

from intel_realsense.bgrem_utils import (  # noqa: E402
    fit_plane_ransac, label_blobs, reject_flying_pixels, snap_to_colour)

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
N_SUPERDEC_POINTS = 4096
K3 = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))

CORE_COST = ['params_m', 'vram_peak_mb', 'fps', 'latency_p95_ms']
CORE_METRICS = ['fg_iou', 'fg_mae', 'inst_overlap_f', 'inst_f075', 'inst_under_seg',
                'pc_outlier_ratio', 'pc_completeness', 'sd_radial_gt_mm', 'sd_flatness']


# ============================================================================================
# Segmenters: ROS-free copies of the nodes. Each returns labels (published mask ids),
# cloud_labels (ids that become points), in_volume, points (row-major over in_volume), plane.
# ============================================================================================

class GeometricSegmenter:
    """Mirror of irs_bgrem_v1_1.IRSNode.image_callback."""
    params = dict(min_depth=0.20, max_depth=1.20, plane_threshold=0.01, plane_clearance=0.01,
                  edge_threshold=0.001, edge_reference=0.1, cloud_erode=0, guide_radius=16,
                  guide_eps=1e-2, ransac_iterations=128, ransac_samples=4000,
                  min_blob_area=300, freeze_plane=False)

    def __init__(self):
        self.kernel = cv.getStructuringElement(cv.MORPH_RECT, (5, 5))
        self.edge_kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
        self.reset()

    def reset(self):
        self.rng = np.random.default_rng(0)
        self.plane = None

    def __call__(self, frame):
        p = self.params
        color, depth, rays = frame['bgr'], frame['depth'], frame['rays']
        in_volume = reject_flying_pixels(depth, self.edge_kernel, p['edge_threshold'],
                                         p['edge_reference'], p['min_depth'], p['max_depth'])
        if np.count_nonzero(in_volume) < 3:
            in_volume[:] = False

        mask = np.zeros(depth.shape, dtype=np.uint8)
        labels = np.zeros(depth.shape, dtype=np.int32)
        points = np.empty((0, 3))
        above = np.zeros(depth.shape, dtype=bool)

        if in_volume.any():
            points = back_project(depth, rays, in_volume)
            if self.plane is None or not p['freeze_plane']:
                sample = points
                if points.shape[0] > p['ransac_samples']:
                    sample = points[self.rng.choice(points.shape[0], p['ransac_samples'], replace=False)]
                self.plane = fit_plane_ransac(sample, p['plane_threshold'],
                                              p['ransac_iterations'], self.rng) or self.plane

            if self.plane is None:
                above = in_volume.copy()
            else:
                normal, offset = self.plane
                above[in_volume] = (points @ normal + offset) > p['plane_clearance']
            mask[above] = 255

            mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, self.kernel)
            mask = snap_to_colour(mask, color, p['guide_radius'], p['guide_eps'])
            mask, labels = label_blobs(p['min_blob_area'], mask)

        cloud_labels = np.where(above, labels, 0)
        if p['cloud_erode'] > 0:
            core = cv.erode(mask, self.edge_kernel, iterations=p['cloud_erode'])
            cloud_labels[core == 0] = 0
        return dict(labels=labels, cloud_labels=cloud_labels, in_volume=in_volume,
                    points=points, plane=self.plane)

    def parameter_count(self):
        return 0


class U2NetSegmenter:
    """Mirror of u2net_bgrm.U2NetBGRM.image_callback."""
    params = dict(inference_size=320, mask_threshold=0.5, min_depth=0.20, max_depth=1.20,
                  plane_threshold=0.010, plane_clearance=0.010, ransac_iterations=128,
                  ransac_samples=4000, min_blob_area=300, freeze_plane=False, amp=True)

    def __init__(self, variant, weights, device):
        from intel_realsense.u2net import U2Net, U2NetP

        self.device = device
        self.kernel = cv.getStructuringElement(cv.MORPH_ELLIPSE, (5, 5))
        self.mean = torch.tensor(IMAGENET_MEAN, device=device).view(3, 1, 1)
        self.std = torch.tensor(IMAGENET_STD, device=device).view(3, 1, 1)

        if not os.path.isfile(weights):
            raise FileNotFoundError(f'Weights not found: {weights}')
        self.model = U2Net() if variant == 'u2net' else U2NetP()
        checkpoint = torch.load(weights, map_location='cpu')
        self.model.load_state_dict(checkpoint.get('model', checkpoint))
        self.model.to(device).eval()
        self.reset()

    def reset(self):
        self.rng = np.random.default_rng(0)
        self.plane = None

    def infer_mask(self, rgb):
        size = self.params['inference_size']
        height, width = rgb.shape[:2]
        resized = cv.resize(rgb, (size, size), interpolation=cv.INTER_LINEAR)
        tensor = torch.from_numpy(np.ascontiguousarray(resized)).to(self.device)
        tensor = tensor.permute(2, 0, 1).float().div_(255.0).sub_(self.mean).div_(self.std).unsqueeze(0)

        use_amp = self.params['amp'] and self.device.type == 'cuda'
        with torch.inference_mode(), torch.autocast(self.device.type, dtype=torch.bfloat16,
                                                    enabled=use_amp):
            probability = torch.sigmoid(self.model(tensor)[0])
        probability = probability[0, 0].float().cpu().numpy()
        probability = cv.resize(probability, (width, height), interpolation=cv.INTER_LINEAR)
        return ((probability > self.params['mask_threshold']) * 255).astype(np.uint8)

    def update_plane(self, points, is_background):
        p = self.params
        if self.plane is not None and p['freeze_plane']:
            return
        candidates = points[is_background] if np.count_nonzero(is_background) >= 3 else points
        if candidates.shape[0] > p['ransac_samples']:
            candidates = candidates[self.rng.choice(candidates.shape[0], p['ransac_samples'],
                                                    replace=False)]
        self.plane = fit_plane_ransac(candidates, p['plane_threshold'],
                                      p['ransac_iterations'], self.rng) or self.plane

    def __call__(self, frame):
        p = self.params
        depth, rays = frame['depth'], frame['rays']
        rgb = cv.cvtColor(frame['bgr'], cv.COLOR_BGR2RGB)
        mask = self.infer_mask(rgb)

        in_volume = (depth > p['min_depth']) & (depth < p['max_depth'])
        points = back_project(depth, rays, in_volume)
        self.update_plane(points, mask[in_volume] == 0)

        mask = cv.morphologyEx(mask, cv.MORPH_CLOSE, self.kernel)
        mask[~in_volume] = 0
        if self.plane is not None and p['plane_clearance'] > 0.0:
            normal, offset = self.plane
            above = np.zeros(mask.shape, dtype=bool)
            above[in_volume] = (points @ normal + offset) > p['plane_clearance']
            mask[~above] = 0

        mask, labels = label_blobs(p['min_blob_area'], mask)
        return dict(labels=labels, cloud_labels=labels, in_volume=in_volume,
                    points=points, plane=self.plane)

    def parameter_count(self):
        return sum(t.numel() for t in self.model.parameters())


class FastSAMSegmenter:
    """Mirror of fastsam_bgrem.FastSAMNode.image_callback (empty prompt: segment everything)."""
    params = dict(img_size=640, conf=0.7, iou=0.85, min_depth=0.20, max_depth=1.20,
                  plane_threshold=0.01, plane_clearance=0.01, edge_threshold=0.001,
                  edge_reference=0.1, ransac_iterations=128, ransac_samples=4000,
                  min_blob_area=300, freeze_plane=False, max_mask_ratio=0.3, min_above_ratio=0.5)

    def __init__(self, weights, device):
        from ultralytics import FastSAM

        if not os.path.isfile(weights):
            raise FileNotFoundError(f'Weights not found: {weights}')
        self.model = FastSAM(weights)
        self.device = device
        self.edge_kernel = cv.getStructuringElement(cv.MORPH_RECT, (3, 3))
        self.reset()

    def reset(self):
        self.rng = np.random.default_rng(0)
        self.plane = None

    def fit_table(self, depth, rays):
        p = self.params
        in_volume = reject_flying_pixels(depth, self.edge_kernel, p['edge_threshold'],
                                         p['edge_reference'], p['min_depth'], p['max_depth'])
        above = np.zeros(depth.shape, dtype=bool)
        if np.count_nonzero(in_volume) < 3:
            return np.zeros_like(in_volume), above, np.empty((0, 3), np.float32)

        points = back_project(depth, rays, in_volume)
        if self.plane is None or not p['freeze_plane']:
            sample = points
            if points.shape[0] > p['ransac_samples']:
                sample = points[self.rng.choice(points.shape[0], p['ransac_samples'], replace=False)]
            self.plane = fit_plane_ransac(sample, p['plane_threshold'],
                                          p['ransac_iterations'], self.rng) or self.plane

        if self.plane is None:
            above[in_volume] = True
        else:
            normal, offset = self.plane
            above[in_volume] = (points @ normal + offset) > p['plane_clearance']
        return in_volume, above, points

    @staticmethod
    def full_masks(result):
        from ultralytics.utils.ops import scale_masks

        if result.masks is None or len(result) == 0:
            return torch.zeros((0, *result.orig_shape), dtype=torch.bool)
        masks = result.masks.data
        if masks.shape[1:] != result.orig_shape:
            masks = scale_masks(masks[None].float(), result.orig_shape)[0]
        return masks > 0.5

    def on_table(self, masks, in_volume, above):
        p = self.params
        h, w = in_volume.shape
        above_t = torch.from_numpy(above).to(masks.device)
        valid_t = torch.from_numpy(in_volume).to(masks.device)
        area = masks.sum((1, 2))
        raised = (masks & above_t).sum((1, 2))
        valid = (masks & valid_t).sum((1, 2))
        return ((area < p['max_mask_ratio'] * h * w)
                & (raised >= p['min_blob_area'])
                & (raised > p['min_above_ratio'] * valid))

    def __call__(self, frame):
        p = self.params
        color, depth = frame['bgr'], frame['depth']
        in_volume, above, points = self.fit_table(depth, frame['rays'])

        result = self.model.predict(color, device=self.device, conf=p['conf'], iou=p['iou'],
                                    verbose=False, imgsz=p['img_size'])[0]
        masks = self.full_masks(result)
        if len(masks):
            masks = masks[self.on_table(masks, in_volume, above)]

        masks = masks.cpu().numpy()
        labels = np.zeros(depth.shape, dtype=np.int32)
        for i in np.argsort(-masks.sum((1, 2))):
            labels[masks[i]] = i + 1
        labels[~above] = 0

        ids, counts = np.unique(labels, return_counts=True)
        labels[np.isin(labels, ids[counts < p['min_blob_area']])] = 0
        return dict(labels=labels, cloud_labels=labels, in_volume=in_volume,
                    points=points, plane=self.plane)

    def parameter_count(self):
        return sum(t.numel() for t in self.model.model.parameters())


class OracleSegmenter:
    """Returns the ground truth; every metric must come out perfect. Sanity check of this script."""
    params = dict(min_depth=0.20, max_depth=1.20)

    def reset(self):
        pass

    def __call__(self, frame):
        depth = frame['depth']
        in_volume = (depth > self.params['min_depth']) & (depth < self.params['max_depth'])
        return dict(labels=frame['gt'], cloud_labels=frame['gt'], in_volume=in_volume,
                    points=back_project(depth, frame['rays'], in_volume), plane=frame['gt_plane'])

    def parameter_count(self):
        return 0


MODELS = {
    'geometric': ('irs_bgrem_v1_1', lambda dev: GeometricSegmenter()),
    'u2net-v2': ('runs/u2net/v2/best.pth',
                 lambda dev: U2NetSegmenter('u2net', str(REPO_ROOT / 'runs/u2net/v2/best.pth'), dev)),
    'u2netp-v1': ('runs/u2netp/v1/best.pth',
                  lambda dev: U2NetSegmenter('u2netp', str(REPO_ROOT / 'runs/u2netp/v1/best.pth'), dev)),
    'fastsam-s': ('models/FastSAM-s.pt',
                  lambda dev: FastSAMSegmenter(str(REPO_ROOT / 'models/FastSAM-s.pt'), dev)),
    'fastsam-x': ('models/FastSAM-x.pt',
                  lambda dev: FastSAMSegmenter(str(REPO_ROOT / 'models/FastSAM-x.pt'), dev)),
    'oracle': ('ground truth', lambda dev: OracleSegmenter()),
}


# ============================================================================================
# Data
# ============================================================================================

def back_project(depth, rays, mask):
    z = depth[mask]
    r = rays[mask]
    return np.column_stack((r[:, 0] * z, r[:, 1] * z, z))


def make_rays(K, width, height):
    u, v = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    return np.dstack(((u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1]))


def parse_scenes(text):
    scenes = []
    for part in text.split(','):
        lo, _, hi = part.partition('-')
        scenes.extend(range(int(lo), int(hi or lo) + 1))
    return scenes


def find_scene(root, scene, camera):
    for chunk in sorted(Path(root).glob('*')):
        folder = chunk / f'scene_{scene:04d}' / camera
        if folder.is_dir():
            return folder
    raise FileNotFoundError(f'scene_{scene:04d}/{camera} not found under {root}')


def crop_resize(image, size, interpolation):
    """Centre-crop to the target aspect ratio, then resize; returns the image and (x0, scale)."""
    width, height = size
    h, w = image.shape[:2]
    crop_w = min(w, round(h * width / height))
    crop_h = min(h, round(w * height / width))
    x0, y0 = (w - crop_w) // 2, (h - crop_h) // 2
    image = image[y0:y0 + crop_h, x0:x0 + crop_w]
    return cv.resize(image, (width, height), interpolation=interpolation), (x0, y0, width / crop_w)


def iter_frames(args):
    """Yields (scene, view, frame dict) in scene order, resampled to --size."""
    size = tuple(int(v) for v in args.size.lower().split('x'))
    for scene in parse_scenes(args.scenes):
        folder = find_scene(args.dataset, scene, args.camera)
        K = np.load(folder / 'camK.npy')
        cam0_wrt_table = np.load(folder / 'cam0_wrt_table.npy')
        poses = np.load(folder / 'camera_poses.npy')
        rays = None

        for view in range(0, poses.shape[0], args.stride):
            bgr = cv.imread(str(folder / 'rgb' / f'{view:04d}.png'), cv.IMREAD_COLOR)
            raw = cv.imread(str(folder / 'depth' / f'{view:04d}.png'), cv.IMREAD_UNCHANGED)
            gt = cv.imread(str(folder / 'label' / f'{view:04d}.png'), cv.IMREAD_UNCHANGED)

            bgr, (x0, y0, scale) = crop_resize(bgr, size, cv.INTER_AREA)
            raw, _ = crop_resize(raw, size, cv.INTER_NEAREST)
            gt, _ = crop_resize(gt, size, cv.INTER_NEAREST)
            gt = gt.astype(np.int32)
            if args.min_gt_area > 0:
                ids, counts = np.unique(gt, return_counts=True)
                gt[np.isin(gt, ids[(ids > 0) & (counts < args.min_gt_area)])] = 0

            if rays is None:
                Ks = K.astype(np.float64).copy()
                Ks[0, 2] -= x0
                Ks[1, 2] -= y0
                Ks[:2] *= scale
                rays = make_rays(Ks, *size)

            # Same conversion as bgrem_utils.to_meters, applied at the node's input resolution.
            depth = cv.medianBlur(raw, 5).astype(np.float32) * 1e-3

            # Table frame has z up; its z=0 plane expressed in camera coordinates.
            T = cam0_wrt_table @ poses[view]
            normal, offset = T[2, :3].copy(), float(T[2, 3])
            if offset < 0.0:
                normal, offset = -normal, -offset

            yield scene, view, dict(bgr=bgr, depth=depth, rays=rays, gt=gt,
                                    gt_plane=(normal, offset))


# ============================================================================================
# Metrics
# ============================================================================================

def safe_div(a, b):
    return float(a) / float(b) if b > 0 else float('nan')


def f_score(p, r):
    return 2 * p * r / (p + r) if (p + r) > 0 else 0.0


def contingency(pred, gt):
    """Intersection counts between every predicted and GT id (background excluded)."""
    p_ids = np.unique(pred[pred > 0])
    g_ids = np.unique(gt[gt > 0])
    pmap = np.zeros(int(pred.max()) + 1, np.int64)
    gmap = np.zeros(int(gt.max()) + 1, np.int64)
    pmap[p_ids] = np.arange(1, len(p_ids) + 1)
    gmap[g_ids] = np.arange(1, len(g_ids) + 1)
    joint = pmap[pred] * (len(g_ids) + 1) + gmap[gt]
    counts = np.bincount(joint.ravel(), minlength=(len(p_ids) + 1) * (len(g_ids) + 1))
    counts = counts.reshape(len(p_ids) + 1, len(g_ids) + 1)
    return p_ids, g_ids, counts[1:, 1:], counts[1:].sum(1), counts[:, 1:].sum(0)


def boundary_map(labels):
    """Each pixel on the border of its own segment keeps its id, everything else is 0."""
    as_float = labels.astype(np.float32)
    edge = (cv.dilate(as_float, K3) != as_float) | (cv.erode(as_float, K3) != as_float)
    return np.where(edge & (labels > 0), labels, 0)


def boundary_pairs(pred, gt, p_ids, g_ids, tol):
    """
    For every (pred i, gt j): |B_i within tol of B_j| and |B_j within tol of B_i|,
    plus the boundary lengths. Follows the boundary metric of UOIS-Net (Xie et al.).
    """
    disk = cv.getStructuringElement(cv.MORPH_ELLIPSE, (2 * tol + 1, 2 * tol + 1))
    pb, gb = boundary_map(pred), boundary_map(gt)

    def stack(bmap, ids):
        if len(ids) == 0:
            return np.zeros((0, *bmap.shape), bool)
        return np.stack([cv.dilate((bmap == i).astype(np.uint8), disk) > 0 for i in ids])

    p_dil, g_dil = stack(pb, p_ids), stack(gb, g_ids)
    hit_p = np.zeros((len(p_ids), len(g_ids)))
    hit_g = np.zeros((len(p_ids), len(g_ids)))
    n_pb = np.zeros(len(p_ids))
    n_gb = np.zeros(len(g_ids))
    for i, pid in enumerate(p_ids):
        ys, xs = np.nonzero(pb == pid)
        n_pb[i] = len(ys)
        hit_p[i] = g_dil[:, ys, xs].sum(1) if len(g_ids) else 0
    for j, gid in enumerate(g_ids):
        ys, xs = np.nonzero(gb == gid)
        n_gb[j] = len(ys)
        hit_g[:, j] = p_dil[:, ys, xs].sum(1) if len(p_ids) else 0
    return hit_p, hit_g, n_pb, n_gb


def foreground_metrics(pred_fg, gt_fg, tol):
    tp = np.count_nonzero(pred_fg & gt_fg)
    n_pred, n_gt = np.count_nonzero(pred_fg), np.count_nonzero(gt_fg)
    precision = safe_div(tp, n_pred) if n_pred else 0.0
    recall = safe_div(tp, n_gt)
    beta2 = 0.3
    fbeta = ((1 + beta2) * precision * recall / (beta2 * precision + recall)
             if (precision + recall) > 0 else 0.0)

    hit_p, hit_g, n_pb, n_gb = boundary_pairs(pred_fg.astype(np.int32), gt_fg.astype(np.int32),
                                              np.array([1]) if n_pred else np.array([], int),
                                              np.array([1]) if n_gt else np.array([], int), tol)
    bp = safe_div(hit_p.sum(), n_pb.sum()) if n_pb.sum() else 0.0
    br = safe_div(hit_g.sum(), n_gb.sum())
    return dict(fg_iou=safe_div(tp, n_pred + n_gt - tp), fg_precision=precision, fg_recall=recall,
                fg_fbeta=fbeta, fg_mae=float(np.mean(pred_fg != gt_fg)),
                fg_boundary_f=f_score(bp, br))


def instance_metrics(pred, gt, tol):
    """Returns the metrics plus the overlap matching [(pred_id, gt_id)] used for the 3D metrics."""
    p_ids, g_ids, inter, area_p, area_g = contingency(pred, gt)
    P, G = len(p_ids), len(g_ids)
    out = dict(n_gt=G, n_pred=P)
    matches = []

    if P and G:
        f_matrix = 2 * inter / (area_p[:, None] + area_g[None, :])
        rows, cols = linear_sum_assignment(f_matrix, maximize=True)
        keep = f_matrix[rows, cols] > 0
        rows, cols = rows[keep], cols[keep]
        matches = [(int(p_ids[r]), int(g_ids[c])) for r, c in zip(rows, cols)]
        tp = inter[rows, cols].sum()
        op, orr = tp / area_p.sum(), tp / area_g.sum()
        out['inst_f075'] = float(np.sum(f_matrix[rows, cols] >= 0.75)) / G

        hit_p, hit_g, n_pb, n_gb = boundary_pairs(pred, gt, p_ids, g_ids, tol)
        bp_m = hit_p / np.maximum(n_pb[:, None], 1)
        br_m = hit_g / np.maximum(n_gb[None, :], 1)
        bf_matrix = np.where(bp_m + br_m > 0, 2 * bp_m * br_m / np.maximum(bp_m + br_m, 1e-12), 0)
        rows_b, cols_b = linear_sum_assignment(bf_matrix, maximize=True)
        bp = safe_div(hit_p[rows_b, cols_b].sum(), n_pb.sum())
        br = safe_div(hit_g[rows_b, cols_b].sum(), n_gb.sum())

        iou = inter / (area_p[:, None] + area_g[None, :] - inter)
        tp_pq = iou > 0.5
        n_tp = int(tp_pq.sum())
        sq = float(iou[tp_pq].mean()) if n_tp else float('nan')
        rq = n_tp / (n_tp + 0.5 * (P - n_tp) + 0.5 * (G - n_tp))
        out.update(sq=sq, rq=rq, pq=(sq * rq) if n_tp else 0.0)

        # A prediction swallowing half of two objects, or an object split into mostly-inside pieces.
        out['inst_under_seg'] = int(np.sum((inter / area_g[None, :] >= 0.5).sum(1) >= 2))
        out['inst_over_seg'] = int(np.sum((inter / area_p[:, None] >= 0.5).sum(0) >= 2))
    else:
        op = orr = bp = br = 0.0
        out.update(inst_f075=0.0, sq=float('nan'), rq=0.0, pq=0.0,
                   inst_under_seg=0, inst_over_seg=0)
        if not G:
            op = orr = bp = br = float('nan')

    out.update(inst_overlap_p=op, inst_overlap_r=orr, inst_overlap_f=f_score(op, orr),
               inst_boundary_p=bp, inst_boundary_r=br, inst_boundary_f=f_score(bp, br))
    return out, matches


def superdec_scale(points):
    """The normalisation scale SuperDec uses (superdec_utils.normalize_points)."""
    return 2.0 * np.max(np.abs(points - points.mean(0)))


def gt_cloud(frame, gid, args):
    """Visible GT surface: sensor depth under the eroded label, so label borders do not leak."""
    depth = frame['depth']
    valid = (depth > args.min_depth) & (depth < args.max_depth)
    obj = (frame['gt'] == gid).astype(np.uint8)
    core = cv.erode(obj, K3, iterations=2) > 0
    region = (core if np.count_nonzero(core & valid) >= 3 else obj > 0) & valid
    return back_project(depth, frame['rays'], region)


def cloud_metrics(frame, seg, matches, args):
    """Point-level quality of the published clouds of the matched instances; also returns them."""
    rows = []
    clouds = []
    ids_in_volume = seg['cloud_labels'][seg['in_volume']]
    gt_in_volume = frame['gt'][seg['in_volume']]
    for pid, gid in matches:
        select = ids_in_volume == pid
        pred_pts = seg['points'][select]
        ref = gt_cloud(frame, gid, args)
        if pred_pts.shape[0] < 3 or ref.shape[0] < 3:
            continue

        to_ref, _ = cKDTree(ref).query(pred_pts)
        to_pred, _ = cKDTree(pred_pts).query(ref)
        s_ref = superdec_scale(ref)
        rows.append(dict(
            pc_purity=float(np.mean(gt_in_volume[select] == gid)),
            pc_outlier_ratio=float(np.mean(to_ref > args.dist_tol)),
            pc_chamfer_mm=float(to_ref.mean() * 1e3),
            pc_chamfer_p95_mm=float(np.percentile(to_ref, 95) * 1e3),
            pc_completeness=float(np.mean(to_pred <= args.dist_tol)),
            pc_scale_err=float(abs(superdec_scale(pred_pts) - s_ref) / max(s_ref, 1e-9)),
            pc_n_points=float(pred_pts.shape[0]),
            pc_ge4096=float(pred_pts.shape[0] >= N_SUPERDEC_POINTS)))
        clouds.append((pred_pts, ref))

    keys = ['pc_purity', 'pc_outlier_ratio', 'pc_chamfer_mm', 'pc_chamfer_p95_mm',
            'pc_completeness', 'pc_scale_err', 'pc_n_points', 'pc_ge4096']
    out = {k: float(np.mean([r[k] for r in rows])) if rows else float('nan') for k in keys}
    return out, clouds


def plane_metrics(plane, gt_plane):
    if plane is None:
        return dict(plane_angle_deg=float('nan'), plane_offset_mm=float('nan'))
    normal, offset = plane
    gt_normal, gt_offset = gt_plane
    cosine = abs(float(np.dot(normal, gt_normal))) / np.linalg.norm(normal)
    return dict(plane_angle_deg=float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0)))),
                plane_offset_mm=abs(float(offset) - gt_offset) * 1e3)


def radial_error(points, primitives):
    """Mean Solina-Bajcsy radial distance from each point to its closest primitive, in mm."""
    from intel_realsense.superdec_utils import radial_distance

    if len(primitives['scale']) == 0 or points.shape[0] == 0:
        return float('nan')
    pts = torch.from_numpy(points).double()
    scale = torch.from_numpy(primitives['scale']).double()
    shape = torch.from_numpy(primitives['shape']).double()
    rot = torch.from_numpy(primitives['rotate']).double()
    trans = torch.from_numpy(primitives['trans']).double()

    delta = pts[:, None, :] - trans[None]
    local = torch.einsum('pji,npj->npi', rot, delta)
    distance = radial_distance(local, scale[None], shape[None, :, 0], shape[None, :, 1])
    return float(distance.amin(dim=1).mean() * 1e3)


def superdec_metrics(runner, clouds, plane, device):
    clouds = [(p, r) for p, r in clouds if p.shape[0] >= 32]
    if not clouds:
        return {}
    sync(device)
    started = time.perf_counter()
    primitives = runner([p for p, _ in clouds], plane=plane)
    sync(device)
    elapsed = time.perf_counter() - started

    gt_err, pred_err, counts, flatness = [], [], [], []
    for (pred_pts, ref), prim in zip(clouds, primitives):
        gt_err.append(radial_error(ref, prim))
        pred_err.append(radial_error(pred_pts, prim))
        counts.append(len(prim['scale']))
        if len(prim['scale']):
            edges = np.sort(prim['scale'], axis=1)
            flatness.append(float(np.mean(edges[:, 0] / edges[:, 2])))
    return dict(sd_radial_gt_mm=float(np.nanmean(gt_err)), sd_radial_pred_mm=float(np.nanmean(pred_err)),
                sd_n_sq=float(np.mean(counts)),
                sd_flatness=float(np.mean(flatness)) if flatness else float('nan'),
                sd_latency_ms=elapsed * 1e3)


# ============================================================================================
# Driver
# ============================================================================================

def sync(device):
    if device.type == 'cuda':
        torch.cuda.synchronize(device)


def evaluate_model(name, args, device, runner):
    weights, factory = MODELS[name]
    if device.type == 'cuda':
        torch.cuda.empty_cache()
        baseline = torch.cuda.memory_allocated(device)
        torch.cuda.reset_peak_memory_stats(device)

    segmenter = factory(device)
    frames = iter_frames(args)
    first = next(iter_frames(args))[2]
    for _ in range(args.warmup):
        segmenter(first)
    segmenter.reset()

    rows, current_scene, count = [], None, 0
    for scene, view, frame in frames:
        if args.max_frames and count >= args.max_frames:
            break
        if scene != current_scene:
            segmenter.reset()
            current_scene = scene
            print(f'  [{name}] scene {scene:04d}', flush=True)

        sync(device)
        started = time.perf_counter()
        seg = segmenter(frame)
        sync(device)
        latency = (time.perf_counter() - started) * 1e3

        row = dict(model=name, scene=scene, view=view)
        row.update(foreground_metrics(seg['labels'] > 0, frame['gt'] > 0, args.boundary_tol))
        inst, matches = instance_metrics(seg['labels'], frame['gt'], args.boundary_tol)
        row.update(inst)
        cloud, clouds = cloud_metrics(frame, seg, matches, args)
        row.update(cloud)
        row.update(plane_metrics(seg['plane'], frame['gt_plane']))
        row['latency_ms'] = latency
        if runner is not None and count % args.superdec_every == 0:
            row.update(superdec_metrics(runner, clouds, seg['plane'], device))
        rows.append(row)
        count += 1

    info = dict(model=name, weights=weights, params_m=segmenter.parameter_count() / 1e6,
                vram_peak_mb=float('nan'))
    if device.type == 'cuda':
        info['vram_peak_mb'] = (torch.cuda.max_memory_allocated(device) - baseline) / 2**20

    del segmenter
    gc.collect()
    return rows, info


def bootstrap_ci(values, rng, n=1000):
    if len(values) < 2:
        return float('nan'), float('nan')
    means = values[rng.integers(0, len(values), (n, len(values)))].mean(1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarise(frames, info, args):
    """One row per model: mean, std and bootstrap 95% CI of every per-frame metric."""
    rng = np.random.default_rng(0)
    latency = frames['latency_ms']
    summary = dict(info, n_frames=len(frames), size=args.size, scenes=args.scenes, stride=args.stride,
                   latency_p50_ms=latency.quantile(0.50), latency_p95_ms=latency.quantile(0.95),
                   fps=1e3 / latency.mean())
    metrics = frames.drop(columns=['model', 'scene', 'view']).apply(pd.to_numeric, errors='coerce')
    for key, column in metrics.items():
        values = column.dropna().to_numpy(np.float64)
        summary[key] = values.mean() if len(values) else np.nan
        summary[f'{key}_std'] = values.std(ddof=1) if len(values) > 1 else np.nan
        summary[f'{key}_ci_lo'], summary[f'{key}_ci_hi'] = bootstrap_ci(values, rng)
    return summary


def write_csv(path, frame):
    frame.to_csv(path, index=False, float_format='%.6g', na_rep='')


def core_summary(summary):
    """Reduced table for the thesis: cost columns plus mean and std of the core metrics."""
    columns = ['model', *CORE_COST]
    for key in CORE_METRICS:
        columns += [key, f'{key}_std']
    return summary[[c for c in columns if c in summary.columns]]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--models', default='geometric,u2net-v2,u2netp-v1,fastsam-s,fastsam-x',
                        help=f'comma-separated subset of {list(MODELS)}')
    parser.add_argument('--dataset', default=str(REPO_ROOT / 'datasets' / 'GraspNet-1Billion'))
    parser.add_argument('--camera', default='realsense', choices=['realsense', 'kinect'])
    parser.add_argument('--scenes', default='90-99', help='e.g. "90-99" or "90,92,95-97"')
    parser.add_argument('--stride', type=int, default=8, help='keep one view every N (256 per scene)')
    parser.add_argument('--size', default='640x480', help='evaluation resolution WxH')
    parser.add_argument('--device', default='')
    parser.add_argument('--warmup', type=int, default=5)
    parser.add_argument('--max-frames', type=int, default=0, help='per model, 0 = all')
    parser.add_argument('--min-gt-area', type=int, default=0,
                        help='GT objects smaller than this (px) are treated as background')
    parser.add_argument('--boundary-tol', type=int, default=2, help='boundary tolerance in px')
    parser.add_argument('--dist-tol', type=float, default=0.005, help='3D inlier distance in m')
    parser.add_argument('--min-depth', type=float, default=0.20, help='range of the GT reference cloud')
    parser.add_argument('--max-depth', type=float, default=1.20)
    parser.add_argument('--superdec', action='store_true', help='also run SuperDec on the clouds')
    parser.add_argument('--superdec-every', type=int, default=4, help='SuperDec on one frame every N')
    parser.add_argument('--checkpoint-dir', default=str(REPO_ROOT / 'superdec/checkpoints/normalized'))
    parser.add_argument('--out', default=str(REPO_ROOT / 'doc' / 'metrics'))
    args = parser.parse_args()

    names = [n.strip() for n in args.models.split(',') if n.strip()]
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        parser.error(f'unknown models {unknown}; choose from {list(MODELS)}')

    device = torch.device(args.device or ('cuda' if torch.cuda.is_available() else 'cpu'))
    runner = None
    if args.superdec:
        from intel_realsense.superdec_utils import SuperDecRunner
        runner = SuperDecRunner(str(REPO_ROOT / 'superdec'), args.checkpoint_dir, device=str(device))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    frames, summaries = [], []
    for name in names:
        print(f'Evaluating {name} on {device}', flush=True)
        rows, info = evaluate_model(name, args, device, runner)
        model_frames = pd.DataFrame(rows)
        frames.append(model_frames)
        summaries.append(summarise(model_frames, info, args))

        # Written after every model so a crash later on keeps what is already measured.
        write_csv(out_dir / 'segmentation_frames.csv', pd.concat(frames, ignore_index=True))
        summary = pd.DataFrame(summaries)
        write_csv(out_dir / 'segmentation_summary.csv', summary)
        write_csv(out_dir / 'segmentation_summary_core.csv', core_summary(summary))

    headline = [c for c in CORE_COST + CORE_METRICS if c in summary.columns]
    print()
    with pd.option_context('display.width', 200, 'display.max_columns', None,
                           'display.float_format', '{:.4f}'.format):
        print(summary.set_index('model')[headline])
    print(f'\nWritten segmentation_summary.csv, segmentation_summary_core.csv and '
          f'segmentation_frames.csv to {out_dir}')


if __name__ == '__main__':
    main()
