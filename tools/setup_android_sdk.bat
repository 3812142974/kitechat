@echo off
REM KiteChat Android env one-click setup (Windows wrapper -> Python installer)
REM Core logic lives in tools\setup_android_sdk.py (cross-platform, no Git).
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0setup_android_sdk.py"
pause
