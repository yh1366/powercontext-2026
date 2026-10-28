#!/usr/bin/env bash
set -e

echo "============================================"
echo "  Memory Tavern - Quick Start"
echo "============================================"
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "[1/3] Checking PowerContext..."
if ! curl -sf http://127.0.0.1:8000/health/live > /dev/null 2>&1; then
    echo "  ERROR: PowerContext is not running at http://127.0.0.1:8000"
    echo "  Start it with: powercontext server run"
    exit 1
fi
echo "  PowerContext is running."

echo ""
echo "[2/3] Initializing world data..."
python3 "$SCRIPT_DIR/scripts/init_powercontext.py"

echo ""
echo "[3/3] Starting backend service..."
echo "  Backend will be available at http://127.0.0.1:8600"
echo "  Press Ctrl+C to stop."
echo ""
cd "$SCRIPT_DIR/backend"
pip install -q -r requirements.txt
python3 -m uvicorn app:app --host 0.0.0.0 --port 8600
