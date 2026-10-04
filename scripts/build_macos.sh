#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export PYINSTALLER_CONFIG_DIR="${TMPDIR:-/tmp}/bili-downloader-pyinstaller"
export PIP_CACHE_DIR="${TMPDIR:-/tmp}/bili-downloader-pip-cache"

BUILD_PYTHON="${BUILD_PYTHON:-python3}"
"$BUILD_PYTHON" -c 'import sys; assert sys.version_info >= (3, 10), "Build requires Python 3.10+"'
"$BUILD_PYTHON" -m pip install -r requirements.txt
"$BUILD_PYTHON" -m unittest discover -s tests -v
if command -v node >/dev/null 2>&1; then node tests/test_extension.cjs; node tests/test_background_fetch.cjs; fi
"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name BiliDownloader \
  --osx-bundle-identifier com.codex.bilidownloader \
  --collect-all yt_dlp \
  --collect-all imageio_ffmpeg \
  --add-data "THIRD_PARTY_NOTICES.md:." \
  app.py

"$BUILD_PYTHON" -m PyInstaller \
  --noconfirm \
  --clean \
  --onefile \
  --console \
  --name BiliDownloaderHost \
  --distpath dist/host \
  --workpath build/host \
  --collect-all yt_dlp \
  --collect-all imageio_ffmpeg \
  --add-data "THIRD_PARTY_NOTICES.md:." \
  native_host.py

ARCH="$(uname -m)"
FINAL_PACKAGE="dist/BiliDownloader-macos-${ARCH}"
# Assemble away from Finder-open delivery folders; replace only after ZIP succeeds.
STAGING_DIR="$(mktemp -d "dist/.portable-${ARCH}.XXXXXX")"
PACKAGE_DIR="$STAGING_DIR/BiliDownloader-macos-${ARCH}"
mkdir -p "$PACKAGE_DIR"
cp -R "dist/BiliDownloader.app" "$PACKAGE_DIR/BiliDownloader.app"
cp "dist/host/BiliDownloaderHost" "$PACKAGE_DIR/BiliDownloaderHost"
cp -R browser-extension "$PACKAGE_DIR/browser-extension"
cp scripts/install_native_host_macos.sh "$PACKAGE_DIR/install_native_host_macos.sh"
cp README.md THIRD_PARTY_NOTICES.md "$PACKAGE_DIR/"
cp browser-extension/README.md "$PACKAGE_DIR/EXTENSION-README.md"
cp -R docs "$PACKAGE_DIR/docs"
ditto -c -k --keepParent "$PACKAGE_DIR" "dist/BiliDownloader-macos-${ARCH}.zip"
if [ -d "$FINAL_PACKAGE" ]; then mv "$FINAL_PACKAGE" "$STAGING_DIR/previous"; fi
mv "$PACKAGE_DIR" "$FINAL_PACKAGE"
rm -rf "$STAGING_DIR" || echo "Package built; temporary directory cleanup pending: $STAGING_DIR"
"$BUILD_PYTHON" scripts/package_source.py
echo "Created dist/BiliDownloader-macos-${ARCH}.zip"
