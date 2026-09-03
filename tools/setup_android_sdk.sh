#!/usr/bin/env bash
# KiteChat Android 环境一键准备 (薄壳 -> 转发给 Python 安装器)
#
# 核心逻辑在 tools/setup_android_sdk.py：跨平台、无 Git/无 cmd 依赖，
# 自动检测平台(Windows/Linux)并按平台下载 SDK/JDK 到项目内。
# 本 .sh 仅为 Linux 用户提供一个熟悉的入口；Windows 用户可用 setup_android_sdk.bat。
#
# 用法： bash tools/setup_android_sdk.sh   （等价于 python tools/setup_android_sdk.py）
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

exec "$PY" "$SCRIPT_DIR/setup_android_sdk.py" "$@"
