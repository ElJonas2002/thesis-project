"""
Offline sanity check for SuperDec on point clouds captured by the fastsam_node snapshot service.

Capture a scene with:

    ros2 service call /fastsam_node/snapshot std_srvs/srv/Trigger

which writes snapshots/<timestamp>/objNN.ply plus plane.npz. This script then runs the
`normalized` checkpoint over every object of a snapshot and reports how many superquadrics
survive, how big they are, and how well they cover the input. The preprocessing switches
(--canonical, --extrude, --denoise) exist to measure what each one is worth before any of it
gets baked into a ROS node. Without a display, --export writes the fitted meshes and the
segmented clouds as PLY files to open elsewhere.

    python scripts/superdec_offline.py --canonical --denoise --export exports/run01
"""
import argparse
import glob
import os
import re
import sys
import time
import numpy as np
import torch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, 'src', 'intel_realsense'))

from intel_realsense.superdec_utils import (  # noqa: E402
    N_POINTS, add_superdec_to_path, canonical_rotation, complete_shell, denoise, load_model,
    lm_stage, merge_stage, normalize_points, uniform_resample)

add_superdec_to_path(os.path.join(REPO_ROOT, 'superdec'))

from superdec.data.dataloader import denormalize_outdict, denormalize_points  # noqa: E402
from superdec.utils.predictions_handler import PredictionHandler  # noqa: E402


def read_ply(path):
    """Read the binary xyz PLY written by fastsam_node."""
    with open(path, 'rb') as f:
        header = b''
        while True:
            line = f.readline()
            header += line
            if line.strip() == b'end_header' or not line:
                break
        count = int(re.search(rb'element vertex (\d+)', header).group(1))
        data = np.frombuffer(f.read(count * 12), dtype='<f4')
    return data.reshape(count, 3).astype(np.float64)


def resolve_snapshot(path):
    """
    Accept either a capture folder or the parent, in which case the newest capture wins.
    """
    if glob.glob(os.path.join(path, 'obj*.ply')):
        return path
    # Capture folders are named after their timestamp, so sorting by name sorts by time.
    captures = [d for d in sorted(glob.glob(os.path.join(path, '*')))
                if glob.glob(os.path.join(d, 'obj*.ply'))]
    if not captures:
        raise SystemExit(f'No obj*.ply found in {path} or any of its subfolders')
    return captures[-1]


def preprocess(path, plane, args, rng):
    points = read_ply(path)
    raw_count = points.shape[0]

    if args.denoise:
        points = denoise(points)
    if args.complete and plane is not None:
        points = complete_shell(points, *plane, rng)
    if args.canonical and plane is not None:
        points = points @ canonical_rotation(plane[0]).T
    if args.uniform:
        points = uniform_resample(points)

    idx = rng.choice(points.shape[0], N_POINTS, replace=points.shape[0] < N_POINTS)
    return points[idx], raw_count, points.shape[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', default='snapshots')
    parser.add_argument('--checkpoint-dir', default='superdec/checkpoints/normalized')
    parser.add_argument('--canonical', action='store_true', help='rotate so the table normal is +Y')
    parser.add_argument('--complete', action='store_true',
                        help='close the shell: footprint as floor + silhouette walls')
    parser.add_argument('--uniform', action='store_true',
                        help='voxel downsample so surface density is uniform')
    parser.add_argument('--denoise', action='store_true', help='statistical outlier removal')
    parser.add_argument('--lm', action='store_true',
                        help='Levenberg-Marquardt refinement after the merge, kept per primitive '
                             'only if it lowers the radial error (CUDA only)')
    parser.add_argument('--lm-steps', type=int, default=5, help='LM iterations')
    parser.add_argument('--merge', action='store_true',
                        help='greedily merge primitives a single superquadric can explain')
    parser.add_argument('--merge-tol', type=float, default=1.15,
                        help='accept a merge while its residual stays under tol x the children')
    parser.add_argument('--merge-grid', type=int, default=5, help='exponent grid used in the search')
    parser.add_argument('--merge-cap', type=int, default=192, help='points per group during the search')
    parser.add_argument('--resolution', type=int, default=20)
    parser.add_argument('--export', help='write the fitted meshes and segmented clouds as PLY here')
    parser.add_argument('--viser', action='store_true')
    args = parser.parse_args()

    snapshot_dir = resolve_snapshot(os.path.join(REPO_ROOT, args.snapshot))
    ply_files = sorted(glob.glob(os.path.join(snapshot_dir, 'obj*.ply')))
    if not ply_files:
        raise SystemExit(f'No obj*.ply found in {snapshot_dir}')
    print(f'Snapshot: {snapshot_dir}')

    plane = None
    plane_file = os.path.join(snapshot_dir, 'plane.npz')
    if os.path.exists(plane_file):
        data = np.load(plane_file)
        plane = (data['normal'], float(data['offset']))
        print(f'Plane: normal={plane[0].round(3)} offset={plane[1]:.3f}')
    elif args.canonical:
        raise SystemExit(f'--canonical needs a plane.npz in {snapshot_dir}')

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    model = load_model(os.path.join(REPO_ROOT, args.checkpoint_dir), device)
    if args.lm and device != 'cuda':
        print('LM refinement needs CUDA; --lm ignored')
        args.lm = False

    rng = np.random.default_rng(0)
    batch, translations, scales, names = [], [], [], []
    for path in ply_files:
        points, raw_count, used_count = preprocess(path, plane, args, rng)
        normalized, translation, scale = normalize_points(points)
        batch.append(normalized)
        translations.append(translation)
        scales.append(scale)
        names.append(os.path.basename(path))
        print(f'{names[-1]:>28s}  raw={raw_count:6d}  after_prep={used_count:6d}  extent={scale:.3f} m')

    x = torch.from_numpy(np.stack(batch)).float().to(device)
    with torch.no_grad():
        out = model(x)
    out = {k: v.to(device) for k, v in out.items() if isinstance(v, torch.Tensor)}

    if args.merge:
        start = time.perf_counter()
        with torch.no_grad():
            out = merge_stage(x, out, rng, args.merge_tol, args.merge_grid, args.merge_cap)
        print(f'\nmerge: {time.perf_counter() - start:.2f} s for {x.shape[0]} objects')

    errors = object_errors(x, out)
    lm_errors = None
    if args.lm:
        torch.cuda.synchronize()
        start = time.perf_counter()
        with torch.no_grad():
            out, stats = lm_stage(x, out, args.lm_steps)
        torch.cuda.synchronize()
        print(f'lm: {time.perf_counter() - start:.2f} s, accepted {stats["accepted"]}/'
              f'{stats["alive"]} primitives')
        lm_errors = object_errors(x, out)
    out = {k: v.detach().cpu() for k, v in out.items()}

    translations, scales = np.stack(translations), np.array(scales)
    out = denormalize_outdict(out, translations, scales, z_up=False)
    pcs = denormalize_points(x.cpu(), translations, scales, z_up=False)

    handler = PredictionHandler.from_outdict(out, pcs, names)
    report(handler, names)

    if lm_errors is None:
        print(f'\n{"object":>28s}  {"recon error":>13s}')
        for name, error, scale in zip(names, errors, scales):
            print(f'{name:>28s}  {error * scale * 1000:10.2f} mm')
    else:
        print(f'\n{"object":>28s}  {"before LM":>13s}  {"after LM":>13s}')
        for name, error, lm_error, scale in zip(names, errors, lm_errors, scales):
            print(f'{name:>28s}  {error * scale * 1000:10.2f} mm  {lm_error * scale * 1000:10.2f} mm')

    if args.export:
        export(handler, names, os.path.join(REPO_ROOT, args.export), args.resolution)

    if args.viser:
        show(handler, args.resolution)


def object_errors(x, out):
    return [mean_error(x[b], out['scale'][b], out['shape'][b], out['rotate'][b],
                       out['trans'][b], out['exist'][b, :, 0] > 0.5) for b in range(x.shape[0])]


def mean_error(points, scale, shape, rot, trans, alive):
    """
    Solina-Bajcsy radial distance from each point to the closest primitive surface.

    Evaluated in log space: the 1/epsilon exponents reach 1/0.1 = 10, which overflows to
    NaN if the powers are taken directly.
    """
    index = torch.nonzero(alive).squeeze(1)
    if index.numel() == 0:
        return float('nan')
    scale, shape = scale[index], shape[index]
    rot, trans = rot[index], trans[index]

    delta = points[:, None, :] - trans[None]
    local = torch.einsum('pji,npj->npi', rot, delta)
    logs = 2.0 * torch.log((local / scale[None]).abs().clamp_min(1e-9))

    e_prof, e_sect = shape[None, :, 0], shape[None, :, 1]
    log_xy = (e_sect / e_prof) * torch.logaddexp(logs[..., 0] / e_sect, logs[..., 1] / e_sect)
    log_f = torch.logaddexp(log_xy, logs[..., 2] / e_prof)
    distance = delta.norm(dim=-1) * (torch.exp(-0.5 * e_prof * log_f) - 1.0).abs()
    return float(distance.amin(dim=1).mean())


def report(handler, names):
    print(f'\n{"object":>28s}  {"#sq":>4s}  {"min edge":>9s}  {"max edge":>9s}  '
          f'{"flatness":>9s}  {"eps_prof":>9s}  {"eps_sect":>9s}  {"volume":>9s}')
    for i, name in enumerate(names):
        alive = handler.exist[i, :, 0] > 0.5
        if not alive.any():
            print(f'{name:>28s}  {0:4d}')
            continue
        sizes = handler.scale[i][alive] * 2.0
        # A slab patching the visible surface has one edge far shorter than the others.
        flatness = (sizes.min(axis=1) / sizes.max(axis=1)).mean()
        eps = handler.exponents[i][alive].mean(axis=0)
        volume = np.prod(sizes, axis=1).sum()
        print(f'{name:>28s}  {alive.sum():4d}  {sizes.min():9.4f}  {sizes.max():9.4f}  '
              f'{flatness:9.3f}  {eps[0]:9.3f}  {eps[1]:9.3f}  {volume:9.5f}')
    print('\nflatness = mean(min edge / max edge). <0.15 means flat patches glued to the')
    print('visible surface; >0.35 means the primitives actually have volume.')
    print('eps_prof / eps_sect = superquadric exponents. ~0.1 is a sharp box edge, ~1.0 is')
    print('round. A cylinder wants eps_prof~0.1 with eps_sect~1.0; a cube wants both ~0.1.')


def export(handler, names, out_dir, resolution):
    """PLY pair per object so the fit can be inspected on a machine that has a display."""
    from superdec.utils.visualizations import export_mesh_trimesh, export_o3d_pc

    os.makedirs(out_dir, exist_ok=True)
    meshes, pcs = handler.get_meshes(resolution=resolution), handler.get_segmented_pcs()
    for name, mesh, pc in zip(names, meshes, pcs):
        stem = os.path.splitext(name)[0]
        if mesh is not None:
            export_mesh_trimesh(mesh, os.path.join(out_dir, f'{stem}_sq.ply'))
        export_o3d_pc(pc, os.path.join(out_dir, f'{stem}_seg.ply'))
    print(f'\nExported {len(names)} mesh/cloud pairs to {out_dir}')


def show(handler, resolution):
    import time

    import viser
    server = viser.ViserServer()
    server.scene.set_up_direction([0.0, 1.0, 0.0])

    for i, mesh in enumerate(handler.get_meshes(resolution=resolution)):
        if mesh is not None:
            server.scene.add_mesh_trimesh(f'/sq_{i}', mesh=mesh)
    for i, pc in enumerate(handler.get_segmented_pcs()):
        server.scene.add_point_cloud(f'/pc_{i}', points=np.asarray(pc.points),
                                     colors=np.asarray(pc.colors), point_size=0.002)

    print('\nviser running at http://localhost:8080  (Ctrl-C to stop)')
    while True:
        time.sleep(10.0)


if __name__ == '__main__':
    main()
