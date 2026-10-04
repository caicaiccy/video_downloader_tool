"""Directory persistence, OS dispatch and default-player behavior."""
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import desktop_actions as desktop
import native_host as host


class DesktopTests(unittest.TestCase):
    def test_directory_persistence_cancel_reset_and_queue_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            default = root / "Downloads"
            chosen = root / "自定义 下载"
            chosen.mkdir()
            with patch.object(desktop, "settings_file", return_value=root / "settings.json"), \
                 patch.object(desktop, "default_directory", return_value=default), \
                 patch.object(desktop, "run_dialog", return_value={"ok": True, "path": str(chosen)}):
                self.assertEqual(desktop.output_directory(), default)
                desktop.perform_action("choose_directory")
                self.assertEqual(desktop.output_directory(), chosen)
                self.assertEqual(json.loads((root / "settings.json").read_text())["outputDirectory"], str(chosen))
                with patch.object(desktop, "run_dialog", return_value={"ok": True, "cancelled": True}):
                    desktop.perform_action("choose_directory")
                    self.assertEqual(desktop.output_directory(), chosen)
                destinations = []
                def capture(jobs, destination, retries):
                    destinations.append(destination)
                    host.queue_active.clear()
                with patch.object(host, "run_queue", side_effect=capture), patch.object(host, "send_message"):
                    host.start_queue({"jobs": [{"id": "test", "url": "https://example.com/video"}]})
                    host.worker.join(timeout=2)
                self.assertEqual(destinations, [chosen])
                desktop.perform_action("reset_directory")
                self.assertEqual(desktop.output_directory(), default)
                self.assertTrue(default.is_dir())

    def test_paths_and_missing_video_are_rejected_before_launch(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(desktop, "run_dialog") as launch:
            root = Path(temporary).resolve()
            executable = root / "script.command"
            executable.write_text("not a video")
            for value in ("relative.mp4", str(root / "missing.mp4"), str(executable), str(root)):
                with self.assertRaises(ValueError): desktop.perform_action("play_video", value)
            launch.assert_not_called()
            with self.assertRaises(ValueError): desktop.validate_directory("relative")
            with self.assertRaises(ValueError): desktop.validate_directory(str(executable))

    def test_invalid_settings_fall_back_and_video_mode_is_forwarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            config = root / "settings.json"
            config.write_text('{"outputDirectory":"relative"}')
            video = root / "video ' 中文.mp4"
            video.write_bytes(b"video")
            with patch.object(desktop, "settings_file", return_value=config), patch.object(desktop, "default_directory", return_value=root / "Downloads"):
                self.assertEqual(desktop.output_directory(), root / "Downloads")
            with patch.object(desktop, "run_dialog", return_value={"ok": True}) as launch:
                desktop.perform_action("play_video", str(video), True)
                launch.assert_called_once_with("play_video", video, True)

    def test_windows_missing_default_uses_system_settings_and_open_with(self):
        video = Path("/tmp/video.mp4")
        error = OSError("no association")
        error.winerror = 1155
        with patch.object(desktop.os, "name", "nt"), patch.object(desktop.os, "startfile", create=True, side_effect=[error, None]) as open_file, \
             patch.object(desktop, "_windows_open_with") as picker:
            result = desktop._other_dialog("play_video", video, False)
            self.assertIn("默认应用", result["message"])
            self.assertEqual(open_file.call_args_list[1].args[0], "ms-settings:defaultapps")
            picker.assert_called_once_with(video)
        with patch.object(desktop.os, "name", "nt"), patch.object(desktop.os, "startfile", create=True) as open_file, \
             patch.object(desktop, "_windows_open_with") as picker:
            desktop._other_dialog("play_video", video, True)
            open_file.assert_not_called()
            picker.assert_called_once_with(video)

    def test_mac_default_open_ctrl_picker_and_unassociated_setup(self):
        workspace = Mock()
        workspace.URLForApplicationToOpenURL_.return_value = "default-player"
        panel = Mock()
        panel.runModal.return_value = 1
        panel.URL.return_value = "selected-player"
        alert = Mock()
        alert.runModal.return_value = 1000
        appkit = types.SimpleNamespace(NSApplication=Mock(), NSApplicationActivationPolicyAccessory=1,
            NSOpenPanel=Mock(openPanel=Mock(return_value=panel)), NSModalResponseOK=1,
            NSWorkspace=Mock(sharedWorkspace=Mock(return_value=workspace)), NSWorkspaceOpenConfiguration=Mock(),
            NSAlert=Mock(), NSAlertFirstButtonReturn=1000)
        appkit.NSAlert.alloc.return_value.init.return_value = alert
        foundation = types.SimpleNamespace(NSURL=Mock(fileURLWithPath=Mock()))
        with patch.dict(sys.modules, {"AppKit": appkit, "Foundation": foundation}), \
             patch.object(desktop, "_cocoa_completion", side_effect=lambda operation: operation(lambda *args: None)), \
             patch.object(desktop, "_set_mac_default") as set_default:
            desktop._mac_dialog("play_video", Path("/tmp/video.mp4"), False)
            workspace.openURL_configuration_completionHandler_.assert_called_once()
            panel.runModal.assert_not_called()
            desktop._mac_dialog("play_video", Path("/tmp/video.mp4"), True)
            workspace.openURLs_withApplicationAtURL_configuration_completionHandler_.assert_called_once()
            set_default.assert_not_called()  # Ctrl is a one-time choice, never changes associations.
            workspace.URLForApplicationToOpenURL_.return_value = None
            desktop._mac_dialog("play_video", Path("/tmp/video.mp4"), False)
            alert.runModal.assert_called_once()
            set_default.assert_called_once()
            set_default.reset_mock()
            alert.runModal.return_value = 1001
            result = desktop._mac_dialog("play_video", Path("/tmp/video.mp4"), False)
            self.assertTrue(result["cancelled"])
            set_default.assert_not_called()

    def test_native_action_response_and_busy_queue_guard(self):
        responded = threading.Event()
        messages = []
        def capture(message):
            messages.append(message)
            responded.set()
        with patch.object(host, "send_message", side_effect=capture), \
             patch.object(host, "perform_action", return_value={"ok": True, "outputDirectory": "/tmp/chosen"}) as action:
            host.desktop_action({"requestId": "request", "action": "get_directory"})
            self.assertTrue(responded.wait(2))
            self.assertEqual(messages[-1]["type"], "desktop_result")
            self.assertEqual(messages[-1]["outputDirectory"], "/tmp/chosen")
            host.queue_active.set()
            try:
                host.desktop_action({"requestId": "busy", "action": "choose_directory"})
                self.assertFalse(messages[-1]["ok"])
                self.assertEqual(action.call_count, 1)
            finally:
                host.queue_active.clear()


if __name__ == "__main__":
    unittest.main()
