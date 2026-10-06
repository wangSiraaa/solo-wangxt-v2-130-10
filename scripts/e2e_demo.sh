#!/usr/bin/env bash
set -euo pipefail

BASE=${BASE:-http://localhost:8000}
curl -fsS "$BASE/health" >/dev/null

project=$(curl -fsS -X POST "$BASE/api/projects" -H 'Content-Type: application/json' \
  -d '{"code":"DEMO-2026","name":"验收演示水准网"}')
project_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' <<<"$project")

curl -fsS -X POST "$BASE/api/projects/$project_id/import" -H 'Content-Type: application/json' -d '{
  "points": [
    {"code":"BM-A","name":"基准A"},{"code":"P1","name":"测点1"},{"code":"P2","name":"测点2"},{"code":"BM-B","name":"基准B"}
  ],
  "observations": [
    {"line_code":"L1","from_code":"BM-A","to_code":"P1","observed_delta_m":1.001,"distance_m":1000},
    {"line_code":"L2","from_code":"P1","to_code":"P2","observed_delta_m":1.000,"distance_m":1000},
    {"line_code":"L3","from_code":"P2","to_code":"BM-B","observed_delta_m":1.002,"distance_m":1000},
    {"line_code":"L4","from_code":"BM-A","to_code":"BM-B","observed_delta_m":3.004,"distance_m":3000}
  ]
}'

curl -fsS -X POST "$BASE/api/projects/$project_id/datums?point_code=BM-A&elevation_m=100&sigma_m=0.0001"
curl -fsS -X POST "$BASE/api/projects/$project_id/datums?point_code=BM-B&elevation_m=103.001&sigma_m=0.0001"
curl -fsS -X POST "$BASE/api/projects/$project_id/weight-rules?name=mm-sqrt-km" \
  -H 'Content-Type: application/json' \
  -d '{"rule":{"method":"millimeter_sqrt_km","c_km":1.0,"base_sigma_m":0.001}}'

job=$(curl -fsS -X POST "$BASE/api/projects/$project_id/jobs")
echo "$job"
job_id=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])' <<<"$job")

for _ in $(seq 1 30); do
  detail=$(curl -fsS "$BASE/api/jobs/$job_id")
  status=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' <<<"$detail")
  echo "status=$status"
  [[ "$status" == "completed" || "$status" == "failed" || "$status" == "audited_only" ]] && break
  sleep 1
done
curl -fsS "$BASE/api/jobs/$job_id" | python3 -m json.tool
curl -fsS "$BASE/api/jobs/$job_id/residuals?limit=20" | python3 -m json.tool
