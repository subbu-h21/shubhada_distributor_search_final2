@echo off
cd /d "%~dp0"

echo ============================================
echo  Shubhada Distributor Search - Start
echo ============================================
echo.

if not exist "backend\.venv\Scripts\python.exe" (
    echo [ERROR] Backend isn't set up yet. Run setup.bat first.
    pause
    exit /b 1
)
if not exist "backend\.env" (
    echo [ERROR] backend\.env is missing. Run setup.bat first.
    pause
    exit /b 1
)
if not exist "frontend\node_modules" (
    echo [ERROR] Frontend isn't set up yet. Run setup.bat first.
    pause
    exit /b 1
)
if not exist "frontend\build\index.html" (
    echo [ERROR] Frontend hasn't been built yet. Run setup.bat first.
    pause
    exit /b 1
)

REM --- Make sure Docker Desktop is running ---
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready_start

echo Starting Docker Desktop, please wait...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"

:waitdocker_start
timeout /t 3 >nul
docker info >nul 2>&1
if errorlevel 1 goto waitdocker_start

:docker_ready_start
echo [OK] Docker is ready.

REM --- Start MongoDB ---
echo Starting MongoDB container...
docker start pharmascrape-mongo >nul 2>&1
echo.

REM --- Backend, in its own window. It serves the frontend/build directory
REM directly (see server.py's catch-all route) - single origin, single port,
REM which is what a Cloudflare Tunnel / LAN visitor actually needs to reach.
echo Starting the app on http://localhost:8001 ...
start "PharmaScrape" cmd /k "cd /d "%~dp0backend" && .venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8001"

echo.
echo ============================================
echo  Starting in its own window. Once it says
echo  "Application startup complete", open:
echo.
echo      http://localhost:8001
echo.
echo  Close that window (or Ctrl+C in it) to stop.
echo  Made frontend code changes? They won't show up
echo  here until you rerun setup.bat to rebuild.
echo ============================================
pause
