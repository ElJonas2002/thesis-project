"""
Shared SuperDec front-end: preprocessing, inference and primitive merging.

It lives in the ROS package so that `superdec_node` and the offline bench in
`scripts/superdec_offline.py` exercise exactly the same code path; the bench exists to
measure changes here before they reach the robot.
"""
import os
import sys
import cv2 as cv
import numpy as np
import torch

N_POINTS = 4096


def write_ply(path, points):
    """Minimal binary PLY writer so the node stays free of open3d/trimesh."""
    header = ("ply\n"
              "format binary_little_endian 1.0\n"
              f"element vertex {points.shape[0]}\n"
              "property float x\nproperty float y\nproperty float z\n"
              "end_header\n")
    with open(path, 'wb') as f:
        f.write(header.encode('ascii'))
        f.write(points.astype(np.float32).tobytes())


def add_superdec_to_path(root):
    """The repo folder `superdec/` shadows the package of the same name, so its parent wins."""
    root = os.path.abspath(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def load_model(checkpoint_dir, device, lm_optimization=False):
    from omegaconf import OmegaConf
    from superdec.superdec import SuperDec

    config = OmegaConf.load(os.path.join(checkpoint_dir, 'config.yaml'))
    checkpoint = torch.load(os.path.join(checkpoint_dir, 'ckpt.pt'),
                            map_location=device, weights_only=False)
    model = SuperDec(config.superdec).to(device)
    model.load_state_dict(checkpoint['model_state_dict'])
    if lm_optimization:
        from superdec.lm_optimization.lm_optimizer import LMOptimizer
        model.lm_optimizer = LMOptimizer()
        model.lm_optimization = True
    model.eval()
    return model


def canonical_rotation(normal):
    """
    Rotation taking the table normal to +Y, matching the ShapeNet y-up training convention.
    """
    up = normal / np.linalg.norm(normal)
    ref = np.array([0.0, 0.0, 1.0])
    if abs(up @ ref) > 0.95:
        ref = np.array([1.0, 0.0, 0.0])
    x_axis = np.cross(up, ref)
    x_axis /= np.linalg.norm(x_axis)
    z_axis = np.cross(x_axis, up)
    return np.stack([x_axis, up, z_axis])


def complete_shell(points, normal, offset, rng, grid=64, floor_ratio=0.3, wall_ratio=0.6):
    """
    Close the single-view shell using the table as a floor.

    SuperDec was trained on points sampled from *closed mesh surfaces*, not a filled solid: the visible surface, 
    the footprint acting as the bottom face, and vertical walls raised along the silhouette to stand in for the
    occluded sides. Wall heights and cell positions are drawn continuously; sampling them on
    a fixed ladder makes the network fit one flat primitive per rung.
    """
    # 1. Get the up (y) and plane axes (x, z) for the floor plane.
    up = normal / np.linalg.norm(normal)
    plane_axes = canonical_rotation(normal)[[0, 2]]

    # 2. Compute the height of each point above the floor and its UV coordinates in the plane (projection onto the floor).
    height = np.maximum(points @ up + offset, 0.0)
    uv = points @ plane_axes.T

    lo = uv.min(axis=0)
    span = np.maximum(uv.max(axis=0) - lo, 1e-6)
    cell = np.clip(((uv - lo) / span * (grid - 1)).astype(np.int32), 0, grid - 1)

    hmap = np.zeros((grid, grid), np.float32)
    np.maximum.at(hmap, (cell[:, 1], cell[:, 0]), height.astype(np.float32))

    # Square structuring elements would square off round silhouettes.
    round3 = cv.getStructuringElement(cv.MORPH_ELLIPSE, (3, 3))
    occupied = cv.morphologyEx((hmap > 0).astype(np.uint8), cv.MORPH_CLOSE, round3)
    hmap = cv.dilate(hmap, round3)

    def to_3d(rows, cols, heights):
        # Jitter inside the cell so the raster grid does not show up as stair steps.
        jitter = rng.uniform(-0.5, 0.5, (rows.shape[0], 2))
        centres = (np.stack([cols, rows], axis=1) + jitter) / (grid - 1) * span + lo
        return centres @ plane_axes + np.outer(heights - offset, up)

    rows, cols = np.nonzero(occupied)
    if rows.size == 0:
        return points
    pick = rng.integers(0, rows.shape[0], int(points.shape[0] * floor_ratio))
    floor = to_3d(rows[pick], cols[pick], np.zeros(pick.shape[0]))

    border = occupied - cv.erode(occupied, round3)
    brows, bcols = np.nonzero(border)
    if brows.size == 0:
        return np.vstack([points, floor])
    pick = rng.integers(0, brows.shape[0], int(points.shape[0] * wall_ratio))
    tops = hmap[brows[pick], bcols[pick]] * rng.random(pick.shape[0])
    walls = to_3d(brows[pick], bcols[pick], tops)

    return np.vstack([points, floor, walls])


def uniform_resample(points, target=N_POINTS):
    """
    Equalise surface density before inference.

    ShapeNet clouds are sampled uniformly over a mesh, but a depth camera puts far more
    points on the face it can see than the synthetic floor/walls carry. The assignment
    matrix allocates primitives roughly in proportion to point density, so the visible
    face ends up wearing several stacked slabs.
    """
    import open3d as o3d
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    diag = np.linalg.norm(points.max(axis=0) - points.min(axis=0))

    low, high = diag / 400.0, diag / 8.0
    best = points
    for _ in range(20):
        voxel = 0.5 * (low + high)
        down = np.asarray(pc.voxel_down_sample(voxel).points)
        if down.shape[0] >= target:
            best = down
            low = voxel
        else:
            high = voxel
        if abs(down.shape[0] - 1.3 * target) < 0.05 * target:
            break
    return best


def denoise(points, neighbours=20, std_ratio=2.0):
    """Statistical outlier removal; a single flying pixel corrupts the whole normalisation."""
    import open3d as o3d
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    kept, _ = pc.remove_statistical_outlier(nb_neighbors=neighbours, std_ratio=std_ratio)
    kept = np.asarray(kept.points)
    return kept if kept.shape[0] >= 32 else points


def normalize_points(points):
    translation = points.mean(0)
    points = points - translation
    scale = 2.0 * np.max(np.abs(points))
    return points / scale, translation, scale


def radial_distance(local, extent, e_prof, e_sect):
    """
    Solina-Bajcsy radial distance, evaluated in log space.

    The 1/epsilon exponents reach 1/0.1 = 10 and overflow to NaN if the powers are direct.
    """
    logs = 2.0 * torch.log((local / extent).abs().clamp_min(1e-9))
    log_xy = (e_sect / e_prof) * torch.logaddexp(logs[..., 0] / e_sect, logs[..., 1] / e_sect)
    log_f = torch.logaddexp(log_xy, logs[..., 2] / e_prof)
    return local.norm(dim=-1) * (torch.exp(-0.5 * e_prof * log_f) - 1.0).abs()


def fit_groups(pts, mask, grid=5, shrinks=(0.85, 0.95, 1.0), budget=4_000_000):
    """
    Fit one superquadric per padded group of points, without gradient descent.

    Pose and extents are closed form (PCA of the group); only the two exponents and a
    global shrink factor are searched, on a grid evaluated for every group at once. All
    three axis permutations are tried because a squat cylinder has two near-equal radial
    variances, so the order eigh returns is arbitrary and the profile axis can land inside
    the cross-section, which is precisely where a box is the better fit.
    """
    device = pts.device
    groups, count = pts.shape[0], pts.shape[1]
    weight = mask.float()
    total = weight.sum(1).clamp_min(1.0)
    centre = (pts * weight[..., None]).sum(1) / total[:, None]

    delta = (pts - centre[:, None]) * weight[..., None]
    cov = delta.transpose(1, 2) @ delta / total[:, None, None]
    cov = cov + torch.eye(3, device=device) * 1e-9
    axes = torch.linalg.eigh(cov).eigenvectors

    perms = torch.tensor([[0, 1, 2], [1, 2, 0], [2, 0, 1]], device=device)
    rot = axes[:, None].expand(groups, 3, 3, 3).gather(
        3, perms.view(1, 3, 1, 3).expand(groups, 3, 3, 3))
    # eigh may return a reflection; a superquadric is symmetric per axis, so flipping one is free.
    rot[..., 0] = rot[..., 0] * torch.where(torch.linalg.det(rot) < 0, -1.0, 1.0)[..., None]

    local = torch.einsum('gpji,gmj->gpmi', rot, pts - centre[:, None])
    extent = local.abs().masked_fill(~mask[:, None, :, None], 0.0).amax(2).clamp_min(1e-4)

    axis = torch.linspace(0.1, 1.9, grid, device=device)
    shrink, e_prof, e_sect = (t.reshape(-1) for t in torch.meshgrid(
        torch.tensor(shrinks, device=device), axis, axis, indexing='ij'))
    combos = shrink.numel()

    scores = []
    step = max(1, budget // (3 * combos * count * 3))
    for start in range(0, groups, step):
        chunk = slice(start, start + step)
        candidate = extent[chunk, :, None, None, :] * shrink.view(1, 1, combos, 1, 1)
        dist = radial_distance(local[chunk, :, None], candidate,
                               e_prof.view(1, 1, combos, 1), e_sect.view(1, 1, combos, 1))
        scores.append((dist * weight[chunk, None, None, :]).sum(-1) / total[chunk, None, None])
    score = torch.cat(scores).reshape(groups, -1)

    best = score.argmin(1)
    rows = torch.arange(groups, device=device)
    perm, combo = torch.div(best, combos, rounding_mode='floor'), best % combos
    params = {
        'scale': extent[rows, perm] * shrink[combo][:, None],
        'shape': torch.stack((e_prof[combo], e_sect[combo]), 1),
        'rotate': rot[rows, perm],
        'trans': centre,
    }
    return params, score[rows, best]


def stack_groups(points, groups, cap, rng):
    """Pad per-group point sets into one batch so every candidate is fitted in one call."""
    width = min(cap, max(g.size for g in groups))
    pts = points.new_zeros(len(groups), width, 3)
    mask = torch.zeros(len(groups), width, dtype=torch.bool, device=points.device)
    for i, group in enumerate(groups):
        idx = group if group.size <= width else rng.choice(group, width, replace=False)
        pts[i, :idx.size] = points[torch.as_tensor(idx, device=points.device)]
        mask[i, :idx.size] = True
    return pts, mask


def merge_object(points, groups, tol, rng, grid, cap):
    """
    Greedily merge primitives whose points a single superquadric still explains.

    The retired refine stage cost ~19 s/object because each candidate pair triggered an
    11-DOF Adam optimisation. Here a candidate costs one 3x3 eigendecomposition plus a
    grid lookup, and every pair of a round is tested in a single batched call, so the
    search collapses from minutes to milliseconds.
    """
    _, residual = fit_groups(*stack_groups(points, groups, cap, rng), grid=grid)
    residual = residual.tolist()

    while len(groups) > 1:
        pairs = [(a, b) for a in range(len(groups)) for b in range(a + 1, len(groups))]
        union = [np.concatenate((groups[a], groups[b])) for a, b in pairs]
        _, cand = fit_groups(*stack_groups(points, union, cap, rng), grid=grid)

        base = torch.tensor([max(residual[a], residual[b]) for a, b in pairs],
                            device=cand.device).clamp_min(1e-6)
        ratio = cand / base
        pick = int(ratio.argmin())
        if float(ratio[pick]) > tol:
            break
        a, b = pairs[pick]
        groups[a], residual[a] = union[pick], float(cand[pick])
        groups.pop(b)
        residual.pop(b)
    return groups


def merge_stage(x, out, rng, tol=1.15, grid=5, cap=192):
    """Rebuild the prediction dict around the merged groups, keeping SuperDec's assignment."""
    assign = out['assign_matrix'].argmax(-1).cpu().numpy()
    alive = (out['exist'][..., 0] > 0.5).cpu().numpy()
    batch, count = x.shape[0], x.shape[1]

    kept = []
    for b in range(batch):
        groups = [np.nonzero(assign[b] == i)[0] for i in np.nonzero(alive[b])[0]]
        groups = [g for g in groups if g.size >= 32] or [np.arange(count)]
        kept.append(merge_object(x[b], groups, tol, rng, grid, cap))

    width = max(len(g) for g in kept)
    merged = {
        'scale': torch.full((batch, width, 3), 1e-4),
        'shape': torch.ones(batch, width, 2),
        'rotate': torch.eye(3).repeat(batch, width, 1, 1),
        'trans': torch.zeros(batch, width, 3),
        'exist': torch.zeros(batch, width, 1),
        'assign_matrix': torch.zeros(batch, count, width),
    }
    for b, groups in enumerate(kept):
        pts, mask = stack_groups(x[b], groups, 4 * cap, rng)
        params, _ = fit_groups(pts, mask, grid=2 * grid + 3)
        for key, value in params.items():
            merged[key][b, :len(groups)] = value.cpu()
        merged['exist'][b, :len(groups), 0] = 1.0
        for k, group in enumerate(groups):
            merged['assign_matrix'][b, group, k] = 1.0
    return {k: v.to(x.device) for k, v in merged.items()}


class SuperDecRunner:
    """
    Point clouds in, superquadrics out, in the frame the clouds arrived in.

    Defaults are the configuration adopted in EXPERIMENTS.md: denoise + complete + canonical
    + uniform, followed by the closed-form merge.
    """

    def __init__(self, superdec_root, checkpoint_dir, device=None, denoise=True,
                 complete=True, canonical=True, uniform=True, merge=True, merge_tol=1.15,
                 merge_grid=5, merge_cap=192, seed=0):
        add_superdec_to_path(superdec_root)
        self.device = device or ('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = load_model(checkpoint_dir, self.device)
        self.denoise = denoise
        self.complete = complete
        self.canonical = canonical
        self.uniform = uniform
        self.merge = merge
        self.merge_tol = merge_tol
        self.merge_grid = merge_grid
        self.merge_cap = merge_cap
        self.rng = np.random.default_rng(seed)

    def prepare(self, points, plane, frame):
        if self.denoise:
            points = denoise(points)
        if self.complete and plane is not None:
            points = complete_shell(points, plane[0], plane[1], self.rng)
        if frame is not None:
            points = points @ frame.T
        if self.uniform:
            points = uniform_resample(points)
        idx = self.rng.choice(points.shape[0], N_POINTS, replace=points.shape[0] < N_POINTS)
        return points[idx]

    @torch.no_grad()
    def __call__(self, clouds, plane=None):
        """`clouds` is a list of (N, 3) arrays; returns one dict of primitives per cloud."""
        frame = canonical_rotation(plane[0]) if (self.canonical and plane is not None) else None

        batch, shifts, scales = [], [], []
        for points in clouds:
            normalized, shift, scale = normalize_points(self.prepare(points, plane, frame))
            batch.append(normalized)
            shifts.append(shift)
            scales.append(scale)

        x = torch.from_numpy(np.stack(batch)).float().to(self.device)
        out = self.model(x)
        out = {k: v for k, v in out.items() if isinstance(v, torch.Tensor)}
        if self.merge:
            out = merge_stage(x, out, self.rng, self.merge_tol, self.merge_grid, self.merge_cap)

        scale = np.asarray(scales)[:, None]
        results = []
        for b in range(len(clouds)):
            alive = out['exist'][b, :, 0].cpu().numpy() > 0.5
            sizes = out['scale'][b].cpu().numpy()[alive] * scale[b]
            trans = out['trans'][b].cpu().numpy()[alive] * scale[b] + shifts[b]
            rotate = out['rotate'][b].cpu().numpy()[alive]
            if frame is not None:
                # Undo the canonical rotation so the primitives land back in the camera frame.
                trans = trans @ frame
                rotate = np.einsum('ij,pjk->pik', frame.T, rotate)
            results.append({'scale': sizes, 'shape': out['shape'][b].cpu().numpy()[alive],
                            'rotate': rotate, 'trans': trans})
        return results
