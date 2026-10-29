#!/usr/bin/env bash
# 在 Linux 容器里跑量化，避免污染本机 Python 环境。
# 用法: bash tools/quantize.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MSYS_NO_PATHCONV=1 docker run --rm -v "$ROOT:/app" -w /app python:3.12-slim bash -c \
  "pip install -q onnxruntime==1.20.1 && python tools/quantize.py"
