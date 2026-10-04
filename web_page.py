"""Resolve public HTML video links with ordinary HTTP, without a browser."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse

from yt_dlp.extractor.generic import GenericIE
from yt_dlp.networking import Request
from yt_dlp.utils import UnsupportedError, determine_ext, mimetype2ext

from media_policy import MediaPolicyError, is_public_web_url

MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 8
MAX_DEPTH = 3
MEDIA_SUFFIXES = {"mp4", "webm", "mov", "mkv", "flv", "m4v", "ogv", "m3u8", "mpd"}
LITERALS = re.compile(r'''["']((?:https?:)?//[^"'\s<>]+|/[^"'\s<>]+)["']''')


def media_kind(url):
    if not isinstance(url, str) or not url:
        return None
    extension = determine_ext(url, None)
    return "hls" if extension == "m3u8" else "dash" if extension == "mpd" else "direct" if extension in MEDIA_SUFFIXES else None


@dataclass(frozen=True)
class PageMedia:
    url: str
    kind: str
    referrer: str
    title: str = ""
    extension: str | None = None


class VideoPage(HTMLParser):
    def __init__(self, url):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.base = url
        self.title = ""
        self.in_title = False
        self.video_depth = 0
        self.script = None
        self.candidates = {}
        self.frames = []
        self.alternate_sources = []

    def absolute(self, value):
        if not isinstance(value, str) or not value.strip():
            return None
        url = urldefrag(urljoin(self.base, value.strip()))[0]
        return url if is_public_web_url(url) else None

    def add(self, value, priority, kind=None):
        url = self.absolute(value)
        if url and (kind or media_kind(url)) and len(self.candidates) < 100:
            found = (priority, PageMedia(url, kind or media_kind(url), self.url))
            if url not in self.candidates or priority < self.candidates[url][0]:
                self.candidates[url] = found

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "base" and attrs.get("href"):
            self.base = self.absolute(attrs["href"]) or self.base
        elif tag == "title":
            self.in_title = True
        elif tag == "video":
            self.video_depth += 1
            self.add(attrs.get("src"), 0, "direct" if not media_kind(attrs.get("src", "")) else None)
        elif tag == "source" and self.video_depth:
            value = attrs.get("src")
            mime = attrs.get("type", "").lower()
            kind = "hls" if "mpegurl" in mime else "dash" if "dash+xml" in mime else None
            self.add(value, 0, kind or media_kind(value or "") or "direct")
        elif tag == "meta":
            key = attrs.get("property", attrs.get("name", "")).lower()
            if key in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
                self.add(attrs.get("content"), 1)
        elif tag == "iframe" and attrs.get("src"):
            url = self.absolute(attrs["src"])
            if url and url not in self.frames:
                self.frames.append(url)
        elif tag == "a" and attrs.get("href") and "video_detail_spisode_link" in (attrs.get("class") or "").split():
            # Some public video pages expose equivalent sources as ordinary
            # episode links. If the selected player only publishes an opaque
            # runtime token, a sibling source for the same episode may still
            # expose a normal HLS/DASH URL without executing private code.
            alternate = self.absolute(attrs["href"])
            current = urlparse(self.url)
            candidate = urlparse(alternate) if alternate else None
            current_parts = current.path.strip("/").split("/")
            candidate_parts = candidate.path.strip("/").split("/") if candidate else []
            same_episode = (candidate and candidate.scheme == current.scheme and candidate.netloc == current.netloc and
                            len(current_parts) >= 4 and len(candidate_parts) == len(current_parts) and
                            candidate_parts[:-2] == current_parts[:-2] and candidate_parts[-1] == current_parts[-1] and
                            candidate_parts[-2] != current_parts[-2])
            if same_episode and alternate not in self.alternate_sources and len(self.alternate_sources) < 12:
                self.alternate_sources.append(alternate)
        elif tag == "script" and not attrs.get("src"):
            self.script = (attrs.get("type", ""), [])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "video":
            self.video_depth = max(0, self.video_depth - 1)
        elif tag == "script" and self.script is not None:
            mime, chunks = self.script
            self.parse_script("".join(chunks), mime)
            self.script = None

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.script is not None:
            self.script[1].append(data)

    def parse_script(self, text, mime):
        if mime.lower() == "application/ld+json":
            try:
                self.parse_metadata(json.loads(text))
            except (ValueError, RecursionError):
                pass
            return
        text = text.replace(r"\/", "/")
        # Manifest literals are useful for MSE players. Arbitrary script MP4s
        # (adposter, previews, etc.) must never become automatic downloads.
        for match in LITERALS.finditer(text):
            if media_kind(match[1]) in {"hls", "dash"}:
                self.add(match[1], 2)
        # Read simple URL assignments only when the same identifier is passed
        # to a player's source/url. No JS execution or opaque token decoding.
        assignments = re.finditer(r'''\b(?:var|let|const)\s+([\w$]+)\s*=\s*["']([^"'\s<>]+)["']''', text)
        for match in assignments:
            name = re.escape(match[1])
            if re.search(rf'(?:\b(?:url|src)\s*:\s*{name}\b|\.loadSource\(\s*{name}\s*\)|\.src\s*=\s*{name}\b)', text):
                self.add(match[2], 1)

    def parse_metadata(self, value, depth=0):
        if depth > 10:
            return
        if isinstance(value, list):
            for child in value:
                self.parse_metadata(child, depth + 1)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if types == "VideoObject" or isinstance(types, list) and "VideoObject" in types:
                self.add(value.get("contentUrl"), 1)
                embedded = self.absolute(value.get("embedUrl"))
                if embedded and embedded not in self.frames:
                    self.frames.append(embedded)
            for child in value.values():
                if isinstance(child, (dict, list)):
                    self.parse_metadata(child, depth + 1)


def scan_page(downloader, url, on_status=lambda text: None):
    """Return a single best-evidenced resource, traversing only iframe pages."""
    seen = set()
    root_title = ""

    def visit(page_url, referrer="", depth=0, try_alternates=False, capture_title=False):
        nonlocal root_title
        page_url = urldefrag(page_url)[0]
        if depth > MAX_DEPTH or len(seen) >= MAX_PAGES or page_url in seen:
            return None
        seen.add(page_url)
        on_status("正在解析网页视频{}…".format("（嵌入播放器）" if depth else ""))
        headers = {"Referer": referrer} if referrer else {}
        with downloader.urlopen(Request(page_url, headers=headers)) as response:
            final_url = response.url
            mime = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            kind = media_kind(final_url)
            if kind or mime.startswith("video/") or mime in {"application/vnd.apple.mpegurl", "application/x-mpegurl", "application/dash+xml"}:
                kind = kind or ("hls" if "mpegurl" in mime else "dash" if "dash+xml" in mime else "direct")
                return PageMedia(final_url, kind, referrer, root_title, mimetype2ext(mime))
            prefix = response.read(512)
            if mime not in {"text/html", "application/xhtml+xml"} and not re.search(br'<(?:!doctype\s+html|html|video|iframe)\b', prefix, re.I):
                return None
            data = prefix + response.read(MAX_PAGE_BYTES + 1 - len(prefix))
            if len(data) > MAX_PAGE_BYTES:
                raise MediaPolicyError("网页内容过大，停止自动解析。")
            encoding = re.search(r'charset\s*=\s*["\']?([\w-]+)', response.headers.get("Content-Type", ""), re.I)
            try:
                html = data.decode(encoding[1] if encoding else "utf-8", errors="replace")
            except LookupError:
                html = data.decode("utf-8", errors="replace")
        seen.add(final_url)
        page = VideoPage(final_url)
        page.feed(html)
        if capture_title:
            root_title = " ".join(page.title.split())[:200]
        if page.candidates:
            best = min(priority for priority, _ in page.candidates.values())
            choices = [media for priority, media in page.candidates.values() if priority == best]
            if len(choices) > 1:
                raise MediaPolicyError("网页包含多个视频资源，无法确定目标；请扫描页面后选择要下载的媒体。")
            media = choices[0]
            return PageMedia(media.url, media.kind, media.referrer, root_title or page.title[:200])
        for frame in page.frames:
            result = visit(frame, final_url, depth + 1)
            if result:
                return result
        if try_alternates and page.alternate_sources:
            on_status("当前来源无法直接解析，正在尝试同集其他公开来源…")
            for alternate in page.alternate_sources:
                result = visit(alternate, final_url, 0)
                if result:
                    return result
        return None

    return visit(url, try_alternates=True, capture_title=True)


class LinkedPageIE(GenericIE):
    """Extend only GenericIE; site-specific extractors retain their precedence."""
    @classmethod
    def ie_key(cls):
        return "Generic"

    def __init__(self, downloader, on_status, page_reader=None):
        super().__init__(downloader)
        self.on_status = on_status
        self.page_reader = page_reader or downloader

    def _real_extract(self, url):
        if media_kind(url):
            return super()._real_extract(url)
        media = scan_page(self.page_reader, url, self.on_status)
        if media is None:
            try:
                return super()._real_extract(url)
            except UnsupportedError as exc:
                raise MediaPolicyError("网页未公开可直接解析的视频地址；若需播放器运行后才能获取，请打开页面播放并扫描媒体。") from exc
        video_id = hashlib.sha256(url.encode()).hexdigest()[:12]
        headers = {"Referer": media.referrer} if media.referrer else {}
        info = {"id": video_id, "title": media.title or "网页视频", "webpage_url": url,
                "http_headers": headers}
        self.on_status("已解析到网页视频，正在准备下载…")
        if media.kind == "hls":
            info["formats"] = self._extract_m3u8_formats(media.url, video_id, "mp4", entry_protocol="m3u8_native", headers=headers)
        elif media.kind == "dash":
            info["formats"] = self._extract_mpd_formats(media.url, video_id, headers=headers)
        else:
            info.update(url=media.url, ext=media.extension or determine_ext(media.url, "mp4"))
        return info
