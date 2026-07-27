@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================
echo  Shubhada Distributor Search - Setup
echo ============================================
echo  This is a one-time setup. Re-running it later
echo  is safe - it skips anything already done.
echo ============================================
echo.

REM --- Check prerequisites ---
where docker >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker not found. Install Docker Desktop first:
    echo         https://www.docker.com/products/docker-desktop/
    pause
    exit /b 1
)

py -3.11 -c "print(1)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11 not found via the "py" launcher.
    echo         Install Python 3.11 first: https://www.python.org/downloads/
    pause
    exit /b 1
)

where node >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js not found. Install Node.js first:
    echo         https://nodejs.org/
    pause
    exit /b 1
)

echo [OK] Docker, Python 3.11, and Node.js found.
echo.

REM --- Make sure Docker Desktop is actually running (needed below) ---
docker info >nul 2>&1
if not errorlevel 1 goto docker_ready_setup

echo Docker Desktop isn't running yet - starting it...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
echo Waiting for Docker to be ready, this can take a minute...

:waitdocker_setup
timeout /t 3 >nul
docker info >nul 2>&1
if errorlevel 1 goto waitdocker_setup

:docker_ready_setup
echo [OK] Docker is ready.
echo.

REM --- Backend virtual environment ---
if not exist "backend\.venv\Scripts\python.exe" (
    echo Creating backend virtual environment - Python 3.11...
    py -3.11 -m venv "backend\.venv"
) else (
    echo Backend virtual environment already exists, skipping.
)

echo Installing backend dependencies (this can take a few minutes)...
if not exist "backend\requirements.local.txt" (
    REM emergentintegrations is a private emergent.sh-only package, not
    REM importable outside their container and unused in this codebase.
    findstr /v /b "emergentintegrations" "backend\requirements.txt" > "backend\requirements.local.txt"
)
"backend\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
"backend\.venv\Scripts\python.exe" -m pip install -r "backend\requirements.local.txt"
if errorlevel 1 (
    echo [ERROR] Backend dependency install failed - see the output above.
    pause
    exit /b 1
)

echo Installing Playwright's Chromium browser...
"backend\.venv\Scripts\python.exe" -m playwright install chromium
echo.

REM --- backend\.env (secrets - never overwrite if it already exists) ---
if not exist "backend\.env" (
    echo Generating backend\.env with fresh secrets...
    for /f %%A in ('"backend\.venv\Scripts\python.exe" -c "import secrets;print(secrets.token_hex(32))"') do set JWT_SECRET=%%A
    for /f %%A in ('"backend\.venv\Scripts\python.exe" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"') do set ENC_KEY=%%A
    (
        echo MONGO_URL=mongodb://localhost:27017
        echo DB_NAME=pharmascrape
        echo JWT_SECRET=!JWT_SECRET!
        echo JWT_EXPIRE_DAYS=30
        echo ENCRYPTION_KEY=!ENC_KEY!
    ) > "backend\.env"
    echo [OK] backend\.env created.
) else (
    echo backend\.env already exists, leaving it alone.
)
echo.

REM --- MongoDB via Docker ---
echo Setting up the MongoDB container...
docker inspect pharmascrape-mongo >nul 2>&1
if errorlevel 1 (
    docker run -d --name pharmascrape-mongo -p 27017:27017 -v pharmascrape-mongo-data:/data/db mongo:7
    echo [OK] MongoDB container created and started.
) else (
    docker start pharmascrape-mongo >nul 2>&1
    echo [OK] MongoDB container already existed - started it.
)
echo.

REM --- Frontend ---
if not exist "frontend\.env" (
    echo REACT_APP_BACKEND_URL=http://localhost:8001> "frontend\.env"
    echo [OK] frontend\.env created.
) else (
    echo frontend\.env already exists, leaving it alone.
)

echo Enabling corepack (fetches the exact pinned yarn version)...
call corepack enable >nul 2>&1

echo Installing frontend dependencies (this can take a few minutes)...
pushd frontend
call yarn install
set FRONTEND_INSTALL_RESULT=%errorlevel%
popd
if not %FRONTEND_INSTALL_RESULT%==0 (
    echo [ERROR] Frontend dependency install failed - see the output above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo  Run start.bat whenever you want to use the app.
echo ============================================
pause
