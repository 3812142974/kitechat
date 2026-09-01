#!/usr/bin/env bash
# KiteChat Android 环境一键准备脚本（通用版，无本机硬编码路径）
#
# 三步：
#   1. 检测 Android SDK 与 JDK —— 顺序：环境变量 -> 项目内 tools/ -> 常见全局路径
#   2. 若缺失：自动下载并安装到【项目内】 tools/android-sdk 与 tools/jdk
#      （SDK 装 platform-tools / platforms;android-35 / build-tools;35.0.0，JDK 用 Temurin 17）
#   3. 写入 client/android/local.properties (sdk.dir) 并打印导出用的环境变量
#
# 用法：  bash tools/setup_android_sdk.sh
# 网络：  需要能访问 dl.google.com（SDK）与 Adoptium / 镜像（JDK）
set -u

# ---------- 工具 ----------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"          # 项目根
TOOLS_DIR="$ROOT/tools"
ANDROID_DIR="$ROOT/client/android"
LOCAL_PROPS="$ANDROID_DIR/local.properties"

SDK_LOCAL="$TOOLS_DIR/android-sdk"            # 项目内 SDK
JDK_LOCAL="$TOOLS_DIR/jdk"                     # 项目内 JDK

SDK_PLATFORMS="platforms;android-35"
SDK_BUILD_TOOLS="build-tools;35.0.0"
SDK_PLATFORM_TOOLS="platform-tools"

# 下载源（可按需改镜像）
SDK_CLT_URL="https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip"
JDK_ZIP_URL="https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse"

log()  { printf '\033[1;34m[env]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

has_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- 1. 检测 ----------
detect_sdk() {
  local p
  for var in ANDROID_HOME ANDROID_SDK_ROOT; do
    p="${!var:-}"
    [ -n "$p" ] && [ -d "$p" ] && { echo "$p"; return 0; }
  done
  [ -d "$SDK_LOCAL" ] && { echo "$SDK_LOCAL"; return 0; }
  for p in \
    "$LOCALAPPDATA/Android/Sdk" "$HOME/Android/Sdk" \
    "/c/Android/Sdk" "/d/Android/Sdk"; do
    [ -d "$p" ] && { echo "$p"; return 0; }
  done
  return 1
}

detect_jdk() {
  local p jbin
  p="${JAVA_HOME:-}"
  [ -n "$p" ] && [ -d "$p" ] && { echo "$p"; return 0; }
  # 项目内 JDK：解压后形如 tools/jdk/<jdk-17.x>/bin/java.exe
  local cand="$JDK_LOCAL"
  if [ -d "$cand" ]; then
    local found=""
    for inner in "$cand"/* ; do
      [ -f "$inner/bin/java.exe" ] && found="$inner" && break
    done
    [ -n "$found" ] || found="$cand"
    if [ -f "$found/bin/java.exe" ]; then echo "$found"; return 0; fi
  fi
  if has_cmd java; then
    jbin="$(command -v java)"
    jd="$(cd "$(dirname "$jbin")/.." && pwd)"
    [ -f "$jd/bin/java.exe" ] && { echo "$jd"; return 0; }
  fi
  for p in "$LOCALAPPDATA/Programs"/* "$LOCALAPPDATA/Programs"/*/* \
    /c/Program\ Files/Java/* /d/Program\ Files/Java/* /c/Program\ Files/Eclipse\ Adoptium/*; do
    [ -f "$p/bin/java.exe" ] && { echo "$p"; return 0; }
  done
  return 1
}

# ---------- 2. 安装（缺失时） ----------
download() {  # $1=url  $2=out
  log "下载 $1"
  curl -fL --retry 3 --connect-timeout 20 -o "$2" "$1" || die "下载失败: $1"
}

install_sdk() {
  local sdk_root="$SDK_LOCAL"
  if [ -f "$sdk_root/cmdline-tools/latest/bin/sdkmanager.bat" ]; then
    ok "项目内 SDK 已存在: $sdk_root"
  else
    log "在项目内安装 Android SDK 到 tools/android-sdk ..."
    mkdir -p "$sdk_root"
    local ztmp="$TOOLS_DIR/clt.zip"
    download "$SDK_CLT_URL" "$ztmp"
    ( cd "$sdk_root" && unzip -q -o "$ztmp" || {
        warn "unzip 不可用，改用 PowerShell 解压"
        powershell -NoProfile -Command \
          "Expand-Archive -Force -LiteralPath '$(cygpath -w "$ztmp")' -DestinationPath '$(cygpath -w "$sdk_root")'"
      } )
    rm -f "$ztmp"
    # commandlinetools zip 顶层是 cmdline-tools 目录 → 移到 cmdline-tools/latest
    if [ -d "$sdk_root/cmdline-tools" ] && [ ! -d "$sdk_root/cmdline-tools/latest" ]; then
      mv "$sdk_root/cmdline-tools" "$sdk_root/cmdline-tools_tmp"
      mkdir -p "$sdk_root/cmdline-tools"
      mv "$sdk_root/cmdline-tools_tmp" "$sdk_root/cmdline-tools/latest"
    fi
  fi
  [ -f "$sdk_root/cmdline-tools/latest/bin/sdkmanager.bat" ] || {
    # 老版本目录层级：cmdline-tools/latest/bin/sdkmanager.bat 兜底查找
    local sm
    sm="$(find "$sdk_root" -iname 'sdkmanager.bat' 2>/dev/null | head -1)"
    [ -n "$sm" ] || die "SDK cmdline-tools 安装失败（未找到 sdkmanager）"
  }
  local sdkmanager="$sdk_root/cmdline-tools/latest/bin/sdkmanager.bat"
  log "接受 SDK 许可 + 安装组件: $SDK_PLATFORM_TOOLS / $SDK_PLATFORMS / $SDK_BUILD_TOOLS"
  ( cd "$sdk_root"
    yes 2>/dev/null | "$sdkmanager" --licenses >/dev/null 2>&1 || true
    "$sdkmanager" "$SDK_PLATFORM_TOOLS" "$SDK_PLATFORMS" "$SDK_BUILD_TOOLS" >/dev/null 2>&1 \
      || die "sdkmanager 安装组件失败"
  )
  ok "SDK 组件安装完成: $sdk_root"
  echo "$sdk_root"
}

install_jdk() {
  local jdk_root="$JDK_LOCAL"
  # 已解压好（含 bin/java.exe）
  local found=""
  if [ -d "$jdk_root" ]; then
    for inner in "$jdk_root"/*; do
      [ -f "$inner/bin/java.exe" ] && found="$inner" && break
    done
    [ -n "$found" ] && { ok "项目内 JDK 已存在: $found"; echo "$found"; return; }
    # 根目录本身即是 jdk
    if [ -f "$jdk_root/bin/java.exe" ]; then ok "项目内 JDK 已存在: $jdk_root"; echo "$jdk_root"; return; fi
  fi
  log "在项目内安装 JDK 17 到 tools/jdk ..."
  mkdir -p "$jdk_root"
  local ztmp="$TOOLS_DIR/jdk.zip"
  download "$JDK_ZIP_URL" "$ztmp"
  ( cd "$jdk_root" && unzip -q -o "$ztmp" || {
      warn "unzip 不可用，改用 PowerShell 解压"
      powershell -NoProfile -Command \
        "Expand-Archive -Force -LiteralPath '$(cygpath -w "$ztmp")' -DestinationPath '$(cygpath -w "$jdk_root")'"
    } )
  rm -f "$ztmp"
  for inner in "$jdk_root"/*; do
    [ -f "$inner/bin/java.exe" ] && found="$inner" && break
  done
  if [ -z "$found" ] && [ -f "$jdk_root/bin/java.exe" ]; then found="$jdk_root"; fi
  [ -n "$found" ] || die "JDK 安装失败（未找到 bin/java.exe）"
  ok "JDK 安装完成: $found"
  echo "$found"
}

# ---------- main ----------
SDK_ROOT="$(detect_sdk || true)"
JDK_ROOT="$(detect_jdk || true)"

echo "======================================================"
echo " KiteChat Android 构建环境准备"
echo "======================================================"
[ -n "$SDK_ROOT" ] && ok "Android SDK : $SDK_ROOT" || {
  warn "未检测到 Android SDK -> 自动安装到项目内"
  SDK_ROOT="$(install_sdk)"
}
[ -n "$JDK_ROOT" ] && ok "JDK         : $JDK_ROOT" || {
  warn "未检测到 JDK -> 自动安装到项目内"
  JDK_ROOT="$(install_jdk)"
}

# ---------- 3. 写入配置 ----------
mkdir -p "$ANDROID_DIR"
# local.properties 用正斜杠（Gradle 兼容，Windows 也接受）
SDK_DIR_SLASH="$(echo "$SDK_ROOT" | sed 's|\\\\|/|g')"
if grep -qs '^sdk.dir=' "$LOCAL_PROPS" 2>/dev/null; then
  sed -i "s|^sdk.dir=.*|sdk.dir=$SDK_DIR_SLASH|" "$LOCAL_PROPS"
else
  echo "sdk.dir=$SDK_DIR_SLASH" >> "$LOCAL_PROPS"
fi
ok "已写入 $LOCAL_PROPS  ->  sdk.dir=$SDK_DIR_SLASH"

# 缺组件提示（SDK 已装但缺 android-35 时）
if [ -n "$SDK_ROOT" ] && [ ! -d "$SDK_ROOT/platforms/android-35" ]; then
  warn "SDK 缺少 android-35 / build-tools 35.0.0，请运行："
  warn "  $SDK_ROOT/cmdline-tools/latest/bin/sdkmanager.bat \"$SDK_PLATFORM_TOOLS\" \"$SDK_PLATFORMS\" \"$SDK_BUILD_TOOLS\""
fi

echo "------------------------------------------------------"
echo " 构建/导出时设置以下环境变量（或写入服务端配置）："
echo "   export ANDROID_HOME=\"$SDK_ROOT\""
echo "   export ANDROID_SDK_ROOT=\"$SDK_ROOT\""
echo "   export JAVA_HOME=\"$JDK_ROOT\""
echo "------------------------------------------------------"
echo " 环境准备完成 ✅ 现在可进行 Android APK 构建。"
