#!/usr/bin/env bash
# 一键生成「技术使用记录」里用到的全部日志（命令 + 退出码 + 输出）。
#
#   cd minimax && bash scripts/record_evidence.sh
#
# 顺序刻意保持「先回到修改前再建索引」，这样第 7 步的上下文里看到的是
# 修改前的 refund.py（缺运费分摊），与验证样例的叙事一致。
# 每一步落盘到 docs/logs/NN_xxx.log，首行是命令，末尾是退出码。

set -u

cd "$(dirname "$0")/.." || exit 1
PY="${PYTHON:-python}"
OUT="docs/logs"
mkdir -p "$OUT"

export PYTHONDONTWRITEBYTECODE=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1

run() {
  local name="$1"
  shift
  {
    echo "\$ $*"
    echo "--- workdir: $(pwd)"
    echo "--- started: $(date -Iseconds)"
  } >"$OUT/$name.log"
  "$@" >>"$OUT/$name.log" 2>&1
  local code=$?
  echo "--- exit_code: $code" >>"$OUT/$name.log"
  printf '%-30s exit=%s\n' "$name" "$code"
  return 0
}

echo "== 1. 环境自检"
run 01_doctor "$PY" -m sddctx doctor

echo "== 2. 回到修改前（目标测试应当失败）"
run 02_demo_reset "$PY" -m sddctx demo reset

echo "== 3. 建表 + 全量导入（规格 v1，索引的是修改前代码）"
run 03_init "$PY" -m sddctx init
run 04_ingest_v1 "$PY" -m sddctx ingest --reset

echo "== 4. 修改前：目标测试失败、回归测试通过"
run 05_pytest_before "$PY" -m pytest -q -p no:cacheprovider demo_project/tests/test_refund_shipping_allocation.py
run 06_verify_before "$PY" -m sddctx verify --task TASK-0007 --phase before
run 07_pytest_regression "$PY" -m pytest -q -p no:cacheprovider demo_project/tests/test_pricing.py demo_project/tests/test_shipping.py demo_project/tests/test_refund_existing.py

echo "== 5. 组装开发上下文并自检"
run 08_context_v1 "$PY" -m sddctx context --task TASK-0007
run 09_check_v1 "$PY" -m sddctx check --task TASK-0007

echo "== 6. 应用代码修改后重跑测试（预期全部通过）"
run 10_demo_apply "$PY" -m sddctx demo apply
run 11_pytest_after "$PY" -m pytest -q -p no:cacheprovider demo_project/tests
run 12_verify_after "$PY" -m sddctx verify --task TASK-0007 --phase after
run 13_ingest_after_patch "$PY" -m sddctx ingest
run 14_context_after_patch "$PY" -m sddctx context --task TASK-0007

echo "== 7. 规格变更：导入 SPEC-003 v1.1 后重新组装上下文"
run 15_ingest_v2 "$PY" -m sddctx ingest --variant v2
run 16_context_v2 "$PY" -m sddctx context --task TASK-0007
run 17_check_v2 "$PY" -m sddctx check --task TASK-0007 --variant v2
run 18_ingest_v1_back "$PY" -m sddctx ingest

echo "== 8. 状态复用：新进程读回 PowerContext 里的任务状态"
run 19_state_show "$PY" -m sddctx state show --task TASK-0007

echo "== 9. 一条命令复跑整条验证样例（评审最省事的入口）"
run 20_demo_sample "$PY" -m sddctx demo sample
run 21_state_after_sample "$PY" -m sddctx state show --task TASK-0007

echo
echo "日志已写入 $OUT/"
