#!/usr/bin/env bash
# KiteChat 重置所有用户 (Linux 版)
# 镜像 Windows 的 重置所有用户.bat：停服务 -> 删数据库 -> 起服务 -> 恢复 AstrBot 桥接配置。
# 用法： bash 重置所有用户.sh
set -e
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
  VP=".venv/bin/python"
else
  VP=".venv/Scripts/python.exe"
fi

echo "[KiteChat] Stopping server on port 8920..."
# 找出监听 8920 的 PID 并终止 (Linux: lsof/ss)
if command -v lsof >/dev/null 2>&1; then
  PIDS=$(lsof -ti :8920 2>/dev/null || true)
elif command -v ss >/dev/null 2>&1; then
  PIDS=$(ss -tlnp 2>/dev/null | grep ':8920' | grep -oE 'pid=[0-9]+' | grep -oE '[0-9]+' || true)
else
  PIDS=""
fi
if [ -n "$PIDS" ]; then
  for p in $PIDS; do kill "$p" 2>/dev/null || true; done
  echo "[KiteChat] Killed PID(s): $PIDS"
else
  echo "[KiteChat] No server running on 8920."
fi
sleep 2

echo "[KiteChat] Deleting database (all users, sessions, messages, friends)..."
rm -f data/kitechat.db data/kitechat.db-wal data/kitechat.db-shm

echo "[KiteChat] Starting server (fresh database)..."
"$VP" run.py &
sleep 5

echo "[KiteChat] Restoring AstrBot bridge config (app_ws_url)..."
"$VP" -c "import sqlite3;c=sqlite3.connect('data/kitechat.db');c.execute(\"INSERT INTO config(key,value) VALUES('app_ws_url','ws://127.0.0.1:6199/ws') ON CONFLICT(key) DO UPDATE SET value=excluded.value\");c.commit();print('app_ws_url restored');print('NEW admin token:',c.execute(\"SELECT value FROM config WHERE key='admin_token'\").fetchone()[0])"

echo ""
echo "[KiteChat] Done. New admin token printed above."
echo "Virtual numbers now start at #1."
