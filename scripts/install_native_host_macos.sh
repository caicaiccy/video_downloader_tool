#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  echo "用法：$0 /绝对路径/BiliDownloaderHost [Chrome扩展ID] [Edge扩展ID]" >&2
  exit 2
fi

HOST_PATH="$1"
CHROME_ID="${2:-}"
EDGE_ID="${3:-}"

if [ ! -f "$HOST_PATH" ]; then
  echo "找不到 Native Host：$HOST_PATH" >&2
  exit 1
fi
case "$HOST_PATH" in
  /*) ;;
  *) echo "请为 Native Host 提供绝对路径。" >&2; exit 1 ;;
esac

validate_id() {
  id="$1"
  [ -z "$id" ] && return 0
  [ "${#id}" -eq 32 ] || return 1
  case "$id" in *[!a-p]*) return 1 ;; esac
}

validate_id "$CHROME_ID" || { echo "Chrome 扩展 ID 格式无效。" >&2; exit 1; }
validate_id "$EDGE_ID" || { echo "Edge 扩展 ID 格式无效。" >&2; exit 1; }
[ -n "$CHROME_ID$EDGE_ID" ] || { echo "至少要提供 Chrome 或 Edge 扩展 ID。" >&2; exit 2; }

INSTALL_DIR="$HOME/Library/Application Support/BiliDownloader"
mkdir -p "$INSTALL_DIR"
INSTALLED_HOST="$INSTALL_DIR/BiliDownloaderHost"
# Replace with a fresh inode: overwriting an executing signed Mach-O can leave
# the old code-signature cache attached and cause macOS to terminate the update.
INSTALLED_TEMP="$(mktemp "$INSTALL_DIR/.BiliDownloaderHost.XXXXXX")"
cp "$HOST_PATH" "$INSTALLED_TEMP"
chmod u+x "$INSTALLED_TEMP"
mv -f "$INSTALLED_TEMP" "$INSTALLED_HOST"
HOST_PATH="$INSTALLED_HOST"

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

ESCAPED_HOST="$(json_escape "$HOST_PATH")"
ORIGINS=""
if [ -n "$CHROME_ID" ]; then
  ORIGINS="\"chrome-extension://$CHROME_ID/\""
fi
if [ -n "$EDGE_ID" ]; then
  if [ -n "$ORIGINS" ]; then ORIGINS="$ORIGINS,"; fi
  ORIGINS="$ORIGINS\"chrome-extension://$EDGE_ID/\""
fi

write_manifest() {
  destination="$1"
  mkdir -p "$(dirname "$destination")"
  cat > "$destination" <<EOF
{
  "name": "com.codex.bili_downloader",
  "description": "Bilibili 本地下载器 Native Messaging Host",
  "path": "$ESCAPED_HOST",
  "type": "stdio",
  "allowed_origins": [$ORIGINS]
}
EOF
  chmod 644 "$destination"
}

chmod u+x "$HOST_PATH"
if [ -n "$CHROME_ID" ]; then
  write_manifest "$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.codex.bili_downloader.json"
fi
if [ -n "$EDGE_ID" ]; then
  write_manifest "$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts/com.codex.bili_downloader.json"
fi

echo "Native Host 已注册。请重新加载 Edge/Chrome 扩展。"
echo "默认保存到系统下载目录，可在扩展弹窗中更改。"
