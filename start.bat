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

REM --- Backend, in its own window ---
echo Starting backend on http://localhost:8001 ...
start "PharmaScrape Backend" cmd /k "cd /d "%~dp0backend" && .venv\Scripts\python.exe -m uvicorn server:app --host 127.0.0.1 --port 8001"

REM --- Frontend, in its own window ---
echo Starting frontend on http://localhost:3000 ...
start "PharmaScrape Frontend" cmd /k "cd /d "%~dp0frontend" && yarn start"

echo.
echo ============================================
echo  Both servers are starting in their own windows.
echo  Wait for the frontend window to say
echo  "Compiled successfully", then open:
echo.
echo      http://localhost:3000
echo.
echo  Close those windows (or Ctrl+C in each) to stop.
echo ============================================
pause
