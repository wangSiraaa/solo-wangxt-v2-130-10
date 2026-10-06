#!/usr/bin/env python3
"""Offline algorithm acceptance checks for the 100k pressure dataset.

This deliberately exercises only the deterministic SciPy core, so algorithm behavior
can be reproduced without services or a database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(ROOT))

from app.services.network import (  # noqa: E402
    build_components,
    canonical_sha256,
    compute_weights,
    fundamental_closures,
    solve_component,
)


def load(path: Path) -> tuple[list[dict], list[dict]]:
    data = json.loads(path.read_text())
    points = [{"id": i + 1, "code": p["code"], "name": p.get("name")} for i, p in enumerate(data["points"])]
    code_to_id = {p["code"]: p["id"] for p in points}
    observations = [
        {
            "id": i + 1,
            "line_code": o["line_code"],
            "from_point_id": code_to_id[o["from_code"]],
            "to_point_id": code_to_id[o["to_code"]],
            "observed_delta_m": float(o["observed_delta_m"]),
            "distance_m": float(o["distance_m"]),
            "weight_override": o.get("weight_override"),
        }
        for i, o in enumerate(data["observations"])
    ]
    return points, observations


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("scripts/pressure_100k.json"))
    args = parser.parse_args()
    points, observations = load(args.input)
    raw_hash = canonical_sha256([(o["line_code"], o["observed_delta_m"]) for o in observations])
    started = time.perf_counter()
    components = build_components(points, observations)
    weights = compute_weights(observations, {"method": "millimeter_sqrt_km", "c_km": 1.0, "base_sigma_m": 0.001})
    datums = [{"point_id": points[0]["id"], "elevation_m": 100.0, "sigma_m": 0.0001}]
    point_to_component: dict[int, int] = {}
    for index, component in enumerate(components):
        for local_index in component:
            point_to_component[points[local_index]["id"]] = index
    component_observations: list[list[tuple[int, dict]]] = [[] for _ in components]
    for index, observation in enumerate(observations):
        component = point_to_component[observation["from_point_id"]]
        if point_to_component.get(observation["to_point_id"]) == component:
            component_observations[component].append((index, observation))
    results = [
        solve_component(
            component,
            points,
            component_observations[index],
            datums,
            weights,
            ill_threshold=1e12,
            rank_tol=1e-9,
            dense_qr_max_rows=20_000,
            estimate_condition=False,
        )
        for index, component in enumerate(components)
    ]
    solved_seconds = time.perf_counter() - started
    closures = fundamental_closures(observations, max_cycles=500)

    assert len(components) == 1
    assert all(r["status"] == "ok" for r in results)
    assert all(r["diagnostics"].get("regularization") == "none" for r in results)
    assert all(r["method"] == "sparse_normal_equations" for r in results)
    assert len(observations) == 100_000
    assert len(closures) == 500

    report = {
        "input_file": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "raw_observations_sha256": raw_hash,
        "point_count": len(points),
        "observation_count": len(observations),
        "component_count": len(components),
        "solve_seconds": round(solved_seconds, 3),
        "methods": sorted({r["method"] for r in results}),
        "regularization": "none",
        "closure_checks": len(closures),
        "closure_failed": sum(not c["passed"] for c in closures),
        "algorithm": {
            "signature": "weighted-ls-v1",
            "normal_equations": "sparse A^T W A, scipy.sparse.linalg.spsolve",
            "weight_rule": "millimeter_sqrt_km c_km=1 base_sigma_m=.001",
            "qr": "diagnostic only on rank/ill-condition blocks",
        },
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
