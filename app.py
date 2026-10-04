"""A desktop queue for downloading authorized public web videos."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from downloader_core import (
    download_video,
    human_size,
    is_access_restriction,
)
from media_policy import is_public_web_url


APP_TITLE = "网页视频下载器"
WAITING = "等待中"
DOWNLOADING = "下载中"
RETRY_WAIT = "重试等待"
COMPLETED = "完成"
FAILED = "失败"
CANCELLED = "已取消"


@dataclass
class DownloadJob:
    job_id: int
    url: str
    status: str = WAITING
    attempts: int = 0
    error: str = ""
    output_path: str = ""


class BiliDownloaderApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("860x720")
        self.root.minsize(740, 620)

        self.events: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.jobs: list[DownloadJob] = []
        self.next_job_id = 1
        self.max_retries_var = tk.IntVar(value=3)
        self.folder_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.status_var = tk.StringVar(value="添加视频链接，再开始队列")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()
        self.root.after(100, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        body = ttk.Frame(self.root, padding=18)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=APP_TITLE, font=("TkDefaultFont", 18, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            body,
            text="每行粘贴一个视频链接。任务会依次下载，临时错误可自动重试。",
        ).pack(anchor="w", pady=(4, 14))

        ttk.Label(body, text="视频链接（每行一个）").pack(anchor="w")
        entry_frame = ttk.Frame(body)
        entry_frame.pack(fill="x", pady=(5, 10))
        self.urls_text = tk.Text(entry_frame, height=4, wrap="word")
        url_scroll = ttk.Scrollbar(entry_frame, command=self.urls_text.yview)
        self.urls_text.configure(yscrollcommand=url_scroll.set)
        self.urls_text.pack(side="left", fill="x", expand=True)
        url_scroll.pack(side="right", fill="y")

        options_row = ttk.Frame(body)
        options_row.pack(fill="x", pady=(0, 12))
        self.add_button = ttk.Button(
            options_row, text="添加到队列", command=self._enqueue_urls
        )
        self.add_button.pack(side="left")
        ttk.Label(options_row, text="失败后重试").pack(side="left", padx=(18, 4))
        self.retry_spinbox = ttk.Spinbox(
            options_row, from_=0, to=5, width=4, textvariable=self.max_retries_var
        )
        self.retry_spinbox.pack(side="left")
        ttk.Label(options_row, text="次（首次失败后等待 2、4、8… 秒）").pack(
            side="left", padx=(5, 0)
        )

        ttk.Label(body, text="保存到").pack(anchor="w")
        folder_row = ttk.Frame(body)
        folder_row.pack(fill="x", pady=(5, 12))
        self.folder_entry = ttk.Entry(folder_row, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(
            folder_row, text="选择目录…", command=self._choose_folder
        )
        self.browse_button.pack(side="left", padx=(8, 0))

        ttk.Label(body, text="下载队列").pack(anchor="w")
        table_frame = ttk.Frame(body)
        table_frame.pack(fill="both", expand=True, pady=(5, 9))
        self.queue_tree = ttk.Treeview(
            table_frame,
            columns=("url", "status", "attempts"),
            show="headings",
            selectmode="extended",
            height=8,
        )
        self.queue_tree.heading("url", text="视频链接")
        self.queue_tree.heading("status", text="状态")
        self.queue_tree.heading("attempts", text="尝试次数")
        self.queue_tree.column("url", width=540, minwidth=300, stretch=True)
        self.queue_tree.column("status", width=100, minwidth=90, anchor="center")
        self.queue_tree.column("attempts", width=90, minwidth=80, anchor="center")
        tree_scroll = ttk.Scrollbar(
            table_frame, orient="vertical", command=self.queue_tree.yview
        )
        self.queue_tree.configure(yscrollcommand=tree_scroll.set)
        self.queue_tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.remove_button = ttk.Button(
            actions, text="移除选中项", command=self._remove_selected
        )
        self.remove_button.pack(side="left")
        self.start_button = ttk.Button(
            actions, text="开始队列", command=self._start_queue
        )
        self.start_button.pack(side="left", padx=(7, 0))
        self.cancel_button = ttk.Button(
            actions, text="取消队列", command=self._cancel_queue, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(7, 0))
        self.retry_button = ttk.Button(
            actions, text="重试失败项", command=self._retry_failed
        )
        self.retry_button.pack(side="left", padx=(7, 0))
        ttk.Label(actions, textvariable=self.status_var).pack(
            side="left", padx=(12, 0), fill="x", expand=True
        )

        self.progress = ttk.Progressbar(
            body, maximum=100, variable=self.progress_var, mode="determinate"
        )
        self.progress.pack(fill="x", pady=(10, 10))

        ttk.Label(body, text="运行信息").pack(anchor="w")
        log_frame = ttk.Frame(body)
        log_frame.pack(fill="both", expand=True, pady=(5, 9))
        self.log_text = tk.Text(
            log_frame,
            height=7,
            wrap="word",
            state="disabled",
            font=("TkFixedFont", 10),
        )
        log_scroll = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        ttk.Label(
            body,
            text="仅保存你有权保存的公开视频。不读取浏览器 Cookie，不绕过 DRM、加密、登录或地区限制。跨站播放器请使用扩展扫描。",
            wraplength=800,
        ).pack(anchor="w")

    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(
            title="选择保存目录", initialdir=self.folder_var.get() or str(Path.home())
        )
        if selected:
            self.folder_var.set(selected)

    def _enqueue_urls(self) -> None:
        raw_urls = self.urls_text.get("1.0", "end").splitlines()
        urls = [line.strip() for line in raw_urls if line.strip()]
        if not urls:
            messagebox.showinfo("没有链接", "请先粘贴 HTTP(S) 视频或页面链接。")
            return

        invalid = [url for url in urls if not is_public_web_url(url)]
        if invalid:
            preview = "\n".join(invalid[:5])
            if len(invalid) > 5:
                preview += "\n…"
            messagebox.showerror(
                "链接无效",
                "只接受公开 HTTP(S) 链接，不接受本地地址或含账号密码的 URL。以下链接未加入：\n\n" + preview,
            )
            return

        known_urls = {job.url for job in self.jobs}
        added = 0
        duplicates = 0
        for url in urls:
            if url in known_urls:
                duplicates += 1
                continue
            job = DownloadJob(self.next_job_id, url)
            self.next_job_id += 1
            self.jobs.append(job)
            self.queue_tree.insert(
                "", "end", iid=self._tree_id(job.job_id),
                values=(job.url, job.status, "0"),
            )
            known_urls.add(url)
            added += 1

        self.urls_text.delete("1.0", "end")
        self.status_var.set("已添加 {} 项{}".format(
            added, "，跳过重复链接 {} 项".format(duplicates) if duplicates else ""
        ))
        self._append_log("加入队列 {} 项。".format(added))

    def _remove_selected(self) -> None:
        selected = self.queue_tree.selection()
        if not selected:
            return
        if self._is_busy():
            messagebox.showinfo("队列运行中", "请等当前队列结束后再移除任务。")
            return
        selected_ids = {self._job_id_from_tree(item) for item in selected}
        self.jobs = [job for job in self.jobs if job.job_id not in selected_ids]
        for item in selected:
            self.queue_tree.delete(item)
        self.status_var.set("已移除选中项")

    def _start_queue(self, only_job_ids: set[int] | None = None) -> None:
        if self._is_busy():
            return
        if not self.jobs:
            messagebox.showinfo("队列为空", "请先添加视频链接。")
            return
        output_dir = Path(self.folder_var.get()).expanduser()
        if not output_dir.exists() or not output_dir.is_dir():
            messagebox.showerror("目录无效", "请选择一个已存在的保存目录。")
            return
        try:
            max_retries = max(0, min(5, int(self.max_retries_var.get())))
        except (ValueError, tk.TclError):
            messagebox.showerror("重试次数无效", "重试次数须为 0 到 5 的整数。")
            return

        pending = [
            (job.job_id, job.url)
            for job in self.jobs
            if job.status in {WAITING, CANCELLED}
            and (only_job_ids is None or job.job_id in only_job_ids)
        ]
        if not pending:
            messagebox.showinfo("没有待下载项", "队列中没有等待中的任务。")
            return

        self.cancel_event.clear()
        self.progress_var.set(0)
        self._set_busy(True)
        self.status_var.set("队列准备开始，共 {} 项".format(len(pending)))
        self._append_log("队列开始：{} 项，失败后最多重试 {} 次。".format(
            len(pending), max_retries
        ))
        self.worker = threading.Thread(
            target=self._queue_worker,
            args=(pending, output_dir, max_retries),
            daemon=True,
        )
        self.worker.start()

    def _queue_worker(
        self, pending: list[tuple[int, str]], output_dir: Path, max_retries: int
    ) -> None:
        cancelled = False
        for position, (job_id, url) in enumerate(pending, start=1):
            if self.cancel_event.is_set():
                cancelled = True
                break
            self.events.put(("progress", job_id, 0, 0, 0))
            self.events.put(("status", "正在处理第 {} / {} 项".format(position, len(pending))))

            for attempt in range(1, max_retries + 2):
                if self.cancel_event.is_set():
                    self.events.put(("job_state", job_id, CANCELLED, attempt - 1, ""))
                    cancelled = True
                    break
                self.events.put(("job_state", job_id, DOWNLOADING, attempt, ""))
                self.events.put(("status", "第 {} / {} 项：第 {} 次尝试".format(
                    position, len(pending), attempt
                )))
                try:
                    saved_path = self._download_one(job_id, url, output_dir)
                except Exception as exc:
                    error = str(exc).strip() or exc.__class__.__name__
                    if self.cancel_event.is_set():
                        self.events.put(("job_state", job_id, CANCELLED, attempt, error))
                        self.events.put(("log", job_id, "已取消当前任务。"))
                        cancelled = True
                        break
                    if is_access_restriction(error):
                        self.events.put(("job_state", job_id, FAILED, attempt, error))
                        self.events.put(("log", job_id, "检测到访问限制，不自动重试：{}".format(error)))
                        break
                    if attempt <= max_retries:
                        delay = min(2 ** attempt, 30)
                        self.events.put(("job_state", job_id, RETRY_WAIT, attempt, error))
                        self.events.put(("log", job_id, "下载失败：{}；{} 秒后重试（{}/{}）。".format(
                            error, delay, attempt, max_retries
                        )))
                        if self.cancel_event.wait(delay):
                            self.events.put(("job_state", job_id, CANCELLED, attempt, error))
                            cancelled = True
                            break
                        continue
                    self.events.put(("job_state", job_id, FAILED, attempt, error))
                    self.events.put(("log", job_id, "达到最大重试次数，任务失败：{}".format(error)))
                    break
                else:
                    self.events.put(("job_state", job_id, COMPLETED, attempt, ""))
                    self.events.put(("job_success", job_id, str(saved_path)))
                    break

            if cancelled:
                break

        self.events.put(("queue_done", cancelled))

    def _download_one(self, job_id: int, url: str, output_dir: Path) -> Path:
        return download_video(
            url=url,
            output_dir=output_dir,
            cancel_event=self.cancel_event,
            on_progress=lambda percent, received, total: self.events.put(
                ("progress", job_id, percent, received, total)
            ),
            on_log=lambda text: self.events.put(("log", job_id, text)),
            on_status=lambda text: self.events.put(("status", text)),
        )

    def _retry_failed(self) -> None:
        if self._is_busy():
            return
        failed_jobs = [job for job in self.jobs if job.status == FAILED]
        if not failed_jobs:
            messagebox.showinfo("没有失败项", "当前队列没有失败任务。")
            return
        for job in failed_jobs:
            job.status = WAITING
            job.attempts = 0
            job.error = ""
            self._render_job(job)
        self._append_log("重新排队 {} 个失败项。".format(len(failed_jobs)))
        self._start_queue({job.job_id for job in failed_jobs})

    def _cancel_queue(self) -> None:
        self.cancel_event.set()
        self.status_var.set("正在停止当前任务…")
        self.cancel_button.configure(state="disabled")

    def _set_busy(self, busy: bool) -> None:
        busy_state = "disabled" if busy else "normal"
        self.urls_text.configure(state=busy_state)
        self.add_button.configure(state=busy_state)
        self.folder_entry.configure(state=busy_state)
        self.browse_button.configure(state=busy_state)
        self.retry_spinbox.configure(state=busy_state)
        self.remove_button.configure(state=busy_state)
        self.start_button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.retry_button.configure(state="disabled" if busy else "normal")

    def _is_busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _render_job(self, job: DownloadJob) -> None:
        retry_limit = max(0, min(5, self._retry_limit_for_display()))
        attempts = "{}/{}".format(job.attempts, retry_limit + 1)
        if job.status == WAITING:
            attempts = "0/{}".format(retry_limit + 1)
        tree_id = self._tree_id(job.job_id)
        values = (job.url, job.status, attempts)
        if self.queue_tree.exists(tree_id):
            self.queue_tree.item(tree_id, values=values)

    def _retry_limit_for_display(self) -> int:
        try:
            return int(self.max_retries_var.get())
        except (ValueError, tk.TclError):
            return 3

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _drain_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    _, job_id, text = event
                    self._append_log("[{}] {}".format(job_id, text))
                elif kind == "progress":
                    _, job_id, percent, received, total = event
                    self.progress_var.set(percent)
                    if total:
                        self.status_var.set(
                            "任务 {}：{:.1f}%（{} / {}）".format(
                                job_id, percent, human_size(received), human_size(total)
                            )
                        )
                    else:
                        self.status_var.set(
                            "任务 {}：已接收 {}".format(job_id, human_size(received))
                        )
                elif kind == "status":
                    self.status_var.set(event[1])
                elif kind == "job_state":
                    _, job_id, status, attempts, error = event
                    job = self._find_job(job_id)
                    if job:
                        job.status = status
                        job.attempts = attempts
                        job.error = error
                        self._render_job(job)
                elif kind == "job_success":
                    _, job_id, output_path = event
                    job = self._find_job(job_id)
                    if job:
                        job.output_path = output_path
                    self._append_log("[{}] 已保存：{}".format(job_id, output_path))
                elif kind == "queue_done":
                    cancelled = event[1]
                    self._set_busy(False)
                    completed = sum(job.status == COMPLETED for job in self.jobs)
                    failed = sum(job.status == FAILED for job in self.jobs)
                    waiting = sum(job.status in {WAITING, CANCELLED} for job in self.jobs)
                    prefix = "队列已取消" if cancelled else "队列处理结束"
                    self.status_var.set(
                        "{}：完成 {}，失败 {}，待处理 {}".format(
                            prefix, completed, failed, waiting
                        )
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._drain_events)

    def _find_job(self, job_id: int) -> DownloadJob | None:
        return next((job for job in self.jobs if job.job_id == job_id), None)

    @staticmethod
    def _tree_id(job_id: int) -> str:
        return "job-{}".format(job_id)

    @staticmethod
    def _job_id_from_tree(tree_id: str) -> int:
        return int(tree_id.removeprefix("job-"))

    def _on_close(self) -> None:
        if self._is_busy():
            self.cancel_event.set()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    BiliDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
