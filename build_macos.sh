#!/bin/bash
# KiteChat macOS 安装包构建脚本
# 在 Mac 上运行: bash build_macos.sh
# 需要: Python 3.11+, pip3, gh CLI (可选,用于上传 release)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="0.0.2"
OUT_DIR="$SCRIPT_DIR/dist"
INSTALLER="$OUT_DIR/KiteChat-macOS-Installer-$VERSION"

echo "=== KiteChat macOS 安装包构建 ==="
echo "版本: $VERSION"
echo ""

# 检查 Python
if ! command -v python3 &>/dev/null; then
    echo "❌ Python3 未安装"
    exit 1
fi
echo "✓ Python: $(python3 --version)"

# 检查 pip
if ! python3 -m pip --version &>/dev/null; then
    echo "❌ pip 未安装"
    exit 1
fi
echo "✓ pip: $(python3 -m pip --version | head -1)"

# 创建输出目录
mkdir -p "$OUT_DIR"

# 打包安装器
echo ""
echo "正在打包 macOS 安装器..."

# 方案1: 用 zip 打包(简单,不需要额外工具)
INSTALLER_DIR="$OUT_DIR/KiteChat-macOS-Installer"
rm -rf "$INSTALLER_DIR"
mkdir -p "$INSTALLER_DIR"

# 复制服务端源码
cp -R "$SCRIPT_DIR/server" "$INSTALLER_DIR/"
cp "$SCRIPT_DIR/run.py" "$INSTALLER_DIR/"
cp "$SCRIPT_DIR/requirements.txt" "$INSTALLER_DIR/"
cp -R "$SCRIPT_DIR/client" "$INSTALLER_DIR/" 2>/dev/null || true

# 复制 macOS 安装器脚本
cp "$SCRIPT_DIR/tools/macos_installer.py" "$INSTALLER_DIR/"

# 生成 .command 启动脚本
cat > "$INSTALLER_DIR/启动服务端.command" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
python3 run.py
EOF
chmod +x "$INSTALLER_DIR/启动服务端.command"

# 生成 .command 重置脚本
cat > "$INSTALLER_DIR/重置所有用户.command" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
echo "警告: 将删除所有用户数据!"
read -p "确认重置? (yes/no): " confirm
if [ "$confirm" = "yes" ]; then
    rm -f data/kitechat.db
    echo "重置完成"
else
    echo "已取消"
fi
EOF
chmod +x "$INSTALLER_DIR/重置所有用户.command"

# 创建 zip
cd "$OUT_DIR"
zip -r "KiteChat-macOS-Installer-$VERSION.zip" "KiteChat-macOS-Installer"
cd "$SCRIPT_DIR"

echo ""
echo "✓ 安装包已生成: $OUT_DIR/KiteChat-macOS-Installer-$VERSION.zip"
echo ""

# 上传到 GitHub Release (需要 gh CLI)
if command -v gh &>/dev/null; then
    echo "检测到 gh CLI,是否上传到 GitHub Release v$VERSION? (y/n)"
    read -p "> " upload
    if [ "$upload" = "y" ] || [ "$upload" = "Y" ]; then
        cd "$SCRIPT_DIR"
        gh release upload "v$VERSION" "$OUT_DIR/KiteChat-macOS-Installer-$VERSION.zip" --clobber
        echo "✓ 已上传到 GitHub Release"
    fi
else
    echo "未检测到 gh CLI,请手动上传:"
    echo "  gh release upload v$VERSION $OUT_DIR/KiteChat-macOS-Installer-$VERSION.zip --clobber"
fi

echo ""
echo "=== 构建完成 ==="
echo ""
echo "安装方式:"
echo "  1. 解压 KiteChat-macOS-Installer-$VERSION.zip"
echo "  2. 双击 macos_installer.py 运行安装器"
echo "  或者:"
echo "  1. 直接双击 '启动服务端.command'"
