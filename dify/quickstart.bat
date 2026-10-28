@echo off
echo ============================================
echo   Memory Tavern - Quick Start
echo ============================================
echo.

echo [1/3] Checking PowerContext...
curl -s http://127.0.0.1:8000/health/live >nul 2>&1
if %errorlevel% neq 0 (
    echo   ERROR: PowerContext is not running at http://127.0.0.1:8000
    echo   Start it with: powercontext server run
    pause
    exit /b 1
)
echo   PowerContext is running.

echo.
echo [2/3] Initializing world data...
python "%~dp0scripts\init_powercontext.py"
if %errorlevel% neq 0 (
    echo   ERROR: Initialization failed.
    pause
    exit /b 1
)

echo.
echo [3/3] Starting backend service...
echo   Backend will be available at http://127.0.0.1:8600
echo   Press Ctrl+C to stop.
echo.
cd /d "%~dp0backend"
pip install -q -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8600
