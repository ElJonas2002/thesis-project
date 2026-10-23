"""
Shared SuperDec front-end: preprocessing, inference and primitive merging.

It lives in the ROS package so that `superdec_node` and the offline bench in
`scripts/superdec_offline.py` exercise exactly the same code path; the bench exists to
measure changes here before they reach the robot.
"""
import os
import sys
import warnings
import cv2 as cv
import numpy as np
import torch

# Number of points to sample for the SuperDec network. 
# More or less points will be randomly sampled from the input point cloud.
N_POINTS = 4096


def add_superdec_to_path(root):
    """
    The repo folder `superdec/` shadows the package of the same name, so its parent wins.
    """
    root = os.path.abspath(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


def load_model(checkpoint_dir, device, lm_optimization=False):
    """
    `lm_optimization` runs LM inside the network forward, i.e. before the merge, which then
    discards it; use `lm_stage` to refine the merged primitives instead.
    """
    from omegaconf import OmegaConf
    from superdec.superdec import SuperDec

    config_path = os.path.join(checkpoint_dir, 'config.yaml')
    checkpoint_path = os.path.join(checkpoint_dir, 'ckpt.pt')

    # Close configs path as it is no longer needed after loading
    with open(config_path) as f:
        config = OmegaConf.load(f)

    checkpoint = torch.load(checkpoint_path, map_location=device, weights_only=False)
    model = SuperDec(config.superdec).to(device)
    model.load_state_dict(checkpoint['model_state_dict'])
    model.lm_optimization = lm_optimization

    if lm_optimization:
        from superdec.lm_optimization.lm_optimizer import LMOptimizer
        model.lm_optimizer = LMOptimizer()

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

    # 3. Create a grid x grid mesh covering the UV plane of the points.
    lo = uv.min(axis=0)
    span = np.maximum(uv.max(axis=0) - lo, 1e-6)
    cell = np.clip(((uv - lo) / span * (grid - 1)).astype(np.int32), 0, grid - 1)

    # 4. Compute a height map: maximum height of each cell
    hmap = np.zeros((grid, grid), np.float32)
    np.maximum.at(hmap, (cell[:, 1], cell[:, 0]), height.astype(np.float32))

    # 5. Use ellipse kernel to avoid squaring off round silhouettes.
    round3 = cv.getStructuringElement(cv.MORPH_ELLIPSE, (3, 3))
    occupied = cv.morphologyEx((hmap > 0).astype(np.uint8), cv.MORPH_CLOSE, round3)
    hmap = cv.dilate(hmap, round3)

    def to_3d(rows, cols, heights):
        """
        Convert 2D grid coordinates and heights back to 3D points with a small jitter inside each cell 
        so that the raster grid does not show up as stair steps.
        Args:
            rows (np.ndarray): Row indices of the occupied cells.
            cols (np.ndarray): Column indices of the occupied cells.
            heights (np.ndarray): Heights corresponding to each cell.

        Returns:
            np.ndarray: 3D points reconstructed from the 2D grid and heights.
        """
        jitter = rng.uniform(-0.5, 0.5, (rows.shape[0], 2))
        centres = (np.stack([cols, rows], axis=1) + jitter) / (grid - 1) * span + lo
        return centres @ plane_axes + np.outer(heights - offset, up)

    # 6. Sample points from the occupied floor and wall cells to reconstruct the 3D scene.s
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
    """
    Statistical outlier removal; a single flying pixel corrupts the whole normalisation.
    """
    import open3d as o3d

    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))
    kept, _ = pc.remove_statistical_outlier(nb_neighbors=neighbours, std_ratio=std_ratio)
    kept = np.asarray(kept.points)
    return kept if kept.shape[0] >= 32 else points


def normalize_points(points):
    """
    Normalize points to fit within a unit cube centered at the origin.
    """
    translation = points.mean(0)
    points = points - translation
    scale = 2.0 * np.max(np.abs(points))
    return points / scale, translation, scale


def radial_distance(local, extent, e_prof, e_sect):
    """
    Solina-Bajcsy radial distance, evaluated in log space.

    Why log space?: The 1/epsilon exponents reach 1/0.1 = 10 and overflow to NaN if the powers are direct.

    Args:
        local (torch.Tensor): Local coordinates of points relative to the superquadric center.
        extent (torch.Tensor): Extent of the superquadric along each axis.
        e_prof (float): Profile exponent of the superquadric ($\varepsilon_{1}$).
        e_sect (float): Section exponent of the superquadric ($\varepsilon_{2}$).
    Returns:
        Radial distance of each point from the superquadric surface.

    """
    log_f = log_inside_outside(local, extent, e_prof, e_sect)
    return local.norm(dim=-1) * (torch.exp(-0.5 * e_prof * log_f) - 1.0).abs()


def log_inside_outside(local, extent, e_prof, e_sect):
    """Log of the superquadric inside-outside function: negative inside, zero on the surface."""
    logs = 2.0 * torch.log((local / extent).abs().clamp_min(1e-9))
    log_xy = (e_sect / e_prof) * torch.logaddexp(logs[..., 0] / e_sect, logs[..., 1] / e_sect)
    return torch.logaddexp(log_xy, logs[..., 2] / e_prof)


# Floor for a merge baseline, in normalised units (~0.5-1.25 mm on 0.1-0.25 m objects), about the
# depth noise: below it, residuals are grid and subsampling jitter and their ratio means nothing.
MERGE_FLOOR = 5e-3

# Cyclic permutations keep the determinant, so a permuted rotation is still a rotation.
AXIS_PERMUTATIONS = ((0, 1, 2), (1, 2, 0), (2, 0, 1))


def box_rotations(pts, mask, up):
    """
    Build one rotation per group from `up` and the footprint's minimum-area rectangle.

    `up` becomes the profile axis and the rectangle sets the yaw. For an object resting on
    the table only the yaw is unknown, and the footprint rectangle recovers it even for a
    cube, whose isotropic covariance leaves the PCA axes arbitrary.
    """
    up = np.asarray(up, dtype=np.float64)
    up = up / np.linalg.norm(up)
    plane = canonical_rotation(up)[[0, 2]]
    flat = (pts.double() @ torch.as_tensor(plane.T, device=pts.device)).cpu().numpy()
    keep = mask.cpu().numpy()

    rots = np.empty((pts.shape[0], 3, 3))
    for g in range(pts.shape[0]):
        _, _, angle = cv.minAreaRect(flat[g][keep[g]].astype(np.float32))
        theta = np.deg2rad(angle)
        u = np.cos(theta) * plane[0] + np.sin(theta) * plane[1]
        rots[g] = np.stack([u, np.cross(up, u), up], axis=1)
    return torch.as_tensor(rots, dtype=pts.dtype, device=pts.device)


def robust_bounds(local, mask, trim):
    """Per-axis `trim` and `1 - trim` quantiles of the valid points, (G, R, 3) each."""
    valid = mask[:, None, :, None]
    ordered = local.masked_fill(~valid, float('inf')).sort(dim=2).values
    last = (mask.sum(1).clamp_min(1) - 1).float()
    lo = torch.floor(trim * last).long()
    hi = torch.ceil((1.0 - trim) * last).long()

    def pick(index):
        index = index.view(-1, 1, 1, 1).expand(-1, local.shape[1], 1, 3)
        return ordered.gather(2, index).squeeze(2)
    return pick(lo), pick(hi)


def core_mask(pts, mask, trim):
    """Drop the `trim` fraction of points farthest from the median, which would skew the axes."""
    if trim <= 0.0:
        return mask
    median = pts.masked_fill(~mask[..., None], float('nan')).nanmedian(1).values
    dist = (pts - median[:, None]).norm(dim=-1).masked_fill(~mask, float('inf'))
    rank = dist.argsort(1).argsort(1)
    keep = torch.ceil((1.0 - trim) * mask.sum(1).float()).long().clamp_min(1)
    return mask & (rank < keep[:, None])


def fit_groups(pts, mask, grid=5, shrinks=(0.85, 0.95, 1.0), budget=4_000_000,
               seeds=None, up=None, trim=0.0):
    """
    Fit one superquadric per padded group of points, without gradient descent.

    Every candidate rotation is tried in its three axis permutations, because which axis is
    the profile one decides between a box and a cylinder. Candidates are the PCA axes, the
    footprint box around `up` when the table is known, and `seeds` (G, K, 3, 3), normally the
    network's rotations, which stay meaningful where PCA is arbitrary (isotropic shapes).
    Per rotation the centre and extents come from the `trim` quantiles of the local
    coordinates, so neither the uneven density of a depth camera nor a stray point can shift
    the box the way a point mean and a maximum do. Only the two exponents and a global
    shrink factor are searched, on a grid evaluated for every group and rotation at once.
    """
    device = pts.device
    groups, count = pts.shape[0], pts.shape[1]
    weight = mask.float()
    total = weight.sum(1).clamp_min(1.0)

    # The axes come from the core points only; a handful of outliers can rotate both the
    # covariance and the footprint rectangle away from every face of the object.
    core = core_mask(pts, mask, trim)
    core_weight = core.float()
    core_total = core_weight.sum(1).clamp_min(1.0)
    mean = (pts * core_weight[..., None]).sum(1) / core_total[:, None]

    delta = (pts - mean[:, None]) * core_weight[..., None]
    cov = delta.transpose(1, 2) @ delta / core_total[:, None, None]
    cov = cov + torch.eye(3, device=device) * 1e-9
    bases = [torch.linalg.eigh(cov).eigenvectors[:, None]]
    if up is not None:
        bases.append(box_rotations(pts, core, up)[:, None])
    if seeds is not None:
        bases.append(seeds.to(device=device, dtype=pts.dtype))
    bases = torch.cat(bases, 1)

    perms = torch.tensor(AXIS_PERMUTATIONS, device=device)
    rot = bases[:, :, None].expand(-1, -1, 3, -1, -1).gather(
        4, perms.view(1, 1, 3, 1, 3).expand(groups, bases.shape[1], 3, 3, 3))
    rot = rot.reshape(groups, -1, 3, 3).clone()
    # eigh may return a reflection; a superquadric is symmetric per axis, so flipping one is free.
    rot[..., 0] = rot[..., 0] * torch.where(torch.linalg.det(rot) < 0, -1.0, 1.0)[..., None]
    rots = rot.shape[1]

    local = torch.einsum('gpji,gmj->gpmi', rot, pts - mean[:, None])
    lo, hi = robust_bounds(local, mask, trim)
    middle = 0.5 * (lo + hi)
    extent = (0.5 * (hi - lo)).clamp_min(1e-4)
    local = local - middle[:, :, None]
    centre = mean[:, None] + torch.einsum('gpij,gpj->gpi', rot, middle)

    axis = torch.linspace(0.1, 1.9, grid, device=device)
    shrink, e_prof, e_sect = (t.reshape(-1) for t in torch.meshgrid(
        torch.tensor(shrinks, device=device), axis, axis, indexing='ij'))
    combos = shrink.numel()

    scores = []
    step = max(1, budget // (rots * combos * count * 3))
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
        'trans': centre[rows, perm],
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


def surface_samples(params, resolution=16):
    """
    Sample each primitive's surface with area weights.

    Returns the quad centres of an (eta, omega) grid, (P, S, 3), and the quad areas, (P, S).
    The grid alone is no uniform sampler: with exponents near 0.1 nearly all of its vertices
    gather on the edges and corners, which a real cloud rounds off, so unweighted it would
    penalise exactly the sharp boxes the merge must keep. x and y carry the section exponent
    and z the profile one, as in `radial_distance`.
    """
    device = params['scale'].device
    eta = torch.linspace(-np.pi / 2, np.pi / 2, resolution + 1, device=device)
    omega = torch.linspace(-np.pi, np.pi, 2 * resolution + 1, device=device)
    eta, omega = torch.meshgrid(eta, omega, indexing='ij')
    eta, omega = eta[None], omega[None]
    e_prof, e_sect = params['shape'][:, 0, None, None], params['shape'][:, 1, None, None]

    def signed_pow(value, exponent):
        return value.sign() * value.abs() ** exponent
    cos_eta = signed_pow(torch.cos(eta), e_prof)
    local = torch.stack([cos_eta * signed_pow(torch.cos(omega), e_sect),
                         cos_eta * signed_pow(torch.sin(omega), e_sect),
                         signed_pow(torch.sin(eta), e_prof).expand_as(cos_eta)], -1)
    local = local * params['scale'][:, None, None]
    vertices = torch.einsum('pij,pabj->pabi', params['rotate'], local)
    vertices = vertices + params['trans'][:, None, None]

    corner = vertices[:, :-1, :-1]
    centres = 0.25 * (corner + vertices[:, 1:, :-1] + vertices[:, :-1, 1:] + vertices[:, 1:, 1:])
    diagonal = torch.cross(vertices[:, 1:, 1:] - corner,
                           vertices[:, 1:, :-1] - vertices[:, :-1, 1:], dim=-1)
    area = 0.5 * diagonal.norm(dim=-1)
    return centres.flatten(1, 2), area.flatten(1, 2)


def inside(points, params):
    """Whether each point lies strictly inside each primitive, (P, N)."""
    local = torch.einsum('pji,pnj->pni', params['rotate'], points[None] - params['trans'][:, None])
    shape = params['shape'][:, None]
    return log_inside_outside(local, params['scale'][:, None], shape[..., 0], shape[..., 1]) < 0.0


def point_distances(points, params):
    """Radial distance from every point to every primitive in `params`, (P, N)."""
    local = torch.einsum('pji,pnj->pni', params['rotate'], points[None] - params['trans'][:, None])
    shape = params['shape'][:, None]
    return radial_distance(local, params['scale'][:, None], shape[..., 0], shape[..., 1])


def leaf_seeds(leaves, sizes, members, net, count):
    """Network rotations of the `count` largest leaves of each union, (G, count, 3, 3)."""
    seeds = []
    for union in members:
        largest = sorted(union, key=lambda leaf: -sizes[leaf])[:count]
        largest += largest[-1:] * (count - len(largest))
        seeds.append(net['rotate'][largest])
    return torch.stack(seeds)


def merge_object(points, leaves, net, rng, tol=1.3, grid=5, cap=192, up=None, trim=0.01,
                 adjacency=0.05):
    """
    Greedily merge touching leaves while one superquadric still explains their points.

    `leaves` are SuperDec's groups and `net` their predicted primitives (or None). Every leaf
    is represented by the better of its network primitive and its own closed-form fit. A
    union is accepted only while its single primitive stays under `tol` times the leaves'
    primitives *together* on two errors:
    - fit, each point to its closest primitive: neighbours overlap and cover each other's
      points, so a leaf against its own primitive alone would understate what is lost;
    - coverage, each surface sample to the closest point of the whole object, ignoring
      samples buried inside another leaf: the fit error cannot see a primitive that bulges
      past the object, while a face lying on a neighbour's points is legitimate.
    The baseline depends only on the leaves, never on earlier merges, so the tolerance cannot
    compound across rounds. Only leaves whose closest points lie within `adjacency`
    (normalised units) are ever paired. Returns lists of leaf indices.
    """
    device = points.device
    sizes = [leaf.size for leaf in leaves]
    index = [torch.as_tensor(leaf, device=device) for leaf in leaves]
    seeds = None if net is None else net['rotate'][:, None]
    chosen, _ = fit_groups(*stack_groups(points, leaves, cap, rng), grid=grid,
                           seeds=seeds, up=up, trim=trim)
    dist = point_distances(points, chosen)
    if net is not None:
        net_dist = point_distances(points, net)
        own = torch.stack([torch.stack((dist[k, i].mean(), net_dist[k, i].mean()))
                           for k, i in enumerate(index)])
        use_net = own[:, 1] <= own[:, 0]
        dist = torch.where(use_net[:, None], net_dist, dist)
        chosen = {k: torch.where(use_net.view(-1, *[1] * (v.dim() - 1)), net[k].to(v.dtype), v)
                  for k, v in chosen.items()}

    samples, area = surface_samples(chosen)
    count = samples.shape[1]
    buried = inside(samples.reshape(-1, 3), chosen).view(len(leaves), len(leaves), count)
    own_surface = torch.eye(len(leaves), dtype=torch.bool, device=device)[..., None]
    buried = buried.transpose(0, 1) & ~own_surface
    sample_gap = torch.cdist(samples.reshape(-1, 3), points).amin(1).view(len(leaves), count)

    def base_coverage(members):
        weight = area[members] * ~buried[members][:, members].any(1)
        return (weight * sample_gap[members]).sum() / weight.sum().clamp_min(1e-12)

    near = torch.stack([torch.cdist(points, points[i]).amin(1) for i in index])
    gap = torch.stack([near[:, i].amin(1) for i in index]).cpu().numpy()

    groups = [[k] for k in range(len(leaves))]
    while len(groups) > 1:
        pairs = [(a, b) for a in range(len(groups)) for b in range(a + 1, len(groups))
                 if gap[np.ix_(groups[a], groups[b])].min() <= adjacency]
        if not pairs:
            break
        members = [groups[a] + groups[b] for a, b in pairs]
        union = [torch.cat([index[k] for k in m]) for m in members]
        seeds = None if net is None else leaf_seeds(leaves, sizes, members, net, 2)
        params, _ = fit_groups(*stack_groups(points, [u.cpu().numpy() for u in union], cap, rng),
                               grid=grid, seeds=seeds, up=up, trim=trim)

        # Errors use every point, so the subsample only steers the fit.
        cand_samples, cand_area = surface_samples(params)
        cand_gap = torch.cdist(cand_samples.reshape(-1, 3), points).amin(1).view_as(cand_area)
        cand_gap = (cand_area * cand_gap).sum(1) / cand_area.sum(1).clamp_min(1e-12)
        ratio = []
        for g, (m, u) in enumerate(zip(members, union)):
            fit = point_distances(points[u], {k: v[g:g + 1] for k, v in params.items()}).mean()
            base_fit = dist[m][:, u].amin(0).mean()
            ratio.append(torch.maximum(fit / base_fit.clamp_min(MERGE_FLOOR),
                                       cand_gap[g] / base_coverage(m).clamp_min(MERGE_FLOOR)))
        ratio = torch.stack(ratio)
        pick = int(ratio.argmin())
        if float(ratio[pick]) > tol:
            break
        a, b = pairs[pick]
        groups[a] = members[pick]
        groups.pop(b)
    return groups


def split_leaves(points, labels, ids):
    """
    Split the points into one leaf per primitive in `ids`.

    Points owned by any other primitive go to the nearest leaf, so none is left without one.
    """
    leaves = [torch.nonzero(labels == i).squeeze(1) for i in ids]
    orphans = torch.nonzero(~torch.isin(labels, torch.as_tensor(ids, device=labels.device)))
    orphans = orphans.squeeze(1)
    if orphans.numel():
        near = torch.stack([torch.cdist(points[orphans], points[leaf]).amin(1) for leaf in leaves])
        owner = near.argmin(0)
        leaves = [torch.cat((leaf, orphans[owner == k])) for k, leaf in enumerate(leaves)]
    return [leaf.sort().values.cpu().numpy() for leaf in leaves]


PRIMITIVE_KEYS = ('scale', 'shape', 'rotate', 'trans')


def merge_stage(x, out, rng, tol=1.3, grid=5, cap=192, up=None, trim=0.01, adjacency=0.05,
                min_points=32):
    """
    Merge SuperDec's primitives, keeping the network's own fit wherever nothing was merged.

    Primitives with fewer than `min_points` points are dropped and their points go to the
    nearest surviving group. A group that survives alone keeps the network's parameters
    untouched; only merged groups are refitted in closed form, on a finer grid and seeded
    with the rotations of their two largest leaves. `up` is the table normal in the frame of
    `x` (+Y after the canonical rotation), or None if unknown.
    """
    assign = out['assign_matrix'].argmax(-1)
    alive = out['exist'][..., 0] > 0.5
    batch, count = x.shape[0], x.shape[1]

    kept = []
    for b in range(batch):
        ids = [int(i) for i in torch.nonzero(alive[b]).squeeze(1)
               if int((assign[b] == i).sum()) >= min_points]
        if not ids:
            kept.append(([np.arange(count)], None, [[0]]))
            continue
        leaves = split_leaves(x[b], assign[b], ids)
        net = {k: out[k][b, ids].to(x.dtype) for k in PRIMITIVE_KEYS}
        groups = merge_object(x[b], leaves, net, rng, tol, grid, cap, up, trim, adjacency)
        kept.append((leaves, net, groups))

    width = max(len(groups) for _, _, groups in kept)
    merged = {
        'scale': torch.full((batch, width, 3), 1e-4),
        'shape': torch.ones(batch, width, 2),
        'rotate': torch.eye(3).repeat(batch, width, 1, 1),
        'trans': torch.zeros(batch, width, 3),
        'exist': torch.zeros(batch, width, 1),
        'assign_matrix': torch.zeros(batch, count, width),
    }
    for b, (leaves, net, groups) in enumerate(kept):
        refit = [k for k, g in enumerate(groups) if net is None or len(g) > 1]
        if refit:
            union = [np.concatenate([leaves[i] for i in groups[k]]) for k in refit]
            seeds = None if net is None else leaf_seeds(
                leaves, [leaf.size for leaf in leaves], [groups[k] for k in refit], net, 2)
            params, _ = fit_groups(*stack_groups(x[b], union, 4 * cap, rng), grid=2 * grid + 3,
                                   seeds=seeds, up=up, trim=trim)
            for key, value in params.items():
                merged[key][b, refit] = value.float().cpu()
        for k, group in enumerate(groups):
            if k not in refit:
                for key in PRIMITIVE_KEYS:
                    merged[key][b, k] = net[key][group[0]].float().cpu()
            merged['exist'][b, k, 0] = 1.0
            merged['assign_matrix'][b, np.concatenate([leaves[i] for i in group]), k] = 1.0
    return {k: v.to(x.device) for k, v in merged.items()}


def table_up(plane, frame):
    """
    Return the table normal in the frame SuperDec sees.

    That is +Y after the canonical rotation, the camera-frame normal otherwise (normalisation
    only shifts and scales), and None without a plane.
    """
    if frame is not None:
        return np.array([0.0, 1.0, 0.0])
    return None if plane is None else np.asarray(plane[0], dtype=np.float64)


def primitive_errors(x, out):
    """Mean radial distance of each primitive to the points assigned to it, (B, P); NaN if none."""
    assign = out['assign_matrix']
    hard = torch.nn.functional.one_hot(assign.argmax(-1), assign.shape[-1])
    hard = (hard * (assign.sum(-1, keepdim=True) > 0)).transpose(1, 2).float()

    local = torch.einsum('bpji,bpnj->bpni', out['rotate'], x[:, None] - out['trans'][:, :, None])
    shape = out['shape'][:, :, None]
    dist = radial_distance(local, out['scale'][:, :, None], shape[..., 0], shape[..., 1])

    count = hard.sum(-1)
    error = (dist * hard).sum(-1) / count.clamp_min(1.0)
    return error.masked_fill(count == 0, float('nan'))


LM_SHAPE_MARGIN = 1e-3


def lm_stage(x, out, steps=5):
    """
    Levenberg-Marquardt refinement of the merged primitives, kept per primitive only if it helps.

    LM seeds its exponents with logit((eps - 0.1) / 1.8); the merge grid emits exactly 0.1 and
    1.9, which map to -inf/+inf, hence the clamp. A primitive keeps the LM result only if it is
    finite and lowers the radial error on its own points, so LM can never make a fit worse.
    LMOptimizer hard-codes `.cuda()`, so `x` must live on a CUDA device.
    """
    from superdec.lm_optimization.lm_optimizer import LMOptimizer

    alive = out['exist'][..., 0] > 0.5
    before = primitive_errors(x, out)
    if steps < 1 or not alive.any():
        return out, {'accepted': 0, 'alive': int(alive.sum()), 'before': before, 'after': before}
    if x.device.type != 'cuda':
        raise RuntimeError('LMOptimizer only runs on CUDA')

    seed = {k: v.clone() for k, v in out.items()}
    seed['shape'] = seed['shape'].clamp(0.1 + LM_SHAPE_MARGIN, 1.9 - LM_SHAPE_MARGIN)
    optimizer = LMOptimizer()
    optimizer.num_steps = steps
    refined = optimizer(seed, x)

    keys = ('scale', 'shape', 'rotate', 'trans')
    refined = {k: refined[k].to(out[k].dtype) for k in keys}
    finite = torch.stack([refined[k].flatten(2).isfinite().all(-1) for k in keys]).all(0)
    after = primitive_errors(x, {**out, **refined})
    accept = alive & finite & after.isfinite() & (after < before)

    result = dict(out)
    for k in keys:
        mask = accept.view(*accept.shape, *([1] * (out[k].dim() - 2)))
        result[k] = torch.where(mask, refined[k], out[k])
    stats = {'accepted': int(accept.sum()), 'alive': int(alive.sum()),
             'before': before, 'after': torch.where(accept, after, before)}
    return result, stats


class SuperDecRunner:
    """
    Point clouds in, superquadrics out, in the frame the clouds arrived in.

    Defaults: denoise + complete + canonical + uniform, followed by the closed-form merge.
    `lm=True` adds a guarded Levenberg-Marquardt refinement after the merge (CUDA only).
    """

    def __init__(self, superdec_root, checkpoint_dir, device=None, denoise=True,
                 complete=True, canonical=True, uniform=True, merge=True, merge_tol=1.3,
                 merge_grid=5, merge_cap=192, merge_trim=0.01, merge_adjacency=0.05,
                 lm=False, lm_steps=5, seed=0):
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
        self.merge_trim = merge_trim
        self.merge_adjacency = merge_adjacency
        self.lm = lm and torch.device(self.device).type == 'cuda'
        if lm and not self.lm:
            warnings.warn('LM refinement needs CUDA; disabled on ' + str(self.device))
        self.lm_steps = lm_steps
        self.last_lm_stats = None
        self.seed = seed
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
            # A fresh generator per call: the same cloud always merges the same way.
            out = merge_stage(x, out, np.random.default_rng(self.seed), self.merge_tol,
                              self.merge_grid, self.merge_cap, table_up(plane, frame),
                              self.merge_trim, self.merge_adjacency)
        if self.lm:
            out, self.last_lm_stats = lm_stage(x, out, self.lm_steps)

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
