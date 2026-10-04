"""Stream anonymous browser HTTP responses over Native Messaging, with backpressure."""
from __future__ import annotations

import base64
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field

from yt_dlp.networking import Request, Response
from yt_dlp.networking.exceptions import HTTPError
from media_policy import MediaPolicyError, is_public_web_url

MAX_CHUNK_BYTES = 128 * 1024
MAX_RESOURCE_BYTES = 2 * 1024 * 1024 * 1024
RESPONSE_HEADERS = {"content-type", "content-length", "content-range", "accept-ranges"}


@dataclass
class PendingResponse:
    job_id: str = ""
    file: object = field(default_factory=lambda: tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024))
    done: threading.Event = field(default_factory=threading.Event)
    next_seq: int = 0
    metadata: dict | None = None
    error: str = ""
    received: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    last_progress: float = 0


class BrowserTransport:
    def __init__(self, send_message, cancel_event):
        self.send_message = send_message
        self.cancel_event = cancel_event
        self.pending = {}
        self.lock = threading.Lock()

    def urlopen(self, request, job_id):
        req = Request(request) if isinstance(request, str) else request
        if not isinstance(req, Request) or req.method not in {"GET", "HEAD"} or not is_public_web_url(req.url):
            raise MediaPolicyError("浏览器传输仅支持公开 HTTP(S) GET/HEAD。")
        request_id = uuid.uuid4().hex
        state = PendingResponse(job_id=job_id)
        with self.lock:
            self.pending[request_id] = state
        try:
            self.send_message({"type": "browser_request", "requestId": request_id, "jobId": job_id,
                               "url": req.url, "method": req.method, "range": req.headers.get("Range", "")})
            while not state.done.wait(0.2):
                if self.cancel_event.is_set():
                    raise MediaPolicyError("用户取消了下载")
                if time.monotonic() - state.last_activity > 60:
                    raise MediaPolicyError("浏览器资源请求超时；请确认 Chrome 仍在运行且网络可用。")
            if state.error:
                raise MediaPolicyError(state.error)
            if not state.metadata:
                raise MediaPolicyError("浏览器未返回资源响应信息。")
            metadata = state.metadata
            state.file.seek(0)
            if req.method != "HEAD":
                # Browser fetch delivers decoded bytes; CORS may hide Content-Encoding.
                metadata["headers"]["Content-Length"] = str(state.received)
            response = Response(state.file, metadata["url"], metadata["headers"], metadata["status"])
            state.file = None  # Response now owns the file.
            if metadata["status"] >= 400:
                raise HTTPError(response)
            return response
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
                if state.file is not None:
                    state.file.close()
            self.send_message({"type": "browser_abort", "requestId": request_id})

    def receive(self, message):
        request_id = message.get("requestId")
        seq = message.get("seq")
        with self.lock:
            state = self.pending.get(request_id)
            if not state or state.done.is_set():
                return
            try:
                if not isinstance(seq, int) or seq != state.next_seq:
                    raise ValueError("浏览器分块顺序无效。")
                phase = message.get("phase")
                if phase == "headers":
                    status = message.get("status")
                    url = message.get("url")
                    headers = message.get("headers")
                    if state.metadata or seq != 0 or not isinstance(status, int) or not 200 <= status <= 599 or not is_public_web_url(url) or not isinstance(headers, dict):
                        raise ValueError("浏览器响应头无效。")
                    safe_headers = {str(k): str(v)[:4096] for k, v in headers.items() if str(k).lower() in RESPONSE_HEADERS}
                    state.metadata = {"url": url, "status": status, "headers": safe_headers}
                elif phase == "data":
                    if not state.metadata:
                        raise ValueError("资源分块缺少响应头。")
                    encoded = message.get("data")
                    if not isinstance(encoded, str) or len(encoded) > MAX_CHUNK_BYTES * 4 // 3 + 4:
                        raise ValueError("浏览器资源分块过大。")
                    chunk = base64.b64decode(encoded, validate=True)
                    state.received += len(chunk)
                    if len(chunk) > MAX_CHUNK_BYTES or state.received > MAX_RESOURCE_BYTES:
                        raise ValueError("浏览器资源超过大小限制。")
                    state.file.write(chunk)
                elif phase == "end":
                    if not state.metadata:
                        raise ValueError("资源响应缺少元数据。")
                    state.done.set()
                elif phase == "error":
                    raise ValueError(str(message.get("error") or "浏览器匿名请求失败。")[:1000])
                else:
                    raise ValueError("浏览器资源消息类型无效。")
                state.next_seq += 1
                state.last_activity = time.monotonic()
                ack = {"type": "browser_ack", "requestId": request_id, "seq": seq, "ok": True}
            except (ValueError, TypeError) as exc:
                state.error = str(exc)
                state.done.set()
                ack = {"type": "browser_ack", "requestId": request_id, "seq": seq, "ok": False, "error": state.error}
        self.send_message(ack)
        if message.get("phase") == "data" and time.monotonic() - state.last_progress >= 0.5:
            state.last_progress = time.monotonic()
            self.send_message({"type": "log", "id": state.job_id,
                               "text": "浏览器匿名传输：当前资源已接收 {:.1f} MB…".format(state.received / 1024 / 1024)})

    def cancel(self):
        with self.lock:
            for state in self.pending.values():
                state.error = "用户取消了下载"
                state.done.set()
