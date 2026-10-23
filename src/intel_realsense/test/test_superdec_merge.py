"""Synthetic checks for the closed-form fit and the primitive merge in superdec_utils."""
import os

import numpy as np
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('cv2')

from intel_realsense.superdec_utils import (  # noqa: E402, I100
    fit_groups, merge_object, merge_stage, PRIMITIVE_KEYS, radial_distance, surface_samples)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))


def random_rotation(rng):
    q, r = np.linalg.qr(rng.normal(size=(3, 3)))
    q = q * np.sign(np.diag(r))
    return q if np.linalg.det(q) > 0 else -q


def yaw_rotation(angle):
    """Rotation about +Y, the table normal after the canonical rotation."""
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def box_surface(rng, half, count=1500):
    """Points uniformly spread over the faces of an axis-aligned box."""
    half = np.asarray(half, dtype=np.float64)
    areas = np.array([half[1] * half[2], half[0] * half[2], half[0] * half[1]])
    axis = rng.choice(3, count, p=areas / areas.sum())
    points = rng.uniform(-1.0, 1.0, (count, 3))
    points[np.arange(count), axis] = rng.choice([-1.0, 1.0], count)
    return points * half


def cylinder_surface(rng, radius, half_height, count=1500):
    """Sample a closed cylinder along +Y, area-weighted between the side and the caps."""
    side = 2.0 * radius * half_height
    on_side = rng.random(count) < side / (side + radius ** 2)
    angle = rng.uniform(0.0, 2.0 * np.pi, count)
    rho = np.where(on_side, radius, radius * np.sqrt(rng.random(count)))
    height = np.where(on_side, rng.uniform(-half_height, half_height, count),
                      rng.choice([-half_height, half_height], count))
    return np.stack([rho * np.cos(angle), height, rho * np.sin(angle)], axis=1)


def fit_one(points, **kwargs):
    pts = torch.as_tensor(points, dtype=torch.float32)[None]
    mask = torch.ones(pts.shape[:2], dtype=torch.bool)
    params, score = fit_groups(pts, mask, grid=13, **kwargs)
    return {k: v[0].numpy() for k, v in params.items()}, float(score[0])


def true_error(points, rotation, centre, half, shape):
    local = torch.as_tensor((points - centre) @ rotation, dtype=torch.float32)
    return float(radial_distance(local, torch.as_tensor(half, dtype=torch.float32),
                                 torch.tensor(shape[0]), torch.tensor(shape[1])).mean())


def axis_misalignment_deg(fitted, truth):
    """Worst angle between a fitted axis and its closest true axis (sign and order free)."""
    cosine = np.abs(fitted.T @ truth).max(axis=1).min()
    return float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))


def network_like(points, leaves):
    """Stand-in for SuperDec's per-leaf primitives: a fine closed-form fit of every leaf."""
    params = [fit_one(points[leaf])[0] for leaf in leaves]
    return {k: torch.as_tensor(np.stack([p[k] for p in params]), dtype=torch.float32)
            for k in PRIMITIVE_KEYS}


UP = np.array([0.0, 1.0, 0.0])


@pytest.mark.parametrize('seed', range(4))
def test_cube_on_table_keeps_box_shape(seed):
    rng = np.random.default_rng(seed)
    rotation = yaw_rotation(rng.uniform(0.0, np.pi))
    centre = rng.uniform(-0.1, 0.1, 3)
    points = box_surface(rng, [0.4, 0.4, 0.4]) @ rotation.T + centre

    params, error = fit_one(points, up=UP)

    assert axis_misalignment_deg(params['rotate'], rotation) < 5.0
    np.testing.assert_allclose(params['scale'], 0.4, rtol=0.05)
    assert params['shape'].max() <= 0.3
    assert error <= 2.0 * true_error(points, rotation, centre, [0.4] * 3, (0.1, 0.1)) + 1e-3


@pytest.mark.parametrize('seed', range(3))
def test_cube_in_free_pose_recovered_from_seed(seed):
    """Without a table, a seed rotation (the network's) must win over the arbitrary PCA axes."""
    rng = np.random.default_rng(seed)
    rotation = random_rotation(rng)
    points = box_surface(rng, [0.4, 0.4, 0.4]) @ rotation.T

    seed_rotation = torch.as_tensor(rotation, dtype=torch.float32)[None, None]
    params, _ = fit_one(points, seeds=seed_rotation)

    assert axis_misalignment_deg(params['rotate'], rotation) < 5.0
    assert params['shape'].max() <= 0.3


def test_cylinder_on_table_has_round_section():
    rng = np.random.default_rng(0)
    points = cylinder_surface(rng, radius=0.3, half_height=0.45)

    params, _ = fit_one(points, up=UP)

    profile = params['rotate'][:, 2]
    assert abs(profile @ UP) > np.cos(np.radians(5.0))
    assert params['shape'][0] <= 0.3
    assert abs(params['shape'][1] - 1.0) <= 0.3
    np.testing.assert_allclose(params['scale'], [0.3, 0.3, 0.45], rtol=0.05)


def test_uneven_density_and_outliers_do_not_shift_the_box():
    """Two thirds of the points on one face, plus outliers below the `trim` fraction."""
    rng = np.random.default_rng(1)
    half = np.array([0.3, 0.2, 0.25])
    visible = box_surface(rng, half, 4000)
    visible = visible[np.isclose(visible[:, 2], half[2])][:1200]
    rest = box_surface(rng, half, 300)
    outliers = rng.uniform(0.6, 0.9, (15, 3))
    points = np.vstack([visible, rest, outliers])
    assert outliers.shape[0] / points.shape[0] < 0.02

    params, _ = fit_one(points, up=UP, trim=0.02)

    assert np.linalg.norm(params['trans']) < 0.05 * 2.0 * half.max()
    np.testing.assert_allclose(np.sort(params['scale']), np.sort(half), rtol=0.10)


def split_by_planes(points, cuts):
    """Leaves of a single object cut along x (and z if `cuts` is 4)."""
    labels = (points[:, 0] > 0).astype(int)
    if cuts == 4:
        labels += 2 * (points[:, 2] > 0)
    return [np.nonzero(labels == k)[0] for k in range(cuts)]


@pytest.mark.parametrize('noise', [0.0, 0.005])
@pytest.mark.parametrize('cuts', [2, 4])
def test_split_cube_merges_back(cuts, noise):
    rng = np.random.default_rng(cuts)
    points = box_surface(rng, [0.4, 0.4, 0.4], 3000)
    points = points + rng.normal(0.0, noise, points.shape)
    leaves = split_by_planes(points, cuts)
    pts = torch.as_tensor(points, dtype=torch.float32)

    groups = merge_object(pts, leaves, network_like(points, leaves), rng, up=UP)

    assert len(groups) == 1


def test_separate_objects_never_merge():
    rng = np.random.default_rng(2)
    left = box_surface(rng, [0.2, 0.2, 0.2]) - [0.5, 0.0, 0.0]
    right = box_surface(rng, [0.2, 0.2, 0.2]) + [0.5, 0.0, 0.0]
    points = np.vstack([left, right])
    leaves = [np.arange(left.shape[0]), np.arange(left.shape[0], points.shape[0])]

    # Even an absurd tolerance must not bridge a 0.6 gap.
    groups = merge_object(torch.as_tensor(points, dtype=torch.float32), leaves,
                          network_like(points, leaves), rng, tol=1e6, up=UP)

    assert len(groups) == 2


def test_l_shape_stays_two_primitives():
    rng = np.random.default_rng(3)
    long_bar = box_surface(rng, [0.4, 0.08, 0.08]) + [0.0, -0.32, 0.0]
    upright = box_surface(rng, [0.08, 0.32, 0.08]) + [-0.32, 0.08, 0.0]
    points = np.vstack([long_bar, upright])
    leaves = [np.arange(long_bar.shape[0]), np.arange(long_bar.shape[0], points.shape[0])]

    groups = merge_object(torch.as_tensor(points, dtype=torch.float32), leaves,
                          network_like(points, leaves), rng, up=UP)

    assert len(groups) == 2


def test_tolerance_does_not_compound_along_a_curved_chain():
    """A bar bent into a half ring: each merge looks mild, the whole ring is not one box."""
    rng = np.random.default_rng(4)
    segments, parts = 6, []
    for k in range(segments):
        angle = np.pi * (k + 0.5) / segments
        tangent = yaw_rotation(-angle)
        segment = box_surface(rng, [0.06, 0.06, 0.15], 600) @ tangent.T
        parts.append(segment + 0.45 * np.array([np.cos(angle), 0.0, np.sin(angle)]))
    points = np.vstack(parts)
    bounds = np.cumsum([0] + [p.shape[0] for p in parts])
    leaves = [np.arange(bounds[k], bounds[k + 1]) for k in range(segments)]

    groups = merge_object(torch.as_tensor(points, dtype=torch.float32), leaves,
                          network_like(points, leaves), rng, up=UP)

    assert len(groups) >= 2


def synthetic_out(points, leaves, extra_labels=None, dead=()):
    """SuperDec-shaped prediction dict: hard assignment, `dead` primitives switched off."""
    net = network_like(points, leaves)
    count, prims = points.shape[0], len(leaves)
    labels = np.empty(count, dtype=int)
    for k, leaf in enumerate(leaves):
        labels[leaf] = k
    if extra_labels is not None:
        labels = extra_labels
        prims = labels.max() + 1
    out = {k: torch.zeros((1, prims) + net[k].shape[1:]) for k in PRIMITIVE_KEYS}
    out['rotate'][:] = torch.eye(3)
    out['scale'][:] = 0.1
    out['shape'][:] = 1.0
    for k in PRIMITIVE_KEYS:
        out[k][0, :len(leaves)] = net[k]
    out['exist'] = torch.ones(1, prims, 1)
    for k in dead:
        out['exist'][0, k] = 0.0
    out['assign_matrix'] = torch.nn.functional.one_hot(
        torch.as_tensor(labels), prims).float()[None]
    return out


def two_cubes(rng):
    left = box_surface(rng, [0.2, 0.2, 0.2], 1000) - [0.5, 0.0, 0.0]
    right = box_surface(rng, [0.2, 0.2, 0.2], 1000) + [0.5, 0.0, 0.0]
    points = np.vstack([left, right])
    return points, [np.arange(1000), np.arange(1000, 2000)]


def test_unmerged_primitives_keep_network_parameters():
    rng = np.random.default_rng(5)
    points, leaves = two_cubes(rng)
    out = synthetic_out(points, leaves)
    x = torch.as_tensor(points, dtype=torch.float32)[None]

    merged = merge_stage(x, out, np.random.default_rng(0), up=UP)

    assert int(merged['exist'].sum()) == 2
    for k in PRIMITIVE_KEYS:
        assert torch.equal(merged[k][0, :2], out[k][0, :2])


def test_small_and_dead_primitives_hand_their_points_over():
    rng = np.random.default_rng(6)
    points, leaves = two_cubes(rng)
    labels = np.repeat([0, 1], 1000)
    labels[:10] = 2            # too small to stand alone
    labels[1000:1040] = 3      # assigned to a primitive the network switched off
    out = synthetic_out(points, leaves, extra_labels=labels, dead=(3,))
    x = torch.as_tensor(points, dtype=torch.float32)[None]

    merged = merge_stage(x, out, np.random.default_rng(0), up=UP)

    assert int(merged['exist'].sum()) == 2
    torch.testing.assert_close(merged['assign_matrix'][0].sum(-1), torch.ones(2000))
    assert merged['assign_matrix'][0, :10, 0].all()
    assert merged['assign_matrix'][0, 1000:1040, 1].all()


def test_everything_too_small_falls_back_to_one_primitive():
    rng = np.random.default_rng(7)
    points = box_surface(rng, [0.3, 0.3, 0.3], 200)
    labels = np.arange(200) % 10
    out = synthetic_out(points, [np.nonzero(labels == k)[0] for k in range(10)])
    x = torch.as_tensor(points, dtype=torch.float32)[None]

    merged = merge_stage(x, out, np.random.default_rng(0), up=UP)

    assert int(merged['exist'].sum()) == 1
    torch.testing.assert_close(merged['assign_matrix'][0].sum(-1), torch.ones(200))


def test_batch_pads_objects_with_fewer_primitives():
    rng = np.random.default_rng(8)
    points, leaves = two_cubes(rng)
    single = box_surface(rng, [0.3, 0.3, 0.3], 2000)
    pair, one = synthetic_out(points, leaves), synthetic_out(single, [np.arange(2000)])
    out = {k: torch.cat([pair[k], torch.cat(
        [one[k], torch.zeros_like(one[k][:, :1]).expand_as(one[k])], 1)
        if k != 'assign_matrix' else torch.cat([one[k], torch.zeros_like(one[k])], 2)])
        for k in pair}
    x = torch.as_tensor(np.stack([points, single]), dtype=torch.float32)

    merged = merge_stage(x, out, np.random.default_rng(0), up=UP)

    assert merged['exist'][:, :, 0].sum(1).tolist() == [2.0, 1.0]


def test_merge_is_reproducible_with_a_fresh_generator():
    rng = np.random.default_rng(9)
    points = box_surface(rng, [0.4, 0.4, 0.4], 3000)
    leaves = split_by_planes(points, 4)
    out = synthetic_out(points, leaves)
    x = torch.as_tensor(points, dtype=torch.float32)[None]

    first = merge_stage(x, out, np.random.default_rng(0), up=UP)
    second = merge_stage(x, out, np.random.default_rng(0), up=UP)

    for k in first:
        assert torch.equal(first[k], second[k])


@pytest.mark.parametrize('shape, half, area', [
    ((1.0, 1.0), (0.3, 0.3, 0.3), 4.0 * np.pi * 0.3 ** 2),
    ((0.1, 0.1), (0.3, 0.2, 0.1), 8.0 * (0.3 * 0.2 + 0.3 * 0.1 + 0.2 * 0.1)),
])
def test_surface_samples_are_weighted_by_area(shape, half, area):
    """The coverage term must weigh a face by its area, not by how many grid vertices it gets."""
    rotation = torch.as_tensor(random_rotation(np.random.default_rng(11)), dtype=torch.float32)
    params = {'scale': torch.tensor([half]), 'shape': torch.tensor([shape]),
              'rotate': rotation[None], 'trans': torch.tensor([[0.1, -0.2, 0.3]])}

    samples, weights = surface_samples(params)

    assert float(weights.sum()) == pytest.approx(area, rel=0.05)
    local = (samples[0] - params['trans']) @ rotation
    on_surface = radial_distance(local, params['scale'][0], params['shape'][0, 0],
                                 params['shape'][0, 1])
    assert float((weights[0] * on_surface).sum() / weights.sum()) < 0.01 * max(half)


@pytest.mark.skipif(not torch.cuda.is_available(), reason='LMOptimizer only runs on CUDA')
def test_lm_stage_runs_after_merge():
    from intel_realsense.superdec_utils import add_superdec_to_path, lm_stage
    add_superdec_to_path(os.path.join(REPO_ROOT, 'superdec'))

    rng = np.random.default_rng(10)
    points, leaves = two_cubes(rng)
    out = {k: v.cuda() for k, v in synthetic_out(points, leaves).items()}
    x = torch.as_tensor(points, dtype=torch.float32, device='cuda')[None]

    merged = merge_stage(x, out, np.random.default_rng(0), up=UP)
    _, stats = lm_stage(x, merged, steps=1)

    assert stats['alive'] == 2
