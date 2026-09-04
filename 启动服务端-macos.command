#!/bin/bash
# KiteChat 服务端一键启动脚本 (macOS)
# 双击 .command 文件即可运行

cd "$(dirname "$0")"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}=== KiteChat Server Launcher (macOS) ===${NC}"
echo ""

# 检查 Python3
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
elif [ -f "/usr/local/bin/python3" ]; then
    PYTHON="/usr/local/bin/python3"
elif [ -f "/opt/homebrew/bin/python3" ]; then
    PYTHON="/opt/homebrew/bin/python3"
else
    echo -e "${RED}Python 3 未安装，请先安装:${NC}"
    echo "  brew install python3"
    echo "  或者从 https://www.python.org/downloads/ 下载安装"
    exit 1
fi

echo -e "${GREEN}Python: ${NC}$($PYTHON --version)"

# 检查依赖
if ! $PYTHON -c "import aiohttp" 2>/dev/null; then
    echo -e "${YELLOW}正在安装依赖...${NC}"
    $PYTHON -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
fi

# 检查 SQLite
if ! $PYTHON -c "import sqlite3" 2>/dev/null; then
    echo -e "${RED}SQLite3 模块缺失，请重新安装 Python${NC}"
    exit 1
fi

# 检查端口占用
PORT=${PORT:-8920}
if lsof -i :$PORT -t &>/dev/null; then
    echo -e "${YELLOW}端口 $PORT 已被占用:${NC}"
    lsof -i :$PORT | head -5
    echo ""
    read -p "是否停止占用进程？(y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        kill -9 $(lsof -i :$PORT -t) 2>/dev/null
        sleep 1
    else
        echo "请手动释放端口后重试"
        exit 1
    fi
fi

# 启动服务端
echo -e "${GREEN}正在启动 KiteChat Server...${NC}"
echo -e "HTTP: http://localhost:$PORT"
echo -e "HTTPS: https://localhost:8921"
echo -e "WebUI: http://localhost:$PORT"
echo ""
echo -e "${YELLOW}按 Ctrl+C 停止服务${NC}"
echo ""

$PYTHON run.py
