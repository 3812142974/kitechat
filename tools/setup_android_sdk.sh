#!/usr/bin/env bash
# NovaChat Android SDK setup (everything on D:, shared location).
# SDK lands in D:/Android/sdk (shared with other projects).
# Override with ANDROID_HOME=/your/path before running if you want it elsewhere.
set -e
SDK_ROOT="${ANDROID_HOME:-D:/Android/sdk}"
mkdir -p "$SDK_ROOT"
cd "$SDK_ROOT"

export JAVA_HOME="D:/Program Files/Java21"
export ANDROID_HOME="$SDK_ROOT"
export ANDROID_SDK_ROOT="$SDK_ROOT"

if [ ! -f cmdline-tools/latest/bin/sdkmanager.bat ]; then
  echo "[sdk] downloading commandlinetools..."
  curl -sSL -o clt.zip https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip
  unzip -q -o clt.zip
  # zip top-level dir is 'cmdline-tools'; move to cmdline-tools/latest
  rm -rf cmdline-tools-tmp
  mv cmdline-tools cmdline-tools-tmp
  mkdir -p cmdline-tools
  mv cmdline-tools-tmp cmdline-tools/latest
  rm -f clt.zip
fi

echo "[sdk] accepting licenses..."
yes | cmdline-tools/latest/bin/sdkmanager.bat --licenses > licenses.log 2>&1 || true
echo "[sdk] installing packages..."
cmdline-tools/latest/bin/sdkmanager.bat "platform-tools" "platforms;android-35" "build-tools;35.0.0" > sdk_install.log 2>&1
echo "[sdk] done"
ls platforms build-tools platform-tools
