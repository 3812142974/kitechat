#!/usr/bin/env bash
# KiteChat 服务端启动 (Linux 版)
# 镜像 Windows 的 启动服务端.bat：首次建 .venv (uv 优先, pip 兜底), 之后同步依赖并启动。
# 用法： bash 启动服务端.sh
set -e
cd "$(dirname "$0")"

# 平台化 venv 路径（Linux 是 bin, Windows 是 Scripts）
if [ -x ".venv/bin/python" ]; then
  VP=".venv/bin/python"
else
  VP=".venv/Scripts/python.exe"
fi

# Linux 下缓存也放项目内（用户偏好：不占系统盘）
export UV_CACHE_DIR="$PWD/tools/uv-cache"
export PIP_CACHE_DIR="$PWD/tools/pip-cache"
export PIP_DISABLE_PIP_VERSION_CHECK=1

echo "[KiteChat] 平台: $(uname -s)  |  cwd: $PWD"

# ---- venv 已存在：对齐依赖后启动 ----
if [ -x "$VP" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "[KiteChat] uv available - aligning dependencies..."
    uv pip install --python "$VP" -r requirements.txt >/dev/null 2>&1
  else
    echo "[KiteChat] uv not found - using existing .venv directly."
  fi
  echo "[KiteChat] Starting server..."
  exec "$VP" run.py
  exit 0
fi

# ---- 首次运行：创建 venv (uv 优先, pip 兜底) ----
echo "[KiteChat] First run: creating virtual environment (.venv)..."
if command -v uv >/dev/null 2>&1; then
  echo "[KiteChat] uv detected - using uv (recommended)..."
  uv venv .venv --python 3.11
  "$VP" -m ensurepip >/dev/null 2>&1 || true
  uv pip install --python "$VP" -r requirements.txt
else
  echo "[KiteChat] uv not found - falling back to Python builtin venv + pip..."
  PY="$(command -v python3 || command -v python)"
  [ -n "$PY" ] || { echo "[KiteChat] 错误: 未找到 Python 3.11+。请先安装 Python 或 uv。"; exit 1; }
  "$PY" -m venv .venv
  "$VP" -m ensurepip >/dev/null 2>&1 || true
  "$VP" -m pip install -r requirements.txt
fi

if [ ! -x "$VP" ]; then
  echo "[KiteChat] Setup failed. Please install Python 3.11+ (or uv: pip install uv)"
  exit 1
fi

echo "[KiteChat] Starting server..."
exec "$VP" run.py
