#!/usr/bin/env bash
# Android SDK / JDK 环境自动检测与引导安装脚本（通用版，无本机硬编码）
# 用途：
#   1. 自动检测已安装的 Android SDK 与 JDK 位置
#   2. 未检测到时，给出官网下载链接，引导用户安装
#   3. 检测结果写入 Android 构建需要的配置（local.properties / ANDROID_HOME / JAVA_HOME）
set -e

# ---------- 1. 检测 Android SDK ----------
detect_sdk() {
  # 优先读环境变量
  for var in ANDROID_HOME ANDROID_SDK_ROOT; do
    if [ -n "${!var}" ] && [ -d "${!var}" ]; then
      echo "${!var}"
      return 0
    fi
  done
  # 常见安装位置（无硬编码特定盘符，逐个探测）
  for p in \
    "$LOCALAPPDATA/Android/Sdk" \
    "$HOME/Android/Sdk" \
    "/c/Android/Sdk" \
    "/d/Android/Sdk" \
    "/c/Users/$USER/AppData/Local/Android/Sdk"; do
    if [ -d "$p" ]; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

# ---------- 2. 检测 JDK ----------
detect_jdk() {
  # 优先环境变量
  if [ -n "$JAVA_HOME" ] && [ -d "$JAVA_HOME" ]; then
    echo "$JAVA_HOME"
    return 0
  fi
  # 命令可用则解析
  if command -v java >/dev/null 2>&1; then
    # 尝试从 java 可执行文件反推 JAVA_HOME
    jbin="$(command -v java)"
    if [ -x "$(dirname "$jbin")" ]; then
      echo "$(cd "$(dirname "$(dirname "$jbin")")" && pwd)"
      return 0
    fi
  fi
  # 常见 JDK 位置（探测）
  for p in "$LOCALAPPDATA/Programs"/*/bin \
    /c/Program\ Files/Java/* \
    /d/Program\ Files/Java/*; do
    if [ -d "$p" ]; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

SDK_ROOT="$(detect_sdk || true)"
JAVA_ROOT="$(detect_jdk || true)"

echo "=============================================="
echo " Android SDK / JDK 环境检测"
echo "=============================================="
if [ -n "$SDK_ROOT" ]; then
  echo "[✓] Android SDK : $SDK_ROOT"
else
  echo "[✗] 未检测到 Android SDK"
  echo "    请安装 Android 命令行工具："
  echo "      https://developer.android.com/studio#command-line-tools-only"
  echo "    安装后设置环境变量 ANDROID_HOME，或运行："
  echo "      export ANDROID_HOME=<你的sdk路径>"
fi

if [ -n "$JAVA_ROOT" ]; then
  echo "[✓] JDK         : $JAVA_ROOT"
else
  echo "[✗] 未检测到 JDK（Android 构建需要 JDK 17+）"
  echo "    请下载安装 OpenJDK："
  echo "      https://adoptium.net/temurin/releases/?version=17"
  echo "    或 https://www.oracle.com/java/technologies/downloads/"
  echo "    安装后设置环境变量 JAVA_HOME"
fi

# 将检测结果写入 Android 构建配置（若存在）
ANDROID_PROJ="$(dirname "$0")/../client/android"
if [ -n "$SDK_ROOT" ]; then
  mkdir -p "$ANDROID_PROJ"
  # 写入 local.properties (Android 标准 sdk.dir 配置)
  if [ -f "$ANDROID_PROJ/local.properties" ]; then
    sed -i "s|^sdk.dir=.*|sdk.dir=$(echo "$SDK_ROOT" | sed 's|/|\\\\|g')|" "$ANDROID_PROJ/local.properties" 2>/dev/null \
      || echo "sdk.dir=$(echo "$SDK_ROOT" | sed 's|/|\\\\|g')" >> "$ANDROID_PROJ/local.properties"
  else
    echo "sdk.dir=$(echo "$SDK_ROOT" | sed 's|/|\\\\|g')" > "$ANDROID_PROJ/local.properties"
  fi
  echo "[✓] 已将 sdk.dir 写入 client/android/local.properties"
fi

if [ -n "$SDK_ROOT" ] && [ ! -d "$SDK_ROOT/platforms/android-35" ]; then
  echo ""
  echo "[提示] 检测到 SDK 但缺少 android-35 / build-tools 35.0.0，请运行以下命令补装："
  echo "  \$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager.bat \"platform-tools\" \"platforms;android-35\" \"build-tools;35.0.0\""
fi

echo ""
echo "=============================================="
echo " 环境检测完成。若上两项均为 [✓] 即可进行 Android 构建。"
echo "=============================================="