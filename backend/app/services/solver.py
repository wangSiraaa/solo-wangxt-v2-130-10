from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from math import isfinite
from typing import Any

from app.core.config import get_settings
from app.models.schema import ComponentResult, Job, ObservationResult
from app.services import network
from sqlalchemy.orm import Session


def _valid_number(value: Any, *, nonnegative: bool = False) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and (number >= 0 if nonnegative else True)


def partition_qc(payload: dict[str, Any], partition_count: int = 8) -> list[dict[str, Any]]:
    """Parallelizable row checks by deterministic partition.

    These checks never produce elevations. Solving remains a single global operation,
    although independent connected components may be pre-solved in parallel.
    """
    observations = payload["observations"]
    size = (len(observations) + partition_count - 1) // partition_count
    results = []
    for index in range(partition_count):
        rows = observations[index * size : (index + 1) * size]
        point_ids = {p["id"] for p in payload["points"]}
        bad_geometry = [r["line_code"] for r in rows if r["from_point_id"] not in point_ids or r["to_point_id"] not in point_ids]
        bad_numeric = [
            r["line_code"]
            for r in rows
            if not _valid_number(r["distance_m"], nonnegative=True) or not _valid_number(r["observed_delta_m"])
        ]
        loops = [r["line_code"] for r in rows if r["from_point_id"] == r["to_point_id"]]
        results.append(
            {
                "partition": index,
                "row_count": len(rows),
                "bad_geometry": bad_geometry[:100],
                "bad_numeric": bad_numeric[:100],
                "self_loops": loops[:100],
                "ok": not (bad_geometry or bad_numeric or loops),
            }
        )
    return results


def execute_solve(db: Session, job: Job) -> dict[str, Any]:
    settings = get_settings()
    snapshot = job.snapshot
    payload = snapshot.payload
    points = payload["points"]
    observations = payload["observations"]
    datums = payload["datums"]
    active_rules = payload["weight_rules"]
    if not active_rules:
        raise ValueError("at least one active weight rule is required")
    rule = active_rules[0]["rule"]

    weights = network.compute_weights(observations, rule)
    component_indices = network.build_components(points, observations)
    component_index_by_point: dict[int, int] = {}
    for component_index, local_indices in enumerate(component_indices):
        for local_index in local_indices:
            component_index_by_point[int(points[local_index]["id"])] = component_index
    component_observations: list[list[tuple[int, dict[str, Any]]]] = [[] for _ in component_indices]
    for obs_index, observation in enumerate(observations):
        component_index = component_index_by_point.get(int(observation["from_point_id"]))
        if component_index is not None and component_index_by_point.get(int(observation["to_point_id"])) == component_index:
            component_observations[component_index].append((obs_index, observation))
    # Connected components can be processed concurrently because each has disjoint
    # unknowns/observations. Results are collected as components, never used to
    # fabricate cross-component elevations by simple concatenation.
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(component_indices)))) as executor:
        futures = [
            executor.submit(
                network.solve_component,
                indices,
                points,
                component_observations[index],
                datums,
                weights,
                ill_threshold=settings.illconditioned_condition_number,
                rank_tol=settings.qr_rank_tol,
                dense_qr_max_rows=settings.dense_qr_max_rows,
            )
            for index, indices in enumerate(component_indices)
        ]
        components = [future.result() for future in futures]

    db.query(ComponentResult).filter(ComponentResult.job_id == job.id).delete()
    db.query(ObservationResult).filter(ObservationResult.job_id == job.id).delete()
    db.flush()

    all_residuals: list[float] = []
    blocked = []
    elevations: dict[str, float] = {}
    point_code = {int(p["id"]): p["code"] for p in points}
    component_by_point: dict[int, int] = {}

    for index, result in enumerate(components):
        for pid in result["point_ids"]:
            component_by_point[int(pid)] = index
        db.add(
            ComponentResult(
                job_id=job.id,
                component_index=index,
                point_count=result["point_count"],
                observation_count=result["observation_count"],
                datum_count=result["datum_count"],
                method=result["method"],
                rank=result["rank"],
                degrees_of_freedom=result.get("degrees_of_freedom"),
                condition_number=(
                    None
                    if result.get("condition_number") is None or not isfinite(float(result["condition_number"]))
                    else result["condition_number"]
                ),
                status=result["status"],
                elevations={} if result["x"] is None else dict(zip(map(str, result["point_ids"]), result["x"])),
                diagnostics=result.get("diagnostics", {}),
            )
        )
        if result["status"] != "ok":
            blocked.append({"component": index, "status": result["status"], "diagnostics": result.get("diagnostics", {})})
        elif result["x"] is not None:
            local_points = list(map(int, result["point_ids"]))
            local_index = {pid: i for i, pid in enumerate(local_points)}
            for pid, x in zip(result["point_ids"], result["x"]):
                elevations[point_code[int(pid)]] = float(x)

    for obs_index, obs in enumerate(observations):
        component = component_by_point.get(int(obs["from_point_id"]))
        result = components[component] if component is not None else None
        adjusted = correction = residual = None
        if result is not None and result["x"] is not None:
            local_points = list(map(int, result["point_ids"]))
            local_index = {pid: i for i, pid in enumerate(local_points)}
            x = result["x"]
            h_from = float(x[local_index[int(obs["from_point_id"])]])
            h_to = float(x[local_index[int(obs["to_point_id"])]])
            adjusted = h_to - h_from
            correction = adjusted - float(obs["observed_delta_m"])
            residual = correction
            all_residuals.append(residual)
        db.add(
            ObservationResult(
                job_id=job.id,
                observation_id=obs["id"],
                line_code=obs["line_code"],
                observed_delta_m=float(obs["observed_delta_m"]),
                adjusted_delta_m=adjusted,
                correction_m=correction,
                residual_v=residual,
                weight=float(weights[obs_index]),
            )
        )

    pre_closures = network.fundamental_closures(observations, max_cycles=settings.closure_max_cycles)

    adjusted_lookup: dict[int, float] = {}
    point_to_component: dict[int, tuple[int, dict[int, int], list[float]]] = {}
    for index, result in enumerate(components):
        if result["x"] is None:
            continue
        local_points = list(map(int, result["point_ids"]))
        local_index = {pid: i for i, pid in enumerate(local_points)}
        for pid in local_points:
            point_to_component[pid] = (index, local_index, result["x"])
    for obs in observations:
        if int(obs["from_point_id"]) in point_to_component:
            _index, local_index, x = point_to_component[int(obs["from_point_id"])]
            adjusted_lookup[int(obs["id"])] = float(
                x[local_index[int(obs["to_point_id"])]] - x[local_index[int(obs["from_point_id"])]]
            )
    post_closures = network.fundamental_closures(
        observations, delta_lookup=adjusted_lookup, max_cycles=settings.closure_max_cycles
    )
    summary = network.aggregate_residual_stats(all_residuals, weights)
    diagnostics = {
        "component_count": len(components),
        "blocked_components": blocked,
        "pre_adjustment_closures": {
            "checked": len(pre_closures),
            "failed": [c for c in pre_closures if not c["passed"]][:100],
        },
        "post_adjustment_closures": {
            "checked": len(post_closures),
            "failed": [c for c in post_closures if not c["passed"]][:100],
        },
        "residual_statistics": summary,
        "weight_rule": rule,
        "algorithm_signature": snapshot.algorithm["signature"],
        "regularization": "none",
    }
    db.flush()
    return {"elevations": elevations, "diagnostics": diagnostics, "components": components}
