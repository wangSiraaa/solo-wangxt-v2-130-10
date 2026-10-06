import math

import numpy as np
import pytest

from app.services.network import (
    build_components,
    compute_weights,
    fundamental_closures,
    solve_component,
)


def test_simple_weighted_least_squares_sparse_normal_solution():
    points = [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}, {"id": 3, "code": "C"}]
    observations = [
        {"id": 1, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1.001, "distance_m": 1000, "weight_override": None},
        {"id": 2, "from_point_id": 2, "to_point_id": 3, "observed_delta_m": 1.000, "distance_m": 1000, "weight_override": None},
        {"id": 3, "from_point_id": 1, "to_point_id": 3, "observed_delta_m": 2.004, "distance_m": 2000, "weight_override": None},
    ]
    datums = [{"point_id": 1, "elevation_m": 100.0, "sigma_m": 0.0001}]
    rule = {"method": "distance_inverse_km", "c_km": 1.0, "base_sigma_m": 0.001}
    weights = compute_weights(observations, rule)
    result = solve_component(
        [0, 1, 2], points, list(enumerate(observations)), datums, weights,
        ill_threshold=1e12, rank_tol=1e-9, dense_qr_max_rows=20_000
    )
    assert result["status"] == "ok"
    assert result["method"] == "sparse_normal_equations"
    assert result["diagnostics"]["regularization"] == "none"
    assert result["x"][0] == pytest.approx(100.0, abs=1e-6)
    assert 101.0 < result["x"][1] < 101.002
    assert result["degrees_of_freedom"] == 1


def test_disconnected_component_without_datum_is_blocked_not_regularized():
    points = [{"id": i, "code": f"P{i}"} for i in range(1, 5)]
    observations = [
        {"id": 1, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1, "distance_m": 1000},
        {"id": 2, "from_point_id": 3, "to_point_id": 4, "observed_delta_m": 1, "distance_m": 1000},
    ]
    components = build_components(points, observations)
    assert len(components) == 2
    weights = np.array([1.0, 1.0])
    datumless = next(c for c in components if all(points[i]["id"] in {3, 4} for i in c))
    component_ids = {points[i]["id"] for i in datumless}
    component_obs = [(i, o) for i, o in enumerate(observations) if o["from_point_id"] in component_ids]
    result = solve_component(
        datumless, points, component_obs, [], weights[[i for i, _ in component_obs]],
        ill_threshold=1e12, rank_tol=1e-9, dense_qr_max_rows=20_000
    )
    assert result["x"] is None
    assert result["status"] == "blocked_rank_deficient"
    assert result["diagnostics"]["regularization"] == "none"


def test_multiple_contradictory_datums_block_publication():
    points = [{"id": 1, "code": "A"}, {"id": 2, "code": "B"}]
    observations = [{"id": 1, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1, "distance_m": 1000}]
    datums = [
        {"point_id": 1, "elevation_m": 100.0, "sigma_m": 0.0001},
        {"point_id": 2, "elevation_m": 102.0, "sigma_m": 0.0001},
    ]
    result = solve_component(
        [0, 1], points, list(enumerate(observations)), datums, np.array([1.0]),
        ill_threshold=1e12, rank_tol=1e-9, dense_qr_max_rows=20_000
    )
    assert result["status"] == "blocked"
    assert result["diagnostics"]["reason"] == "weighted datum residual exceeds 3 sigma"


def test_closure_uses_cycle_path_not_component_stitching():
    observations = [
        {"id": 1, "from_point_id": 1, "to_point_id": 2, "observed_delta_m": 1.0, "distance_m": 1000},
        {"id": 2, "from_point_id": 2, "to_point_id": 3, "observed_delta_m": 1.0, "distance_m": 1000},
        {"id": 3, "from_point_id": 1, "to_point_id": 3, "observed_delta_m": 2.005, "distance_m": 2000},
    ]
    closures = fundamental_closures(observations, max_cycles=10)
    assert len(closures) == 1
    assert math.isclose(closures[0]["misclosure_m"], 0.005)
    assert closures[0]["passed"] is False
