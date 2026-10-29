#!/usr/bin/env bash
# 下载 bge-small-zh-v1.5 的 ONNX 权重与分词器文件，并可选做 INT8 量化。
#
#   bash tools/fetch_model.sh              # 只下 INT8 之外的必需文件 + FP32 原始权重
#   bash tools/fetch_model.sh --quantize   # 下载 FP32 后立刻量化出 model_int8.onnx
#   bash tools/fetch_model.sh --with-fp16  # 额外下载官方 FP16 权重（WebGPU 对照组）
#
# 说明：浏览器推理只需要 model_int8.onnx；FP32/FP16 仅用于体积与质量对照。
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="Xenova/bge-small-zh-v1.5"
DIR="models/bge-small-zh-v1.5"
BASE="https://huggingface.co/${REPO}/resolve/main"
MIRROR="${HF_MIRROR:-}"   # 例如 https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main

get() { # get <relative-path>
  local rel="$1" url out
  out="${DIR}/${rel}"
  [ -s "$out" ] && { echo "skip $rel"; return; }
  mkdir -p "$(dirname "$out")"
  if [ -n "$MIRROR" ]; then url="${MIRROR}/${rel}"; else url="${BASE}/${rel}"; fi
  echo "↓ $rel"
  curl -fL --retry 3 -o "$out" "$url"
}

for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json vocab.txt quantize_config.json; do
  get "$f"
done

if [ ! -s "$DIR/onnx/model_int8.onnx" ]; then
  get "onnx/model_int8.onnx" || true
fi

if [ "${1:-}" = "--quantize" ]; then
  get "onnx/model.onnx"
  echo "量化 FP32 -> INT8（Docker 内的 onnxruntime）"
  bash tools/quantize.sh
fi

if [ "${1:-}" = "--with-fp16" ] || [ "${2:-}" = "--with-fp16" ]; then
  get "onnx/model_fp16.onnx"
fi

echo "模型目录：$DIR"
ls -lh "$DIR/onnx" || true
