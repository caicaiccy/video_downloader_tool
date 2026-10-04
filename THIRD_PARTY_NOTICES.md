# Third-party notices

This tool bundles third-party software in its packaged builds:

- **yt-dlp** — Unlicense. Source and license: <https://github.com/yt-dlp/yt-dlp>.
- **imageio-ffmpeg** — BSD 2-Clause. Source and license: <https://github.com/imageio/imageio-ffmpeg>.
- **FFmpeg** — the imageio-ffmpeg platform wheel includes an FFmpeg executable. FFmpeg licensing depends on the exact build configuration. See <https://ffmpeg.org/legal.html> and the imageio-ffmpeg binary build project at <https://github.com/imageio/imageio-binaries>.
- **PyInstaller** — GPL with a bootloader exception. Source and license: <https://github.com/pyinstaller/pyinstaller>.
- **PyObjC (macOS builds)** — MIT. Cocoa bridge source and license: <https://github.com/ronaldoussoren/pyobjc>.

The release maintainer should keep the exact dependency versions used for each build and review the corresponding upstream license and source-distribution requirements before redistributing packaged binaries.
