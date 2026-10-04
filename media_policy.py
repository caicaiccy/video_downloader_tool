"""Conservative public-media policy shared by the UI and Native Host."""
from __future__ import annotations

import io
import ipaddress
import re
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError

MAX_URL_LENGTH = 8192
MAX_MANIFEST_BYTES = 4 * 1024 * 1024


class MediaPolicyError(ValueError):
    """A permanent failure that must not be retried automatically."""


def is_public_web_url(value: str) -> bool:
    if not isinstance(value, str) or len(value) > MAX_URL_LENGTH or any(ord(c) < 32 for c in value):
        return False
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port  # Validate malformed ports too.
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or port == 0 or "\\" in value):
            return False
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            # Reject alternative IPv4 spellings (127.1, integers, octal, hex).
            return "." in host and not all(re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", part) for part in host.split("."))
    except ValueError:
        return False


def validate_manifest(data: bytes, kind: str) -> None:
    if len(data) > MAX_MANIFEST_BYTES:
        raise MediaPolicyError("媒体清单过大，停止处理。")
    text = data.decode("utf-8-sig", errors="replace")
    if kind == "hls":
        if not text.lstrip().startswith("#EXTM3U"):
            raise MediaPolicyError("响应不是有效的 HLS 清单。")
        for line in text.splitlines():
            if line.strip().startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                if not re.search(r"(?:^|[:,])\s*METHOD=NONE(?:,|$)", line.strip()):
                    raise MediaPolicyError("检测到加密 HLS 清单；不获取密钥或尝试解密。")
        if "#EXTINF:" in text and "#EXT-X-ENDLIST" not in text:
            raise MediaPolicyError("暂不支持直播或未结束的 HLS 清单。")
    elif kind == "dash":
        if re.search(r"<(?:[\w.-]+:)?ContentProtection\b", text, re.I):
            raise MediaPolicyError("检测到 DASH ContentProtection / DRM，停止下载。")
        if re.search(r'\btype\s*=\s*[\'"]dynamic[\'"]', text, re.I):
            raise MediaPolicyError("暂不支持动态 DASH / 直播。")


class PublicMediaDL(yt_dlp.YoutubeDL):
    """Inspect manifests at every yt-dlp request, including child playlists.

    Clear native HLS/DASH only: no key downloading or external stream protocols.
    The same guard applies on extraction and download, so a refreshed playlist
    cannot silently switch to encryption after the initial inspection.
    """

    def __init__(self, options, cancel_event, browser_open=None):
        self.cancel_event = cancel_event
        self.browser_open = browser_open
        super().__init__(options)

    def urlopen(self, request):
        if self.cancel_event.is_set():
            raise MediaPolicyError("用户取消了下载")
        url = request if isinstance(request, str) else request.url if isinstance(request, Request) else request.full_url
        if not is_public_web_url(url):
            raise MediaPolicyError("只接受公开 HTTP(S) 资源，不访问本地地址或带账号密码的 URL。")
        try:
            response = self.browser_open(request) if self.browser_open else super().urlopen(request)
        except HTTPError as exc:
            if exc.status not in {401, 403}:
                raise
            body = exc.response.read(4096).decode("utf-8", errors="replace").lower()
            exc.response.close()
            stage = "HLS 清单" if urlparse(url).path.lower().endswith(".m3u8") else "媒体资源"
            reason = "服务端明确拒绝此网络地区的访问" if "region has been denied" in body else "匿名请求被服务端拒绝"
            raise MediaPolicyError(f"{stage} HTTP {exc.status}：{reason}；不绕过访问限制。") from exc
        if not is_public_web_url(response.url):
            response.close()
            raise MediaPolicyError("媒体重定向到不支持的地址。")
        path = urlparse(response.url).path.lower()
        mime = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        kind = "hls" if path.endswith(".m3u8") or mime in {
            "application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl", "audio/x-mpegurl",
        } else "dash" if path.endswith(".mpd") or mime == "application/dash+xml" else None
        if not kind:
            return response
        try:
            data = response.read(MAX_MANIFEST_BYTES + 1)
            validate_manifest(data, kind)
            return Response(io.BytesIO(data), response.url, dict(response.headers),
                            response.status, response.reason, response.extensions)
        finally:
            response.close()


def validate_selected_media(info: dict) -> None:
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise MediaPolicyError("仅支持单个视频，请选择媒体资源或单集页面。")
    if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
        raise MediaPolicyError("暂不支持直播。")
    selected = info.get("requested_formats") or info.get("requested_downloads") or [info]
    for item in selected:
        if item.get("has_drm") or info.get("has_drm"):
            raise MediaPolicyError("检测到 DRM，停止下载。")
        protocol = item.get("protocol") or urlparse(item.get("url", "")).scheme
        if protocol not in {"http", "https", "m3u8_native", "m3u8", "http_dash_segments"}:
            raise MediaPolicyError("不支持此媒体协议：{}".format(protocol))
        if not is_public_web_url(item.get("url", "")):
            raise MediaPolicyError("选中的媒体没有公开 HTTP(S) 地址。")
        if item.get("hls_aes"):
            raise MediaPolicyError("检测到加密 HLS，停止下载。")
