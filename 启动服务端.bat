@echo off
title KiteChat Server
cd /d %~dp0
set "UV_CACHE_DIR=%~dp0tools\uv-cache"
set "PIP_CACHE_DIR=%~dp0tools\pip-cache"
set PIP_DISABLE_PIP_VERSION_CHECK=1

REM ---- venv already exists: align deps then run ----
if exist .venv\Scripts\python.exe goto sync_deps

REM ---- first run: create venv (uv first, pip fallback) ----
echo [KiteChat] First run: creating virtual environment (.venv)...
where uv >nul 2>nul
if %errorlevel%==0 goto setup_uv
goto setup_std

:setup_uv
echo [KiteChat] uv detected - using uv (recommended)...
uv venv .venv --python 3.11
if errorlevel 1 goto setup_fail
.venv\Scripts\python.exe -m ensurepip >nul 2>nul
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
if errorlevel 1 goto setup_fail
goto run

:setup_std
echo [KiteChat] uv not found - falling back to Python builtin venv + pip...
where python >nul 2>nul
if %errorlevel%==0 (
    python -m venv .venv
) else (
    py -3 -m venv .venv
)
if not exist .venv\Scripts\python.exe goto setup_fail
.venv\Scripts\python.exe -m ensurepip >nul 2>nul
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto setup_fail
goto run

:sync_deps
REM ---- venv exists: align deps if uv is available, then run ----
where uv >nul 2>nul
if %errorlevel%==0 (
    echo [KiteChat] uv available - aligning dependencies...
    uv pip install --python .venv\Scripts\python.exe -r requirements.txt >nul 2>nul
) else (
    echo [KiteChat] uv not found - using existing .venv directly.
)
goto run

:setup_fail
echo [KiteChat] Setup failed. Please install Python 3.11+ (or uv: pip install uv)
pause
exit /b 1

:run
echo [KiteChat] Starting server...
.venv\Scripts\python.exe run.py
pause
