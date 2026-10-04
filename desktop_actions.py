"""Local directory preferences and OS-native file/application dialogs.

Dialogs run in a separate process so Native Messaging can keep handling media
chunks even while a system panel is open. No shell command accepts user text.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".flv", ".mpeg", ".mpg", ".ts", ".m2ts", ".ogv"}
ACTIONS = {"get_directory", "choose_directory", "reset_directory", "open_directory", "play_video"}


def default_directory() -> Path:
    if os.name == "nt":
        # The Windows Downloads known folder may have been moved by the user.
        import ctypes
        import uuid
        identifier = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le)
        location = ctypes.c_wchar_p()
        shell = ctypes.windll.shell32
        shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        shell.SHGetKnownFolderPath.restype = ctypes.c_long
        if shell.SHGetKnownFolderPath(ctypes.byref(identifier), 0, None, ctypes.byref(location)) == 0:
            try:
                return Path(location.value)
            finally:
                ctypes.windll.ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
                ctypes.windll.ole32.CoTaskMemFree(ctypes.cast(location, ctypes.c_void_p))
    return Path.home() / "Downloads"


def settings_file() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "BiliDownloader" / "settings.json"


def validate_directory(value: str) -> Path:
    if not isinstance(value, str) or not value or len(value) > 8000 or "\0" in value:
        raise ValueError("下载目录路径无效。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("下载目录必须使用完整路径。")
    path = path.resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("下载目录不能是文件。")
    return path


def output_directory() -> Path:
    config = settings_file()
    if config.is_file():
        try:
            data = json.loads(config.read_text(encoding="utf-8"))
            return validate_directory(data["outputDirectory"])
        except (ValueError, KeyError, TypeError):
            # Corrupt settings never select an unintended path.
            pass
    return default_directory()


def save_directory(path: Path) -> None:
    config = settings_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".settings-", dir=config.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"outputDirectory": str(validate_directory(str(path)))}, stream, ensure_ascii=False)
        os.replace(temporary, config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def video_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value or len(value) > 8000:
        raise ValueError("视频路径无效。")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("视频需要完整路径。")
    path = path.resolve()
    if not path.is_file():
        raise ValueError("视频文件已移动或删除，请检查下载目录。")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise ValueError("只支持打开已下载的视频文件。")
    return path


def run_dialog(action: str, path: Path, choose_application: bool = False) -> dict:
    command = [sys.executable]
    if not getattr(sys, "frozen", False):
        command.append(str(Path(__file__).with_name("native_host.py")))
    command += ["--desktop-action", action, str(path), "choose" if choose_application else "default"]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=600,
                            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
    if result.returncode:
        raise RuntimeError("系统文件操作失败，请重试。")
    try:
        response = json.loads(result.stdout)
    except ValueError as exc:
        raise RuntimeError("系统文件操作未返回有效结果。") from exc
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "系统文件操作失败。")
    return response


def perform_action(action: str, path: str = "", choose_application: bool = False) -> dict:
    if action not in ACTIONS:
        raise ValueError("不支持的文件操作。")
    if action == "play_video":
        return run_dialog(action, video_path(path), choose_application)
    directory = output_directory()
    if action == "reset_directory":
        directory = default_directory()
        directory.mkdir(parents=True, exist_ok=True)
        save_directory(directory)
        return {"ok": True, "outputDirectory": str(directory), "message": "已恢复系统下载目录。"}
    if action == "choose_directory":
        result = run_dialog(action, directory)
        if result.get("cancelled"):
            return {"ok": True, "outputDirectory": str(directory), "cancelled": True}
        directory = validate_directory(result.get("path"))
        if not directory.is_dir() or not os.access(directory, os.W_OK):
            raise ValueError("请选择可写的文件夹。")
        save_directory(directory)
        return {"ok": True, "outputDirectory": str(directory), "message": "下载目录已更新。"}
    if action == "open_directory":
        directory.mkdir(parents=True, exist_ok=True)
        result = run_dialog(action, directory)
        result["outputDirectory"] = str(directory)
        return result
    return {"ok": True, "outputDirectory": str(directory)}


def _cocoa_completion(operation) -> None:
    from Foundation import NSRunLoop, NSDate
    done = []
    operation(lambda *args: done.append(args[-1]))
    deadline = time.monotonic() + 30
    while not done and time.monotonic() < deadline:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
    if not done:
        raise RuntimeError("系统打开操作超时。")
    if done[0] is not None:
        raise RuntimeError(str(done[0].localizedDescription()))


def _mac_dialog(action: str, path: Path, choose_application: bool) -> dict:
    from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSOpenPanel,
                        NSModalResponseOK, NSWorkspace, NSWorkspaceOpenConfiguration, NSAlert,
                        NSAlertFirstButtonReturn)
    from Foundation import NSURL
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    workspace = NSWorkspace.sharedWorkspace()
    url = NSURL.fileURLWithPath_(str(path))
    if action == "open_directory":
        if not workspace.openURL_(url):
            raise RuntimeError("无法在访达中打开下载目录。")
        return {"ok": True, "message": "已打开下载目录。"}
    if action == "choose_directory":
        app.activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择下载目录")
        panel.setPrompt_("选择目录")
        panel.setCanChooseFiles_(False)
        panel.setCanChooseDirectories_(True)
        panel.setAllowsMultipleSelection_(False)
        panel.setCanCreateDirectories_(True)
        panel.setDirectoryURL_(url)
        if panel.runModal() != NSModalResponseOK:
            return {"ok": True, "cancelled": True}
        return {"ok": True, "path": str(panel.URL().path())}
    default_app = workspace.URLForApplicationToOpenURL_(url)
    needs_default = default_app is None and not choose_application
    if needs_default:
        app.activateIgnoringOtherApps_(True)
        alert = NSAlert.alloc().init()
        alert.setMessageText_("未设置默认播放器")
        alert.setInformativeText_("选择播放器，并将其设为此类视频的默认应用。")
        alert.addButtonWithTitle_("选择并设为默认")
        alert.addButtonWithTitle_("取消")
        if alert.runModal() != NSAlertFirstButtonReturn:
            return {"ok": True, "cancelled": True}
    if choose_application or needs_default:
        app.activateIgnoringOtherApps_(True)
        panel = NSOpenPanel.openPanel()
        panel.setTitle_("选择默认播放器" if needs_default else "用其他应用打开视频")
        panel.setPrompt_("设为默认并播放" if needs_default else "打开视频")
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setTreatsFilePackagesAsDirectories_(False)
        panel.setAllowedFileTypes_(["app"])
        panel.setDirectoryURL_(NSURL.fileURLWithPath_("/Applications"))
        if panel.runModal() != NSModalResponseOK:
            return {"ok": True, "cancelled": True}
        application = panel.URL()
        if needs_default:
            _set_mac_default(workspace, application, url)
        configuration = NSWorkspaceOpenConfiguration.configuration()
        _cocoa_completion(lambda completion: workspace.openURLs_withApplicationAtURL_configuration_completionHandler_(
            [url], application, configuration, completion))
    else:
        configuration = NSWorkspaceOpenConfiguration.configuration()
        _cocoa_completion(lambda completion: workspace.openURL_configuration_completionHandler_(url, configuration, completion))
    return {"ok": True, "message": "已打开视频。"}


def _set_mac_default(workspace, application, video_url) -> None:
    if hasattr(workspace, "setDefaultApplicationAtURL_toOpenContentTypeOfFileAtURL_completionHandler_"):
        _cocoa_completion(lambda completion: workspace.setDefaultApplicationAtURL_toOpenContentTypeOfFileAtURL_completionHandler_(
            application, video_url, completion))
        return
    # Launch Services is the public fallback on macOS before the newer API.
    import ctypes
    from Foundation import NSBundle
    content_type, error = workspace.typeOfFile_error_(str(video_url.path()), None)
    identifier = NSBundle.bundleWithURL_(application).bundleIdentifier()
    if error or not content_type or not identifier:
        raise RuntimeError("无法设置默认播放器。")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    services = ctypes.CDLL("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    services.LSSetDefaultRoleHandlerForContentType.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    services.LSSetDefaultRoleHandlerForContentType.restype = ctypes.c_int32
    content = cf.CFStringCreateWithCString(None, str(content_type).encode(), 0x08000100)
    bundle = cf.CFStringCreateWithCString(None, str(identifier).encode(), 0x08000100)
    try:
        if services.LSSetDefaultRoleHandlerForContentType(content, 0xffffffff, bundle):
            raise RuntimeError("系统未能设置默认播放器。")
    finally:
        cf.CFRelease(content)
        cf.CFRelease(bundle)


def _windows_open_with(path: Path) -> None:
    import ctypes
    from ctypes import wintypes
    class OpenAsInfo(ctypes.Structure):
        _fields_ = [("file", wintypes.LPCWSTR), ("class_name", wintypes.LPCWSTR), ("flags", ctypes.c_uint)]
    info = OpenAsInfo(str(path), None, 0x4)  # OAIF_EXEC: user chooses one app; no association change.
    shell = ctypes.windll.shell32
    shell.SHOpenWithDialog.argtypes = [wintypes.HWND, ctypes.POINTER(OpenAsInfo)]
    shell.SHOpenWithDialog.restype = ctypes.c_long
    result = shell.SHOpenWithDialog(None, ctypes.byref(info))
    if result not in (0, -2147023673):  # S_OK or HRESULT_FROM_WIN32(ERROR_CANCELLED)
        raise RuntimeError("无法打开系统应用选择窗口。")


def _other_dialog(action: str, path: Path, choose_application: bool) -> dict:
    if action == "choose_directory":
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            chosen = filedialog.askdirectory(parent=root, title="选择下载目录", initialdir=str(path), mustexist=True)
        finally:
            root.destroy()
        return {"ok": True, "path": chosen} if chosen else {"ok": True, "cancelled": True}
    if os.name != "nt":
        raise RuntimeError("当前平台未提供系统文件操作。")
    if action == "open_directory":
        os.startfile(str(path), "open")
        return {"ok": True, "message": "已打开下载目录。"}
    if choose_application:
        _windows_open_with(path)
    else:
        try:
            os.startfile(str(path), "open")
        except OSError as exc:
            if getattr(exc, "winerror", None) != 1155:
                raise
            # Windows 10+ owns association changes; never edit registry defaults.
            os.startfile("ms-settings:defaultapps")
            _windows_open_with(path)
            return {"ok": True, "message": "请在系统默认应用设置中选择视频播放器。"}
    return {"ok": True, "message": "已打开视频。"}


def helper_main(arguments: list[str]) -> None:
    try:
        action, raw_path, mode = arguments
        if action not in {"choose_directory", "open_directory", "play_video"} or mode not in {"default", "choose"}:
            raise ValueError("不支持的系统操作。")
        path = video_path(raw_path) if action == "play_video" else validate_directory(raw_path)
        result = _mac_dialog(action, path, mode == "choose") if sys.platform == "darwin" else _other_dialog(action, path, mode == "choose")
    except Exception as exc:
        result = {"ok": False, "error": str(exc) or "系统文件操作失败。"}
    print(json.dumps(result, ensure_ascii=True))
