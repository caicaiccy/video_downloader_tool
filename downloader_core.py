"""Shared download logic for the desktop UI and browser native-messaging host."""

from __future__ import annotations

import os
import hashlib
import threading
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable

import imageio_ffmpeg
import yt_dlp
from media_policy import MediaPolicyError, PublicMediaDL, is_public_web_url, validate_selected_media
from web_page import LinkedPageIE


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".m4v", ".ogv"}


def is_bilibili_url(value: str) -> bool:
    """Only allow Bilibili's video and short-link domains."""
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return parsed.scheme in {"http", "https"} and (
        host == "b23.tv"
        or host == "bilibili.com"
        or host.endswith(".bilibili.com")
    )


def is_access_restriction(error: str) -> bool:
    """Avoid retrying errors that need an account or user action."""
    lower = error.lower()
    markers = (
        "login", "log in", "sign in", "member", "premium", "captcha",
        "geo restriction", "not available in your region", "private video",
        "unsupported url", "no video found", "requested format is not available",
        "http error 401", "http error 403", "forbidden", "unauthorized", "drm", "contentprotection",
        "加密", "不支持", "暂不支持", "只接受", "清单过大", "登录", "会员", "大会员", "验证码",
        "地区限制", "仅限", "无权", "无权限", "需要登录",
    )
    return any(marker in lower for marker in markers)


def human_size(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return "{:.1f} {}".format(size, unit)
        size /= 1024
    return "{:.1f} TB".format(size)


class _DownloadLogger:
    def __init__(self, on_log: Callable[[str], None]) -> None:
        self.on_log = on_log

    def debug(self, message: str) -> None:
        if message.strip() and not message.startswith("[debug]"):
            self.on_log(message.strip())

    def warning(self, message: str) -> None:
        if message.strip():
            self.on_log(message.strip())

    def error(self, message: str) -> None:
        if message.strip():
            self.on_log(message.strip())


def download_video(
    url: str,
    output_dir: Path,
    cancel_event: threading.Event,
    on_progress: Callable[[float, int, int], None],
    on_log: Callable[[str], None],
    on_status: Callable[[str], None],
    *,
    media: dict | None = None,
    title: str = "",
    browser_open: Callable | None = None,
    on_metadata: Callable[[str], None] | None = None,
) -> Path:
    """Download one public page or browser-observed media URL, without credentials."""
    if not is_public_web_url(url):
        raise MediaPolicyError("只接受公开 HTTP(S) 视频或页面链接。")
    target = url
    headers = {}
    if media is not None:
        if not isinstance(media, dict) or not is_public_web_url(media.get("url")):
            raise MediaPolicyError("浏览器提供的媒体地址无效；blob: 不是下载地址。")
        if media.get("kind") not in {"direct", "hls", "dash"}:
            raise MediaPolicyError("不支持此媒体类型。")
        target = media["url"]
        referrer = media.get("referrer")
        if referrer:
            if not is_public_web_url(referrer):
                raise MediaPolicyError("媒体来源页面无效。")
            headers["Referer"] = referrer
    if not output_dir.exists() or not output_dir.is_dir():
        raise ValueError("保存目录不存在或不可用。")

    if cancel_event.is_set():
        raise MediaPolicyError("用户取消了下载")
    saved_files = []

    def progress_hook(data: dict) -> None:
        if cancel_event.is_set():
            raise yt_dlp.utils.DownloadError("用户取消了下载")
        status = data.get("status")
        if status == "downloading":
            received = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = (received / total * 100) if total else 0
            on_progress(min(percent, 100), received, total)
        elif status == "finished":
            if data.get("filename"):
                saved_files.append(Path(data["filename"]))
            on_status("当前视频已下载，正在合并音视频…")

    options = {
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/best",
        "merge_output_format": "mp4/mkv",
        "ffmpeg_location": imageio_ffmpeg.get_ffmpeg_exe(),
        "outtmpl": str(output_dir / "%(title).180s [%(id)s].%(ext)s"),
        "noplaylist": True,
        "windowsfilenames": os.name == "nt",
        "overwrites": False,
        "continuedl": True,
        "cachedir": False,
        "quiet": True,
        "no_warnings": False,
        "logger": _DownloadLogger(on_log),
        "progress_hooks": [progress_hook],
        "http_headers": headers,
        "socket_timeout": 20,
        "retries": 0,
        "fragment_retries": 0,
        "extractor_retries": 0,
        "skip_unavailable_fragments": False,
        "allow_unplayable_formats": False,
        "hls_prefer_native": True,
        "external_downloader": {"m3u8": "native", "dash": "native"},
    }
    with ExitStack() as stack:
        downloader = stack.enter_context(PublicMediaDL(options, cancel_event, browser_open=browser_open))
        if media is None:
            page_reader = stack.enter_context(PublicMediaDL(options, cancel_event)) if browser_open else None
            downloader.add_info_extractor(LinkedPageIE(downloader, on_status, page_reader))
        if browser_open and media and media["kind"] in {"hls", "dash"}:
            # Known browser manifests use the same GET flow as the player.
            # GenericIE's preliminary HEAD is unnecessary and some CDNs reject it.
            video_id = hashlib.sha256(urlparse(target).path.encode()).hexdigest()[:12]
            extractor = downloader.get_info_extractor("Generic")
            formats = (extractor._extract_m3u8_formats(target, video_id, "mp4", entry_protocol="m3u8_native", fatal=True)
                       if media["kind"] == "hls" else extractor._extract_mpd_formats(target, video_id, fatal=True))
            info = downloader.process_ie_result({"id": video_id, "title": title or "网页视频", "formats": formats,
                                                 "webpage_url": url, "extractor": "browser", "extractor_key": "Generic"}, download=False)
        else:
            info = downloader.extract_info(target, download=False)
        validate_selected_media(info)
        if media and title:
            info["title"] = title[:200]
        if on_metadata and info.get("title"):
            on_metadata(str(info["title"])[:200])
        info = downloader.process_ie_result(info, download=True)
        for item in [info, *(info.get("requested_downloads") or [])]:
            for key in ("filepath", "_filename"):
                if item.get(key):
                    saved_files.append(Path(item[key]))
        saved_files.append(Path(downloader.prepare_filename(info)))
    # Use this job's exact paths, including merge/remux extension changes.
    candidates = []
    for path in saved_files:
        for ext in (path.suffix, ".mp4", ".mkv"):
            candidate = path.with_suffix(ext)
            if candidate.is_file() and candidate.suffix.lower() in VIDEO_EXTENSIONS:
                candidates.append(candidate)
    if not candidates:
        raise RuntimeError("下载完成，但没有找到可用的视频文件。")
    saved_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    if saved_path.stat().st_size <= 0:
        raise RuntimeError("下载结果是空文件。")
    return saved_path
