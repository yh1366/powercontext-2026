#!/usr/bin/env bash
# 复现全部性能实验（需要先启动 seekdb + 应用，且已安装 playwright/chromium）。
#   bash tools/bench-all.sh
# 输出：docs/bench-*.json 与 docs/images/*.png
set -euo pipefail
cd "$(dirname "$0")/.."
BASE="${BASE:-http://127.0.0.1:5000}"

run() { # run <label> <configs> <batch> [threads]
  local label="$1" configs="$2" batch="$3" threads="${4:-}"
  echo "== $label =="
  BASE="$BASE" CONFIGS="$configs" BATCH="$batch" THREADS="$threads" OUT="bench-$label.json" node tools/bench.mjs
}

# 1) 主配置：INT8 + WASM（自动线程），重复两轮取中位数
run int8-b16-r1 "wasm:int8" 16
run int8-b16-r2 "wasm:int8" 16
# 2) 批处理对照：batch=1
run int8-b1 "wasm:int8" 1
# 3) WASM 单线程对照
run int8-b16-t1 "wasm:int8" 16 1
# 4) FP32 基线（需先 bash tools/fetch_model.sh）
run fp32-b16 "wasm:fp32" 16
# 5) WebGPU（有 GPU 的机器才可用）
if [ "${WITH_WEBGPU:-1}" = "1" ]; then
  run int8-webgpu "webgpu:int8" 16 || echo "WebGPU 不可用，跳过"
fi
