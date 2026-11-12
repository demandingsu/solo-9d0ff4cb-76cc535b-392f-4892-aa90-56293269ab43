#!/usr/bin/env bash
# 离线演示脚本：启动本地静态报价站与抄表站（仅需 Python 标准库）并调用 API。
# 前置：API 已在 8000 端口运行
#   uvicorn app.main:app --host 0.0.0.0 --port 8000
# 或直接使用 docker compose up --build（报价站 quote-site:80、抄表站 meter-site:80）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QUOTE_PORT="${QUOTE_SITE_PORT:-8080}"
METER_PORT="${METER_SITE_PORT:-8081}"
API_BASE="${API_BASE:-http://127.0.0.1:8000/api/v1}"

python3 -m http.server "$QUOTE_PORT" --directory "$ROOT/quote-site" >/tmp/quote-site.log 2>&1 &
QUOTE_PID=$!
python3 -m http.server "$METER_PORT" --directory "$ROOT/meter-site" >/tmp/meter-site.log 2>&1 &
METER_PID=$!
trap 'kill "$QUOTE_PID" "$METER_PID" 2>/dev/null || true' EXIT
sleep 1

QUOTE_PORT="$QUOTE_PORT" METER_PORT="$METER_PORT" API_BASE="$API_BASE" python3 - "$ROOT/demo/request.json" "$ROOT/demo/meter-request.json" <<'PY'
import json, os, sys, urllib.request

quote_port = os.environ["QUOTE_PORT"]
meter_port = os.environ["METER_PORT"]
api_base = os.environ["API_BASE"]


def post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{api_base}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as resp:
        return json.loads(resp.read())


# 1) 抄表整理：累计读数 -> 逐月用量（演示站含换表、异常与 404 分页）
meter_payload = json.load(open(sys.argv[2], encoding="utf-8"))
meter_payload["index_url"] = f"http://127.0.0.1:{meter_port}/index.html"
meter_result = post("/meter-usage", meter_payload)
print("=== 抄表整理 /api/v1/meter-usage ===")
print("comparable:", meter_result["comparable"])
print("unavailable_months:", meter_result["unavailable_months"])
print("failures:", json.dumps(meter_result["failures"], ensure_ascii=False))
print("可用月份 monthly_usage 条数:", len(meter_result["monthly_usage"]))

# 2) 比价：示例请求（12 个月预测）直接调用
compare_payload = json.load(open(sys.argv[1], encoding="utf-8"))
compare_payload["list_url"] = f"http://127.0.0.1:{quote_port}/index.html"
compare_result = post("/compare-plans", compare_payload)
print("\n=== 套餐比价 /api/v1/compare-plans ===")
print("最低价套餐:",
      compare_result["conclusion"]["lowest_plan_id"],
      compare_result["conclusion"]["lowest_plan_name"],
      compare_result["conclusion"]["lowest_annual_total_cny"])
PY
