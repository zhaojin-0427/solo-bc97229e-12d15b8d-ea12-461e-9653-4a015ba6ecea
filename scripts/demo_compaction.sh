#!/usr/bin/env bash
# 历史压缩端到端演示：折叠基线 + 保留口径不变 + 过期报错 + 幂等 + 边界查询。
#
# 用法：
#   docker compose up -d --build        # 先启动服务
#   ./scripts/demo_compaction.sh        # 本机演示（默认 BASE=http://localhost:8000）
#
# 脚本自包含：建议在空库上运行（docker compose down -v 后重新 up）。
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"

pretty() {
  if command -v jq >/dev/null 2>&1; then jq .;
  elif command -v python3 >/dev/null 2>&1; then python3 -m json.tool;
  else cat; fi
}

post() { # $1=path  $2=json body
  curl -sS -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

code_post() { # 仅打印 HTTP 状态码  $1=path $2=json
  curl -sS -o /tmp/demo_compaction_resp.json -w '%{http_code}' \
    -X POST "${BASE}$1" -H 'Content-Type: application/json' -d "$2"
}

echo "==================================================================="
echo "0. 健康检查  GET /health"
echo "==================================================================="
curl -sS "${BASE}/health" | pretty
echo

echo "==================================================================="
echo "1. 构造三个修订（含跨批覆盖、空档、半开边界）"
echo "   rev1: E1 [01-01,07-01) CC-OLD；E2 [01-01,03-01) CC-E2"
echo "   rev2: E1 [04-01,07-01) CC-MID（覆盖 4 月起）"
echo "   rev3: E1 [06-01,07-01) CC-NEW（将作为保留边界 B=3）"
echo "==================================================================="
post /api/revisions '{
  "note": "HR 初始归属",
  "entries": [
    {"employee_id": "E1", "cost_center": "CC-OLD",
     "effective_from": "2026-01-01", "effective_to": "2026-07-01"},
    {"employee_id": "E2", "cost_center": "CC-E2",
     "effective_from": "2026-01-01", "effective_to": "2026-03-01"}
  ]
}' | pretty
post /api/revisions '{
  "note": "HR 四月调岗补录",
  "entries": [
    {"employee_id": "E1", "cost_center": "CC-MID",
     "effective_from": "2026-04-01", "effective_to": "2026-07-01"}
  ]
}' | pretty
post /api/revisions '{
  "note": "HR 六月调岗补录",
  "entries": [
    {"employee_id": "E1", "cost_center": "CC-NEW",
     "effective_from": "2026-06-01", "effective_to": "2026-07-01"}
  ]
}' | pretty
echo

echo "==================================================================="
echo "2. 压缩前留档：rev3 口径查询（注意命中修订号与 7/1 空档）"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 3,
  "records": [
    {"employee_id": "E1", "work_date": "2026-02-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-06-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-07-01", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "3. 历史压缩：POST /api/compactions {retain_from_revision: 3}"
echo "   基线只读计算（不持写锁）-> 单事务原子切换 -> 删除旧修订/原始区间"
echo "==================================================================="
post /api/compactions '{"retain_from_revision": 3}' | pretty
echo

echo "==================================================================="
echo "4. 幂等：同一 B=3 再请求一次（idempotent_replay=true，数据不再变化）"
echo "==================================================================="
post /api/compactions '{"retain_from_revision": 3}' | pretty
echo

echo "==================================================================="
echo "5. 保留边界与累计删除行数  GET /api/compactions/boundary"
echo "==================================================================="
curl -sS "${BASE}/api/compactions/boundary" | pretty
echo

echo "==================================================================="
echo "6. 持久基线审计  POST /api/baseline/segments"
echo "   每条基线行带 source_revision_id（折叠前真正命中的修订号）"
echo "==================================================================="
post /api/baseline/segments '{}' | pretty
echo

echo "==================================================================="
echo "7. 保留口径不变：同样的 rev3 查询，结果与第 2 步完全一致"
echo "==================================================================="
post /api/attribution/query '{
  "revision_id": 3,
  "records": [
    {"employee_id": "E1", "work_date": "2026-02-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-05-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-06-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-07-01", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "8. 过期口径明确报错：rev1 查询 -> HTTP 410 REVISION_EXPIRED"
echo "==================================================================="
echo "HTTP $(code_post /api/attribution/query '{
  \"revision_id\": 1,
  \"records\": [{\"employee_id\": \"E1\", \"work_date\": \"2026-02-15\", \"hours\": 8}]
}')"
pretty < /tmp/demo_compaction_resp.json
echo

echo "==================================================================="
echo "9. 原始区间审计：保留修订 rev3 可审计；过期修订 rev1 -> 410"
echo "==================================================================="
post /api/revisions/3/segments '{}' | pretty
echo "HTTP $(code_post /api/revisions/1/segments '{}')"
pretty < /tmp/demo_compaction_resp.json
echo

echo "==================================================================="
echo "10. 压缩后继续补录：rev4 正常提交，修订号继续递增"
echo "==================================================================="
post /api/revisions '{
  "note": "压缩后的新补录",
  "entries": [
    {"employee_id": "E1", "cost_center": "CC-AFTER",
     "effective_from": "2026-10-01", "effective_to": "2026-11-01"}
  ]
}' | pretty
post /api/attribution/query '{
  "revision_id": 4,
  "records": [
    {"employee_id": "E1", "work_date": "2026-10-15", "hours": 8},
    {"employee_id": "E1", "work_date": "2026-02-15", "hours": 8}
  ]
}' | pretty
echo

echo "==================================================================="
echo "11. 边界不倒退：请求更早的 B=1 -> HTTP 410"
echo "==================================================================="
echo "HTTP $(code_post /api/compactions '{\"retain_from_revision\": 1}')"
pretty < /tmp/demo_compaction_resp.json
echo

echo "==================================================================="
echo "12. 压缩审计流水  GET /api/compactions"
echo "==================================================================="
curl -sS "${BASE}/api/compactions" | pretty
echo
echo "演示完成。交互式文档：${BASE}/docs"
