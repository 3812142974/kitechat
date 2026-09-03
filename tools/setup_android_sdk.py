#!/usr/bin/env python3
"""KiteChat Android 环境一键准备（跨平台，无 Git/无 cmd 依赖）。

用 platform.system() 检测平台，用 Python 标准库完成：检测 -> 缺失则
下载并按平台布局安装到 tools/android-sdk 与 tools/jdk -> 写 local.properties。

支持：Windows、Linux。mac 暂无适配计划（打印提示即退出）。

用法： python tools/setup_android_sdk.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tarfile
import time
import zipfile
import urllib.request
import subprocess

# ------------- 常量 -------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPT_DIR)
TOOLS_DIR = os.path.join(ROOT, "tools")
ANDROID_DIR = os.path.join(ROOT, "client", "android")
LOCAL_PROPS = os.path.join(ANDROID_DIR, "local.properties")

SDK_LOCAL = os.path.join(TOOLS_DIR, "android-sdk")
JDK_LOCAL = os.path.join(TOOLS_DIR, "jdk")

SDK_PLATFORMS = "platforms;android-35"
SDK_BUILD_TOOLS = "build-tools;35.0.0"
SDK_PLATFORM_TOOLS = "platform-tools"

OS = sys.platform                       # 'win32' | 'linux' | 'darwin'
IS_WINDOWS = OS == "win32"
IS_LINUX = OS.startswith("linux")

def platform_name() -> str:
    return "Windows" if IS_WINDOWS else ("Linux" if IS_LINUX else "unknown(%s)" % OS)

def log(msg):  print("\033[1;34m[env]\033[0m %s" % msg)
def ok(msg):   print("\033[1;32m[✓]\033[0m %s" % msg)
def warn(msg): print("\033[1;33m[!]\033[0m %s" % msg)
def die(msg):  print("\033[1;31m[x]\033[0m %s" % msg, file=sys.stderr); sys.exit(1)

# ------------- 平台对应的下载源 -------------
if IS_LINUX:
    SDK_CLT_URL = ("https://dl.google.com/android/repository/"
                   "commandlinetools-linux-16111833_latest.zip")
    # Oracle 官方 JDK 21 LTS (Linux 为 tar.gz)
    JDK_URL = ("https://download.oracle.com/java/21/latest/"
               "jdk-21_linux-x64_bin.tar.gz")
    JDK_ARCHIVE_IS_TAR = True
    SDKMANAGER = "sdkmanager"           # Linux 无 .bat
    SDKEXE = "java"
elif IS_WINDOWS:
    SDK_CLT_URL = ("https://dl.google.com/android/repository/"
                   "commandlinetools-win-16111833_latest.zip")
    # Oracle 官方 JDK 21 LTS (Windows 为 zip)
    JDK_URL = ("https://download.oracle.com/java/21/latest/"
               "jdk-21_windows-x64_bin.zip")
    JDK_ARCHIVE_IS_TAR = False
    SDKMANAGER = "sdkmanager.bat"
    SDKEXE = "java.exe"
else:
    print("⚠️  平台 %s 暂无适配计划（mac 也不支持）。请在 Windows 或 Linux 上构建，"
          "或手动安装后设置 ANDROID_HOME / JAVA_HOME。" % platform_name())
    sys.exit(0)

# ------------- 检测 -------------
def detect_sdk() -> str:
    for v in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        p = os.environ.get(v, "").strip()
        if p and os.path.isdir(p):
            return p
    if os.path.isdir(SDK_LOCAL):
        return SDK_LOCAL
    for p in (os.path.join(os.path.expanduser("~"), "Android", "Sdk"),
              "/opt/android-sdk", "/usr/lib/android-sdk"):
        if os.path.isdir(p):
            return p
    return ""

def _jdk_has_java(p: str) -> bool:
    return os.path.isfile(os.path.join(p, "bin", SDKEXE))

def detect_jdk() -> str:
    p = os.environ.get("JAVA_HOME", "").strip()
    if p and os.path.isdir(p) and _jdk_has_java(p):
        return p
    if os.path.isdir(JDK_LOCAL):
        # 形如 tools/jdk/<jdk-17.x.y>/bin/java[.exe]
        if _jdk_has_java(JDK_LOCAL):
            return JDK_LOCAL
        for name in sorted(os.listdir(JDK_LOCAL)):
            inner = os.path.join(JDK_LOCAL, name)
            if os.path.isdir(inner) and _jdk_has_java(inner):
                return inner
    # java 命令解析
    jbin = shutil.which("java")
    if jbin:
        root = os.path.dirname(os.path.dirname(os.path.abspath(jbin)))
        if _jdk_has_java(root):
            return root
    # Windows: Program Files / LocalAppData
    if IS_WINDOWS:
        cand_bases = [
            r"C:\Program Files\Java",
            r"C:\Program Files\Eclipse Adoptium",
            r"C:\Program Files\Microsoft",
            r"C:\Program Files",        # enumerates Java21 / java8 / etc.
            r"D:\Program Files\Java",
            r"D:\Program Files",        # enumerates Java21 / etc.
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        ]
        for base in cand_bases:
            if not os.path.isdir(base):
                continue
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for name in names:
                inner = os.path.join(base, name)
                if os.path.isdir(inner) and _jdk_has_java(inner):
                    return inner
    # Linux: /usr/lib/jvm 等
    for base in ("/usr/lib/jvm", "/opt", os.path.expanduser("~/.jdks")):
        if not os.path.isdir(base):
            continue
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            inner = os.path.join(base, name)
            if os.path.isdir(inner) and _jdk_has_java(inner):
                return inner
    return ""

# ------------- 下载/解压 -------------
def download(url: str, out: str) -> None:
    log("下载 %s" % url)
    tmpl = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": "agent/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(out, "wb") as f:
            shutil.copyfileobj(resp, f)
    except Exception as e:
        die("下载失败 %s: %s" % (url, e))
    log("  下载完成 %.1fs -> %s" % (time.time() - tmpl, out))

def unzip_zip(src: str, dest: str) -> None:
    log("解压 %s -> %s" % (src, dest))
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(src) as z:
        z.extractall(dest)

def _untar(src: str, dest: str) -> None:
    log("解压 %s -> %s" % (src, dest))
    os.makedirs(dest, exist_ok=True)
    # 兼容 tar.gz / tar.bz2 / tar.xz（按扩展名选模式）
    mode = "r:gz" if src.endswith(".gz") else ("r:bz2" if src.endswith(".bz2")
                                               else "r:xz" if src.endswith(".xz")
                                               else "r:*")
    with tarfile.open(src, mode) as t:
        # 安全解压（避免路径穿越）
        t.extractall(dest, filter="data")

# ------------- 安装 -------------
def install_sdk() -> str:
    sdk_root = SDK_LOCAL
    sm_path = os.path.join(sdk_root, "cmdline-tools", "latest", "bin", SDKMANAGER)
    if os.path.isfile(sm_path):
        ok("项目内 SDK 已存在: %s" % sdk_root)
    else:
        log("在项目内安装 Android SDK 到 tools/android-sdk ...")
        os.makedirs(sdk_root, exist_ok=True)
        ztmp = os.path.join(TOOLS_DIR, "clt.zip")
        download(SDK_CLT_URL, ztmp)
        unzip_zip(ztmp, sdk_root)
        os.remove(ztmp)
        # commandlinetools zip 顶层是 cmdline-tools 目录 -> 移到 cmdline-tools/latest
        cl = os.path.join(sdk_root, "cmdline-tools")
        latest = os.path.join(cl, "latest")
        if os.path.isdir(cl) and not os.path.isdir(latest):
            tmp = os.path.join(sdk_root, "cmdline-tools_tmp")
            os.rename(cl, tmp)
            os.makedirs(cl, exist_ok=True)
            os.rename(tmp, latest)
    sm_path = os.path.join(sdk_root, "cmdline-tools", "latest", "bin", SDKMANAGER)
    if not os.path.isfile(sm_path):
        # 兜底查找
        for dirpath, _, files in os.walk(sdk_root):
            if SDKMANAGER in files:
                sm_path = os.path.join(dirpath, SDKMANAGER)
                break
    if not os.path.isfile(sm_path):
        die("SDK cmdline-tools 安装失败（未找到 %s）" % SDKMANAGER)
    if IS_LINUX:
        os.chmod(sm_path, os.stat(sm_path).st_mode | 0o111)
    log("接受 SDK 许可 + 安装组件: %s / %s / %s"
        % (SDK_PLATFORM_TOOLS, SDK_PLATFORMS, SDK_BUILD_TOOLS))
    import io
    try:
        with subprocess.Popen(
                [sm_path, "--licenses"], cwd=sdk_root, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as p:
            p.communicate(input=b"y\n" * 50, timeout=300)
    except Exception:
        pass
    r = subprocess.run(
        [sm_path, SDK_PLATFORM_TOOLS, SDK_PLATFORMS, SDK_BUILD_TOOLS],
        cwd=sdk_root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=1800)
    if r.returncode != 0:
        die("sdkmanager 安装组件失败 (rc=%s)" % r.returncode)
    ok("SDK 组件安装完成: %s" % sdk_root)
    return sdk_root

def install_jdk() -> str:
    jdk_root = JDK_LOCAL
    for cand in (jdk_root, *[os.path.join(jdk_root, n)
                             for n in (sorted(os.listdir(jdk_root))
                                       if os.path.isdir(jdk_root) else [])]):
        if _jdk_has_java(cand):
            ok("项目内 JDK 已存在: %s" % cand)
            return cand
    log("在项目内安装 Oracle JDK 21 到 tools/jdk ...")
    os.makedirs(jdk_root, exist_ok=True)
    archive = os.path.join(TOOLS_DIR, "jdk.archive")
    download(JDK_URL, archive)
    if JDK_ARCHIVE_IS_TAR:
        _untar(archive, jdk_root)
    else:
        unzip_zip(archive, jdk_root)
    os.remove(archive)
    for n in sorted(os.listdir(jdk_root)):
        inner = os.path.join(jdk_root, n)
        if _jdk_has_java(inner):
            ok("JDK 安装完成: %s" % inner)
            return inner
    if _jdk_has_java(jdk_root):
        ok("JDK 安装完成: %s" % jdk_root)
        return jdk_root
    die("JDK 安装失败（未找到 bin/%s）" % SDKEXE)

# ------------- main -------------
def main() -> None:
    print("=" * 62)
    print(" KiteChat Android 构建环境准备   [平台: %s]" % platform_name())
    print("=" * 62)
    if not (IS_WINDOWS or IS_LINUX):
        print("⚠️  平台 %s 暂无适配计划（含 macOS）。请在 Windows 或 Linux 上构建，"
              "或手动安装后设置 ANDROID_HOME / JAVA_HOME。" % platform_name())
        return

    sdk = detect_sdk()
    jdk = detect_jdk()

    if not sdk:
        warn("未检测到 Android SDK -> 自动安装到项目内")
        sdk = install_sdk()
    else:
        ok("Android SDK : %s" % sdk)
    if not jdk:
        warn("未检测到 JDK -> 自动安装到项目内")
        jdk = install_jdk()
    else:
        ok("JDK         : %s" % jdk)

    os.makedirs(ANDROID_DIR, exist_ok=True)
    sdk_dir = sdk.replace("\\", "/")
    if os.path.isfile(LOCAL_PROPS):
        with open(LOCAL_PROPS, "r", encoding="utf-8") as f:
            lines = f.readlines()
        out = [("sdk.dir=%s\n" % sdk_dir) if l.startswith("sdk.dir=") else l
               for l in lines]
        if not any(l.startswith("sdk.dir=") for l in out):
            out.append("sdk.dir=%s\n" % sdk_dir)
    else:
        out = ["sdk.dir=%s\n" % sdk_dir]
    with open(LOCAL_PROPS, "w", encoding="utf-8") as f:
        f.writelines(out)
    ok("已写入 %s  ->  sdk.dir=%s" % (LOCAL_PROPS, sdk_dir))

    print("-" * 54)
    print(" 构建/导出时设置以下环境变量（或写入服务端配置）：")
    print("   export ANDROID_HOME=\"%s\"" % sdk)
    print("   export ANDROID_SDK_ROOT=\"%s\"" % sdk)
    print("   export JAVA_HOME=\"%s\"" % jdk)
    print("-" * 54)
    print(" 环境准备完成 ✅ 现在可进行 Android APK 构建。")
    print(" 支持平台: Windows / Linux （mac 暂无适配计划）")


if __name__ == "__main__":
    main()
