#!/usr/bin/env python3
"""Generate a 100,000-level-segment pressure dataset.

The graph is mostly linear with redundant chords, allowing a sparse 100k-row normal
matrix and thousands of deterministic closure checks.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--observations", type=int, default=100_000)
    parser.add_argument("--points", type=int, default=55_000)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--output", type=Path, default=Path("scripts/pressure_100k.json"))
    args = parser.parse_args()
    random.seed(args.seed)

    points = [{"code": f"P{i:06d}", "name": f"测点{i}"} for i in range(args.points)]
    observations = []
    true_elevation = [100.0 + i * 0.003 + random.gauss(0, 0.00002) for i in range(args.points)]
    for i in range(args.observations):
        if i < args.points - 1:
            a, b = i, i + 1
        else:
            # Redundant observations and short chords create loops.
            a = random.randrange(args.points - 5)
            b = min(args.points - 1, a + random.randrange(2, 6))
        delta = true_elevation[b] - true_elevation[a] + random.gauss(0, 0.001)
        observations.append(
            {
                "line_code": f"L{i:06d}",
                "from_code": points[a]["code"],
                "to_code": points[b]["code"],
                "observed_delta_m": round(delta, 6),
                "distance_m": round((b - a) * random.uniform(180, 420), 3),
                "direction": "forward",
                "pair_group": None,
                "weight_override": None,
            }
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"points": points, "observations": observations}, ensure_ascii=False))
    print(f"wrote {args.output}: {len(points)} points, {len(observations)} observations")


if __name__ == "__main__":
    main()
