#!/bin/bash
# KiteChat 重置所有用户 (macOS)
# 会删除所有用户数据和会话

cd "$(dirname "$0")"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${RED}=== KiteChat 用户数据重置 ===${NC}"
echo ""
echo -e "${YELLOW}警告: 此操作将删除所有用户数据、会话和消息！${NC}"
echo ""
read -p "确认重置？(输入 yes 确认): " confirm

if [ "$confirm" != "yes" ]; then
    echo "已取消"
    exit 0
fi

# 找 Python
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo -e "${RED}Python 未安装${NC}"
    exit 1
fi

# 删除数据库
if [ -f "data/kitechat.db" ]; then
    rm -f "data/kitechat.db"
    echo -e "${GREEN}已删除数据库${NC}"
fi

# 删除配置
if [ -f "data/config.bin" ]; then
    rm -f "data/config.bin"
    echo -e "${GREEN}已删除配置${NC}"
fi

echo -e "${GREEN}重置完成，请重启服务端${NC}"
