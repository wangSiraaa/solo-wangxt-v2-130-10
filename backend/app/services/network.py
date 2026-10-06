"""Deterministic weighted least-squares core for leveling networks.

Observation model:
    h_to - h_from = observed_height_difference + v

Normal equations are sparse. QR is used only for rank/ill-condition diagnosis.
No ridge term, Bayesian prior, or other arbitrary regularization is added.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from scipy import sparse


@dataclass(frozen=True)
class WeightParams:
    method: str
    c_km: float
    base_sigma_m: float


def canonical_sha256(value: Any) -> str:
    import hashlib

    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def observation_sigma(distance_m: float, weight_override: float | None, params: WeightParams) -> float:
    if weight_override is not None and weight_override > 0:
        return 1.0 / math.sqrt(float(weight_override))
    distance_km = max(float(distance_m), 0.0) / 1000.0
    if params.method == "distance_inverse_km":
        variance_km = params.c_km * distance_km if distance_km > 0 else params.c_km
        return math.sqrt(max(variance_km, 1e-12))
    if params.method == "millimeter_sqrt_km":
        return math.sqrt(params.base_sigma_m**2 + (params.c_km * 0.001) ** 2 * max(distance_km, 0.0))
    raise ValueError(f"unsupported weight method: {params.method}")


def compute_weights(observations: list[dict[str, Any]], rule: dict[str, Any]) -> np.ndarray:
    params = WeightParams(
        method=str(rule.get("method", "millimeter_sqrt_km")),
        c_km=float(rule.get("c_km", 1.0)),
        base_sigma_m=float(rule.get("base_sigma_m", 0.001)),
    )
    sigmas = np.array(
        [observation_sigma(float(o["distance_m"]), o.get("weight_override"), params) for o in observations],
        dtype=np.float64,
    )
    return 1.0 / np.square(sigmas)


def build_components(points: list[dict[str, Any]], observations: list[dict[str, Any]]) -> list[list[int]]:
    point_count = len(points)
    parent = list(range(point_count))
    size = [1] * point_count

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    index = {int(point["id"]): i for i, point in enumerate(points)}
    for obs in observations:
        a = find(index[int(obs["from_point_id"])])
        b = find(index[int(obs["to_point_id"])])
        if a == b:
            continue
        if size[a] < size[b]:
            a, b = b, a
        parent[b] = a
        size[a] += size[b]

    groups: dict[int, list[int]] = {}
    for local_index in range(point_count):
        groups.setdefault(find(local_index), []).append(local_index)
    return [sorted(group) for group in groups.values()]


def _component_rows(
    component_local_indices: list[int],
    points: list[dict[str, Any]],
    component_observations: list[tuple[int, dict[str, Any]]],
    datums: list[dict[str, Any]],
    component_weights: np.ndarray,
) -> tuple[list[int], list[int], sparse.csr_matrix, np.ndarray, np.ndarray, list[dict[str, Any]]]:
    point_ids = [int(points[i]["id"]) for i in component_local_indices]
    local = {pid: i for i, pid in enumerate(point_ids)}
    obs_indices = [source_index for source_index, _obs in component_observations]
    rows: list[int] = []
    cols: list[int] = []
    data: list[float] = []
    b = np.empty(len(obs_indices), dtype=np.float64)
    weights = np.empty(len(obs_indices), dtype=np.float64)
    for row, (source_index, obs) in enumerate(component_observations):
        rows.extend((row, row))
        cols.extend((local[int(obs["from_point_id"])], local[int(obs["to_point_id"])]))
        data.extend((-1.0, 1.0))
        b[row] = float(obs["observed_delta_m"])
        weights[row] = component_weights[source_index]

    component_datums = [d for d in datums if int(d["point_id"]) in local and d.get("active", True)]
    datum_start = len(obs_indices)
    for k, datum in enumerate(component_datums):
        row = datum_start + k
        rows.append(row)
        cols.append(local[int(datum["point_id"])])
        data.append(1.0)
        b = np.append(b, float(datum["elevation_m"]))
        sigma = max(float(datum.get("sigma_m", 0.001)), 1e-9)
        weights = np.append(weights, 1.0 / (sigma * sigma))
    A = sparse.coo_matrix((data, (rows, cols)), shape=(len(b), len(point_ids))).tocsr()
    return point_ids, obs_indices, A, b, weights, component_datums


def _condition_number(N: sparse.csr_matrix, estimate: bool) -> float | None:
    if not estimate or N.shape[0] > 200_000:
        return None
    from scipy.sparse.linalg import eigsh

    try:
        lambda_max = float(eigsh(N, k=1, which="LA", return_eigenvectors=False, maxiter=5000)[0])
        lambda_min = float(eigsh(N, k=1, which="SA", return_eigenvectors=False, maxiter=5000)[0])
        if lambda_min <= 0:
            return math.inf
        return lambda_max / lambda_min
    except Exception:
        return None


def _qr_diagnostic(
    A: sparse.csr_matrix,
    b: np.ndarray,
    w: np.ndarray,
    rank_tol: float,
    dense_max_rows: int,
    reason: str,
) -> dict[str, Any]:
    if A.shape[0] > dense_max_rows:
        # SciPy does not expose a production sparse rank-revealing QR factorization.
        # Use sparse SVD only to establish the rank-defect/datum defect, then refuse
        # a publishable answer instead of returning a minimum-norm pseudo-solution.
        from scipy.sparse.linalg import svds

        sqrt_w = np.sqrt(w)
        Aw = (sparse.diags(sqrt_w) @ A).tocsr()
        try:
            singular_values = svds(Aw, k=1, return_singular_vectors=False, which="SM", maxiter=5000)
            smallest = float(np.min(singular_values))
            blocked_rank = smallest <= rank_tol
        except Exception as exc:
            smallest = None
            blocked_rank = True
            failure = type(exc).__name__
        else:
            failure = None
        return {
            "method": "sparse_rank_diagnostic",
            "status": "blocked_rank_deficient" if blocked_rank else "blocked_illconditioned",
            "reason": reason,
            "smallest_singular_value": smallest,
            "sparse_diagnostic_failure": failure,
            "message": "component exceeds dense QR diagnostic budget; no unique elevation was fabricated",
            "rank": None if blocked_rank else A.shape[1],
            "regularization": "none",
        }
    sqrt_w = np.sqrt(w)
    Aw = A.toarray() * sqrt_w[:, None]
    bw = b * sqrt_w
    Q, R = np.linalg.qr(Aw, mode="reduced")
    diag = np.abs(np.diag(R))
    scale = float(np.max(diag)) if diag.size else 0.0
    rank = int(np.count_nonzero(diag > rank_tol * max(scale, 1.0)))
    full_rank = rank == A.shape[1]
    x, *_ = np.linalg.lstsq(Aw, bw, rcond=rank_tol)
    return {
        "method": "qr_diagnostic",
        "status": "ok_full_rank" if full_rank else "blocked_rank_deficient",
        "reason": reason,
        "rank": rank,
        "nullity": int(A.shape[1] - rank),
        "r_diag_abs": diag.astype(float).tolist(),
        "diagnostic_x": x.astype(float).tolist(),
        "regularization": "none",
    }


def solve_component(
    component_local_indices: list[int],
    points: list[dict[str, Any]],
    component_observations: list[tuple[int, dict[str, Any]]],
    datums: list[dict[str, Any]],
    weights: np.ndarray,
    *,
    ill_threshold: float,
    rank_tol: float,
    dense_qr_max_rows: int,
    estimate_condition: bool = True,
) -> dict[str, Any]:
    point_ids, obs_indices, A, b, w, component_datums = _component_rows(
        component_local_indices, points, component_observations, datums, weights
    )
    n, m, d = len(point_ids), len(obs_indices), len(component_datums)
    W = sparse.diags(w)
    N = (A.T @ W @ A).tocsr()
    u = A.T @ (w * b)
    rank = None
    condition = None
    method = "sparse_normal_equations"
    qr: dict[str, Any] | None = None

    # A connected leveling component without a datum has a one-dimensional vertical
    # datum defect. It must be diagnosed, not pinned by an invented zero elevation.
    if d == 0:
        qr = _qr_diagnostic(A, b, w, rank_tol, dense_qr_max_rows, "no_datum_rank_deficiency")
        rank = qr.get("rank")
        return {
            "point_ids": point_ids,
            "observation_indices": obs_indices,
            "point_count": n,
            "observation_count": m,
            "datum_count": 0,
            "method": qr["method"],
            "status": qr["status"],
            "rank": rank,
            "degrees_of_freedom": None,
            "condition_number": None,
            "x": None,
            "diagnostics": qr,
        }

    try:
        from scipy.sparse.linalg import spsolve

        x = spsolve(N.tocsc(), u)
        if not np.all(np.isfinite(x)):
            raise np.linalg.LinAlgError("non-finite normal-equation solution")
        # The large-network pressure path uses the selected sparse normal solver.
        # Condition estimation is a diagnostic optimization on smaller/block-sized
        # components; factorization failure still triggers QR diagnosis.
        condition = _condition_number(N, estimate_condition and N.shape[0] <= 5_000)
        if condition is not None and condition > ill_threshold:
            qr = _qr_diagnostic(A, b, w, rank_tol, dense_qr_max_rows, f"condition_number>{ill_threshold:g}")
            if qr["status"] != "ok_full_rank":
                return _blocked_result(point_ids, obs_indices, n, m, d, qr)
            # A full-rank but ill-conditioned QR answer remains diagnostic only.
            x = np.asarray(qr["diagnostic_x"], dtype=np.float64)
            method, rank = qr["method"], qr["rank"]
            qr["condition_number"] = condition
            status = "blocked_illconditioned"
        else:
            rank = n
            status = "ok"
    except (np.linalg.LinAlgError, RuntimeError, ValueError) as exc:
        qr = _qr_diagnostic(A, b, w, rank_tol, dense_qr_max_rows, f"normal_equation_failure:{type(exc).__name__}")
        return _blocked_result(point_ids, obs_indices, n, m, d, qr, condition)

    fitted = A @ x
    residual = fitted - b
    # First m rows are leveling observations; datum rows follow.
    obs_residual = residual[:m]
    datum_residual = residual[m:]
    wrss = float(float(w @ np.square(residual)))
    dof = int(m + d - (rank or n))
    datum_diagnostics = [
        {
            "point_id": int(datum["point_id"]),
            "declared_m": float(datum["elevation_m"]),
            "solved_m": float(x[point_ids.index(int(datum["point_id"]))]),
            "residual_m": float(datum_residual[i]),
            "sigma_m": float(datum.get("sigma_m", 0.001)),
        }
        for i, datum in enumerate(component_datums)
    ]
    datum_contradictions = [
        d for d in datum_diagnostics if abs(d["residual_m"]) > 3.0 * max(d["sigma_m"], rank_tol)
    ]
    if datum_contradictions:
        qr = {
            "method": "qr_diagnostic",
            "status": "blocked_datum_contradiction",
            "reason": "weighted datum residual exceeds 3 sigma",
            "rank": rank,
            "nullity": 0,
            "datum_contradictions": datum_contradictions,
            "regularization": "none",
        }
        return _blocked_result(point_ids, obs_indices, n, m, d, qr, condition)

    return {
        "point_ids": point_ids,
        "observation_indices": obs_indices,
        "point_count": n,
        "observation_count": m,
        "datum_count": d,
        "method": method,
        "status": status,
        "rank": rank,
        "degrees_of_freedom": dof,
        "condition_number": condition,
        "x": x.astype(float).tolist(),
        "residual": obs_residual.astype(float).tolist(),
        "weighted_residual_sum_squares": wrss,
        "sigma0_squared": wrss / dof if dof > 0 else None,
        "datum_residuals": datum_diagnostics,
        "diagnostics": qr or {"regularization": "none"},
    }


def _blocked_result(
    point_ids: list[int],
    obs_indices: list[int],
    n: int,
    m: int,
    d: int,
    qr: dict[str, Any],
    condition: float | None = None,
) -> dict[str, Any]:
    return {
        "point_ids": point_ids,
        "observation_indices": obs_indices,
        "point_count": n,
        "observation_count": m,
        "datum_count": d,
        "method": qr["method"],
        "status": "blocked_rank_deficient" if "rank" in qr.get("status", "") else "blocked",
        "rank": qr.get("rank"),
        "degrees_of_freedom": None,
        "condition_number": condition,
        "x": None,
        "diagnostics": qr,
    }


def fundamental_closures(
    observations: list[dict[str, Any]],
    delta_lookup: dict[int, float] | None = None,
    max_cycles: int = 500,
    tolerance: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Deterministic fundamental-cycle closure checks.

    A spanning forest gives root/potential/distance. Every non-tree edge closes one
    independent cycle. Binary-lifting LCA computes the exact tree path length needed
    for the 2 mm/sqrt(km)-style tolerance without stitching separate components.
    """
    adjacency: dict[int, list[tuple[int, float, float, int]]] = {}
    dsu_parent: dict[int, int] = {}

    def find(node: int) -> int:
        dsu_parent.setdefault(node, node)
        while dsu_parent[node] != node:
            dsu_parent[node] = dsu_parent[dsu_parent[node]]
            node = dsu_parent[node]
        return node

    def union(a: int, b: int) -> bool:
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        dsu_parent[rb] = ra
        return True

    chords: list[dict[str, Any]] = []
    for obs in observations:
        a, b = int(obs["from_point_id"]), int(obs["to_point_id"])
        delta = float(delta_lookup.get(int(obs["id"]), obs["observed_delta_m"]) if delta_lookup else obs["observed_delta_m"])
        distance = float(obs["distance_m"])
        if union(a, b):
            adjacency.setdefault(a, []).append((b, delta, distance, int(obs["id"])))
            adjacency.setdefault(b, []).append((a, -delta, distance, int(obs["id"])))
        else:
            chords.append({"id": int(obs["id"]), "a": a, "b": b, "delta": delta, "distance_m": distance})

    root: dict[int, int] = {}
    depth: dict[int, int] = {}
    potential: dict[int, float] = {}
    path_km: dict[int, float] = {}
    parent: dict[int, int] = {}
    for seed in sorted(adjacency):
        if seed in root:
            continue
        root[seed] = seed
        depth[seed] = 0
        potential[seed] = 0.0
        path_km[seed] = 0.0
        parent[seed] = seed
        stack = [seed]
        while stack:
            u = stack.pop()
            for v, signed_delta, distance, _obs_id in adjacency[u]:
                if v in root:
                    continue
                root[v] = seed
                depth[v] = depth[u] + 1
                potential[v] = potential[u] + signed_delta
                path_km[v] = path_km[u] + distance / 1000.0
                parent[v] = u
                stack.append(v)

    levels = max(1, (max(depth.values(), default=0) + 1).bit_length())
    up = [dict(parent)]
    for _level in range(1, levels):
        previous = up[-1]
        up.append({node: previous[previous[node]] for node in parent})

    def lca(a: int, b: int) -> int:
        if depth[a] < depth[b]:
            a, b = b, a
        difference = depth[a] - depth[b]
        bit = 0
        while difference:
            if difference & 1:
                a = up[bit][a]
            difference >>= 1
            bit += 1
        if a == b:
            return a
        for level in range(levels - 1, -1, -1):
            if up[level][a] != up[level][b]:
                a, b = up[level][a], up[level][b]
        return parent[a]

    tol = tolerance or {"mm_per_sqrt_km": 2.0}
    limit = float(tol.get("mm_per_sqrt_km", 2.0)) / 1000.0
    closures = []
    for chord in chords[:max_cycles]:
        a, b = chord["a"], chord["b"]
        if root.get(a) != root.get(b):
            continue
        ancestor = lca(a, b)
        cycle_km = path_km[a] + path_km[b] - 2.0 * path_km[ancestor] + chord["distance_m"] / 1000.0
        misclosure = potential[a] - potential[b] + chord["delta"]
        threshold = limit * math.sqrt(max(cycle_km, 1e-9))
        closures.append(
            {
                "closing_observation_id": chord["id"],
                "from_point_id": a,
                "to_point_id": b,
                "misclosure_m": misclosure,
                "cycle_distance_km": cycle_km,
                "threshold_m": threshold,
                "passed": abs(misclosure) <= threshold,
            }
        )
    return closures


def aggregate_residual_stats(residuals: list[float], weights: np.ndarray) -> dict[str, float]:
    v = np.asarray(residuals, dtype=np.float64)
    if v.size == 0:
        return {"count": 0, "min_m": 0.0, "max_m": 0.0, "mean_m": 0.0, "rms_m": 0.0, "weighted_rms": 0.0}
    return {
        "count": int(v.size),
        "min_m": float(np.min(v)),
        "max_m": float(np.max(v)),
        "mean_m": float(np.mean(v)),
        "rms_m": float(math.sqrt(float(np.mean(v * v)))),
        "weighted_rms": float(math.sqrt(float(weights[: v.size] @ (v * v)) / v.size)) if v.size else 0.0,
    }
