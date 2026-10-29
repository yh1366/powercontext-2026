#!/usr/bin/env bash
# 一键启动：seekdb（Docker）+ 应用（Node）。
# 用法：
#   bash run.sh              # 默认 5000 端口，seekdb 容器端口 2881
#   PORT=8080 bash run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-5000}"
SEEKDB_PORT="${SEEKDB_PORT:-2881}"
CONTAINER="${SEEKDB_CONTAINER:-seekdb-edge-kb}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "缺少依赖：$1"; exit 1; }; }
need node
need npm

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 docker。请手动启动 seekdb 后运行：SEEKDB_HOST=<host> SEEKDB_PORT=<port> npm start"
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "启动已存在的 seekdb 容器 $CONTAINER"
    docker start "$CONTAINER" >/dev/null
  else
    echo "创建 seekdb 容器 $CONTAINER（映射 $SEEKDB_PORT -> 2881）"
    docker run -d --name "$CONTAINER" -p "${SEEKDB_PORT}:2881" oceanbase/seekdb:latest >/dev/null
  fi
fi

echo -n "等待 seekdb 就绪"
for i in $(seq 1 60); do
  if docker logs "$CONTAINER" 2>&1 | grep -q "Initialization complete"; then echo " ✓"; break; fi
  echo -n "."
  sleep 2
done

[ -d node_modules ] || npm install
[ -f models/bge-small-zh-v1.5/onnx/model_int8.onnx ] || bash tools/fetch_model.sh --quantize

echo "打开 http://localhost:${PORT}"
SEEKDB_HOST=127.0.0.1 SEEKDB_PORT="$SEEKDB_PORT" PORT="$PORT" node server/index.mjs
