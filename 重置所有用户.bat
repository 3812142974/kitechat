@echo off
title KiteChat - Reset all users
cd /d %~dp0
echo [KiteChat] Stopping server on port 8920...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr LISTENING ^| findstr ":8920 "') do taskkill /PID %%p /F >nul 2>nul
ping -n 3 127.0.0.1 >nul

echo [KiteChat] Deleting database (all users, sessions, messages, friends)...
del /q data\kitechat.db data\kitechat.db-wal data\kitechat.db-shm >nul 2>nul

echo [KiteChat] Starting server (fresh database)...
start "KiteChat Server" .venv\Scripts\python.exe run.py
ping -n 6 127.0.0.1 >nul

echo [KiteChat] Restoring AstrBot bridge config (app_ws_url)...
.venv\Scripts\python.exe -c "import sqlite3;c=sqlite3.connect('data/kitechat.db');c.execute(\"INSERT INTO config(key,value) VALUES('app_ws_url','ws://127.0.0.1:6199/ws') ON CONFLICT(key) DO UPDATE SET value=excluded.value\");c.commit();print('app_ws_url restored');print('NEW admin token:',c.execute(\"SELECT value FROM config WHERE key='admin_token'\").fetchone()[0])"

echo.
echo [KiteChat] Done. New admin token printed above.
echo Virtual numbers now start at #1.
pause
