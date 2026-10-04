"""Native Messaging bridge used by the Chrome and Edge extension."""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
from pathlib import Path
from typing import Any

from downloader_core import download_video, is_access_restriction
from media_policy import MediaPolicyError, is_public_web_url
from browser_transport import BrowserTransport
from desktop_actions import perform_action, output_directory, helper_main, ACTIONS


MAX_MESSAGE_BYTES = 1024 * 1024
HOST_NAME = "com.codex.bili_downloader"
write_lock = threading.Lock()
cancel_event = threading.Event()
queue_active = threading.Event()
worker: threading.Thread | None = None
desktop_lock = threading.Lock()
directory_changing = threading.Event()


def send_message(message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        payload = json.dumps(
            {"type": "error", "message": "Native Messaging 消息超过 1 MB。"},
            ensure_ascii=False,
        ).encode("utf-8")
    with write_lock:
        sys.stdout.buffer.write(struct.pack("<I", len(payload)))
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()


def read_exact(length: int) -> bytes | None:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sys.stdin.buffer.read(length - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return bytes(chunks)


def read_message() -> dict[str, Any] | None:
    header = read_exact(4)
    if header is None:
        return None
    length = struct.unpack("<I", header)[0]
    if length > MAX_MESSAGE_BYTES:
        raise ValueError("来自浏览器的消息超过 1 MB。")
    payload = read_exact(length)
    if payload is None:
        return None
    message = json.loads(payload.decode("utf-8"))
    if not isinstance(message, dict):
        raise ValueError("消息格式无效。")
    return message


browser_transport = BrowserTransport(send_message, cancel_event)


def run_queue(jobs: list[dict[str, Any]], output_dir: Path, retries: int) -> None:
    cancelled = False
    try:
        for position, job in enumerate(jobs, start=1):
            job_id = job["id"]
            url = job["url"]
            if cancel_event.is_set():
                cancelled = True
                break
            send_message({
                "type": "progress_reset",
                "id": job_id,
                "position": position,
                "total": len(jobs),
            })

            for attempt in range(1, retries + 2):
                if cancel_event.is_set():
                    send_message({
                        "type": "job_state", "id": job_id, "status": "已取消",
                        "attempts": attempt - 1,
                    })
                    cancelled = True
                    break
                send_message({
                    "type": "job_state", "id": job_id, "status": "下载中",
                    "attempts": attempt,
                })
                send_message({
                    "type": "status",
                    "text": "正在处理第 {} / {} 项，第 {} 次尝试。".format(
                        position, len(jobs), attempt
                    ),
                })

                def on_progress(percent: float, received: int, total: int) -> None:
                    send_message({
                        "type": "progress", "id": job_id, "percent": percent,
                        "received": received, "total": total,
                    })

                def on_log(text: str) -> None:
                    send_message({"type": "log", "id": job_id, "text": text[:6000]})

                def on_status(text: str) -> None:
                    send_message({"type": "status", "text": text})

                def on_metadata(title: str) -> None:
                    send_message({"type": "job_metadata", "id": job_id, "title": title})

                try:
                    saved_path = download_video(
                        url=url,
                        output_dir=output_dir,
                        cancel_event=cancel_event,
                        on_progress=on_progress,
                        on_log=on_log,
                        on_status=on_status,
                        media=job.get("media"),
                        title=job.get("title", ""),
                        browser_open=(lambda req: browser_transport.urlopen(req, job_id)) if job.get("media", {}).get("browser") or job.get("transport") == "background" else None,
                        on_metadata=on_metadata,
                    )
                except Exception as exc:
                    error = str(exc).strip() or exc.__class__.__name__
                    if cancel_event.is_set():
                        send_message({
                            "type": "job_state", "id": job_id, "status": "已取消",
                            "attempts": attempt, "error": error,
                        })
                        cancelled = True
                        break
                    if isinstance(exc, MediaPolicyError) or is_access_restriction(error):
                        send_message({
                            "type": "job_state", "id": job_id, "status": "失败",
                            "attempts": attempt, "error": error,
                        })
                        send_message({
                            "type": "log", "id": job_id,
                            "text": "检测到访问限制，不自动重试：{}".format(error),
                        })
                        break
                    if attempt <= retries:
                        delay = min(2 ** attempt, 30)
                        send_message({
                            "type": "job_state", "id": job_id, "status": "重试等待",
                            "attempts": attempt, "error": error,
                        })
                        send_message({
                            "type": "log", "id": job_id,
                            "text": "下载失败：{}；{} 秒后重试（{}/{}）。".format(
                                error, delay, attempt, retries
                            ),
                        })
                        if cancel_event.wait(delay):
                            send_message({
                                "type": "job_state", "id": job_id, "status": "已取消",
                                "attempts": attempt, "error": error,
                            })
                            cancelled = True
                            break
                        continue
                    send_message({
                        "type": "job_state", "id": job_id, "status": "失败",
                        "attempts": attempt, "error": error,
                    })
                    send_message({
                        "type": "log", "id": job_id,
                        "text": "达到最大重试次数，任务失败：{}".format(error),
                    })
                    break
                else:
                    send_message({
                        "type": "job_state", "id": job_id, "status": "完成",
                        "attempts": attempt,
                    })
                    send_message({
                        "type": "job_success", "id": job_id, "path": str(saved_path),
                    })
                    break

            if cancelled:
                break
    finally:
        queue_active.clear()
        send_message({"type": "queue_done", "cancelled": cancelled})


def normalize_job(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("队列任务格式无效。")
    job_id = item.get("id")
    url = item.get("url")
    if not isinstance(job_id, str) or not job_id or len(job_id) > 80 or not is_public_web_url(url):
        raise ValueError("任务 ID 或公开 HTTP(S) 页面链接无效。")
    job = {"id": job_id, "url": url}
    if item.get("transport") is not None:
        if item["transport"] != "background":
            raise ValueError("不支持的下载传输方式。")
        job["transport"] = "background"
    media = item.get("media")
    if media is not None:
        if not isinstance(media, dict) or not is_public_web_url(media.get("url")) or media.get("kind") not in {"direct", "hls", "dash"}:
            raise ValueError("浏览器媒体地址或类型无效。")
        referrer = media.get("referrer", url)
        if not is_public_web_url(referrer):
            raise ValueError("媒体来源页面无效。")
        job["media"] = {"url": media["url"], "kind": media["kind"], "referrer": referrer}
        if media.get("browser"):
            context = media["browser"]
            if not isinstance(context, dict) or not isinstance(context.get("tabId"), int) or context["tabId"] < 0 or not isinstance(context.get("frameId"), int) or context["frameId"] < 0 or not is_public_web_url(context.get("frameUrl")):
                raise ValueError("浏览器播放器上下文无效，请重新扫描。")
            job["media"]["browser"] = {k: context[k] for k in ("tabId", "frameId", "frameUrl")}
        job["title"] = str(item.get("title") or "")[:200]
    return job


def start_queue(message: dict[str, Any]) -> None:
    global worker
    if queue_active.is_set():
        send_message({"type": "error", "message": "已有下载队列正在运行。"})
        return
    if directory_changing.is_set():
        send_message({"type": "error", "message": "请先完成下载目录设置。"})
        return

    supplied_jobs = message.get("jobs")
    if not isinstance(supplied_jobs, list) or not supplied_jobs:
        send_message({"type": "error", "message": "队列中没有可下载的视频。"})
        return

    try:
        jobs = [normalize_job(item) for item in supplied_jobs]
        if len({job["id"] for job in jobs}) != len(jobs):
            raise ValueError("队列任务 ID 重复。")
    except ValueError as exc:
        send_message({"type": "error", "message": str(exc)})
        return

    try:
        retries = int(message.get("maxRetries", 3))
    except (TypeError, ValueError):
        retries = 3
    retries = max(0, min(5, retries))
    try:
        output_dir = output_directory()
        output_dir.mkdir(parents=True, exist_ok=True)
        if not os.access(output_dir, os.W_OK):
            raise PermissionError("下载目录不可写。")
    except (OSError, ValueError) as exc:
        send_message({"type": "error", "message": "无法使用下载目录：{}".format(exc)})
        return

    cancel_event.clear()
    queue_active.set()
    send_message({"type": "queue_started", "count": len(jobs), "retries": retries})
    worker = threading.Thread(
        target=run_queue,
        args=(jobs, output_dir, retries),
        daemon=True,
    )
    worker.start()


def desktop_action(message: dict[str, Any]) -> None:
    request_id = message.get("requestId")
    action = message.get("action")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 80:
        return
    def respond(**result):
        send_message({"type": "desktop_result", "requestId": request_id, **result})
    if action not in ACTIONS:
        respond(ok=False, error="不支持的文件操作。")
        return
    if action in {"choose_directory", "reset_directory"} and queue_active.is_set():
        respond(ok=False, error="请在队列结束后更改下载目录。")
        return
    if not desktop_lock.acquire(blocking=False):
        respond(ok=False, error="请先完成当前系统文件操作。")
        return
    if action in {"choose_directory", "reset_directory"}:
        directory_changing.set()
    def run():
        try:
            respond(**perform_action(action, message.get("path", ""), message.get("chooseApplication") is True))
        except Exception as exc:
            respond(ok=False, error=str(exc)[:1000] or "本地文件操作失败。")
        finally:
            directory_changing.clear()
            desktop_lock.release()
    threading.Thread(target=run, daemon=True).start()


def main() -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)

    send_message({"type": "host_ready", "name": HOST_NAME, "version": "1.5.1",
                  "protocolVersion": 4, "capabilities": ["public_web_media", "browser_http", "desktop_actions", "page_scan", "background_http"]})
    try:
        while True:
            message = read_message()
            if message is None:
                break
            action = message.get("type")
            if action == "start_queue":
                start_queue(message)
            elif action == "cancel_queue":
                cancel_event.set()
                browser_transport.cancel()
                send_message({"type": "status", "text": "正在停止当前下载任务…"})
            elif action == "browser_response":
                browser_transport.receive(message)
            elif action == "desktop_action":
                desktop_action(message)
            else:
                send_message({"type": "error", "message": "不支持的操作。"})
    except Exception as exc:
        send_message({"type": "error", "message": str(exc)[:6000]})
    finally:
        cancel_event.set()
        browser_transport.cancel()
        if worker and worker.is_alive():
            worker.join(timeout=15)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--desktop-action":
        helper_main(sys.argv[2:])
    else:
        main()
