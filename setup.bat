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

REM The "py" launcher (py.exe) is a separate, optional install from
REM python.exe itself - some installs put python.exe on PATH without it.
REM Accept either.
set PYCMD=
where py >nul 2>&1
if not errorlevel 1 set PYCMD=py -3
if not defined PYCMD (
    where python >nul 2>&1
    if not errorlevel 1 set PYCMD=python
)
if not defined PYCMD (
    echo [ERROR] Python not found. Install Python first:
    echo         https://www.python.org/downloads/
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

echo [OK] Docker, Python, and Node.js found.
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
    echo Creating backend virtual environment...
    !PYCMD! -m venv "backend\.venv"
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
    echo         If you're on a very new Python version, this may be a
    echo         real package incompatibility - try installing Python
    echo         3.11 or 3.12 alongside your current version and rerun.
    pause
    exit /b 1
)

echo Installing Playwright's Chromium browser...
"backend\.venv\Scripts\python.exe" -m playwright install chromium
echo.

REM --- backend\.env (secrets - never overwrite if it already exists) ---
REM Generated via temp-file redirection rather than `for /f ('command')`
REM command-capture - that form is fragile with a quoted exe path followed
REM by a quoted -c argument (nested quotes inside the captured command
REM confuse cmd.exe's parser on some Windows/cmd builds) and can fail
REM silently, leaving JWT_SECRET/ENC_KEY empty with no error shown.
if not exist "backend\.env" (
    echo Generating backend\.env with fresh secrets...
    "backend\.venv\Scripts\python.exe" -c "import secrets;print(secrets.token_hex(32))" > "%TEMP%\ps_jwt.tmp"
    "backend\.venv\Scripts\python.exe" -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())" > "%TEMP%\ps_enc.tmp"
    set /p JWT_SECRET=<"%TEMP%\ps_jwt.tmp"
    set /p ENC_KEY=<"%TEMP%\ps_enc.tmp"
    del "%TEMP%\ps_jwt.tmp" "%TEMP%\ps_enc.tmp" >nul 2>&1
    if not defined JWT_SECRET (
        echo [ERROR] Failed to generate JWT_SECRET - see any Python errors above.
        pause
        exit /b 1
    )
    if not defined ENC_KEY (
        echo [ERROR] Failed to generate ENCRYPTION_KEY - see any Python errors above.
        pause
        exit /b 1
    )
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
REM No frontend .env needed - the frontend always calls a relative /api path
REM (see src/lib/api.js) and package.json's "proxy" field handles routing
REM that to the backend during local dev; the built app is same-origin in
REM production, so there's nothing to configure either way.

echo Installing frontend dependencies (this can take a few minutes)...
REM Run the pinned yarn version straight through corepack instead of
REM `corepack enable` + `yarn`. `enable` installs shim files next to
REM node.exe so bare `yarn` works anywhere - if Node is under Program
REM Files that write needs admin rights and fails with EPERM. Invoking
REM `corepack yarn` directly skips that step entirely: no shims, no
REM elevated permissions needed, still runs the exact pinned version.
pushd frontend
call corepack yarn install
set FRONTEND_INSTALL_RESULT=%errorlevel%
popd
if not %FRONTEND_INSTALL_RESULT%==0 (
    echo [ERROR] Frontend dependency install failed - see the output above.
    pause
    exit /b 1
)

echo Building the frontend for production...
REM start.bat serves this build directly from the backend (single origin,
REM single port - see server.py's catch-all route) rather than running the
REM CRA dev server, since that's what an actual tunnel/LAN visitor needs.
pushd frontend
call corepack yarn build
set FRONTEND_BUILD_RESULT=%errorlevel%
popd
if not %FRONTEND_BUILD_RESULT%==0 (
    echo [ERROR] Frontend build failed - see the output above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete!
echo  Run start.bat whenever you want to use the app.
echo ============================================
pause
