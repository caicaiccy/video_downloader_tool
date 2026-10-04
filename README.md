# 网页视频下载器（Bilibili + 通用网页）

Windows / macOS 桌面工具及 Chrome / Edge Manifest V3 扩展。队列顺序下载，失败退避重试，支持 Bilibili、yt-dlp 能解析的公开视频页面，以及浏览器已经提供的 MP4/WebM 等直链、未加密的 HLS 和 DASH 点播。只保存你有权保存的内容。

## 使用

- 桌面：每行输入 HTTP(S) 页面或媒体链接，选择目录，添加到队列并开始。默认失败重试 3 次，等待 2、4、8…秒；可取消或重试失败项。
- 扩展：粘贴网页链接、加入列表、开始队列，工具独立解析网页及嵌入播放器，媒体由扩展后台读取后交给 Host 合并，不需要打开对应网站，也不依赖当前标签页。先按浏览器正常 CORS 请求，能读取时无需额外授权；读取失败且域名未授权时提供“授权视频来源并继续下载”。支持 HTML video/source、VideoObject、播放器公开的清单及简单 URL 赋值，最多读取 8 个页面、嵌套 3 层，每页上限 2 MB；多个同优先级资源会提示先扫描选择，避免自动下载错误视频。
- 动态播放器无法直接解析时，可正常打开并播放目标视频，再使用弹窗扫描。打开弹窗自动扫描一次，零结果隐藏；右上角图标可手动重扫。跨站 iframe 会显示待授权域名，用户点击“授权并扫描跨站播放器”后才读取。自动扫描不弹出新授权请求。扫描媒体由原播放器页面使用 credentials=omit 匿名读取并分块传给 Host，Host 校验清单和合并视频；只有这类扫描任务需要保持原页面打开。已有失败任务再次扫描并点击加入会更新传输上下文。
- Bilibili 建议使用页面链接，让原有提取器选择音视频与合并格式。
- 扩展默认保存到系统下载目录，可在右上角设置页选择并记住其他目录、调整重试次数，也可直接打开目录。队列按下载中、待下载、失败、完成排列，同类按加入时间从晚到早；最多显示 5 条，更多可滚动。输入链接和加入队列区域位于队列下方；输入框默认一行，自动增高到最多 5.5 行，超出可滚动。完成任务提供默认播放器播放，Ctrl + 点击选择其他应用；无默认播放器时进入系统设置流程。扫描候选不代表已验证可下载。

扩展为 1.5.2，本地 Host 为 1.5.1；网页链接后台下载需要 Host 的 `page_scan` / `background_http` 能力。下载中加入的新链接会在当前批次结束后自动继续，启动失败会显示到对应任务。所选线路只有不透明运行时令牌时，Host 会尝试网页明确列出的同集备用线路，寻找公开 HLS/DASH 地址；不执行或解码私有播放器代码。Native Host 名称、可执行文件名与安装目录保留原有 `BiliDownloader` 名称，旧版 Bilibili 队列仍可读取。扩展识别旧 Host 后会提示升级，避免静默丢弃通用任务。安装步骤见 [扩展说明](browser-extension/README.md)。

## 支持边界

不导入浏览器 Cookie、账号令牌或自定义认证头，不解码站点的私有播放参数，也不绕过 DRM、登录、会员、付费墙、验证码、地区限制或其他访问控制。Bilibili 与桌面界面使用 Host 原有提取下载；扩展的通用网页链接通过 Host 读取 HTML、通过后台浏览器正常 fetch 读取媒体，扫描媒体则由原播放器帧读取。均不携带浏览器凭据、不伪造 Origin/Referer/UA、不修改代理或其他网络设置。浏览器可播放不等于匿名读取可用，登录或地区拒绝仍会停止。浏览器扫描仅传递公开媒体 URL、来源页、标题、类型和当前标签/帧标识；资源 URL 可能包含站点公开提供的临时签名，过期时需重新扫描。

HLS 清单出现非 NONE 的 KEY / SESSION-KEY 即拒绝（包括普通 AES-128）；DASH ContentProtection / DRM 即拒绝。检查在提取和下载请求中均执行，包含子清单；不请求密钥、不跳过丢失分段。暂不支持直播、非 HTTP(S)、只有 blob: 而未公开底层地址的资源、特殊分段伪装或未知播放器协议。扫描不会识别所有无扩展名网络端点；实际 video src 可作为直链候选。

页面链接优先使用 yt-dlp 的站点提取器，通用网页新增 HTML / iframe 解析，不启动浏览器、不执行页面脚本。必须运行 JS 才能生成地址、只有 blob: 或依赖浏览器传输的资源，仍可能需要打开播放器后扫描。浏览器扫描依赖 video/source、资源时序和公开内联脚本中的媒体 URL 字面值，可能包含广告或其他清晰度，需用户选择。iframe 域名权限由浏览器持久保存，可在扩展“网站访问权限”中撤销。默认没有全站授权、后台抓包或 Cookie 权限。

Agedm 示例的实际媒体形式、403 结果及测试范围见 [兼容性验证记录](docs/COMPATIBILITY.md)。

## 开发与验证

需要 Python 3.10+（当前依赖在 Python 3.12 验证）及 Node.js 18+ 用于扩展测试。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
node tests/test_extension.cjs
node tests/test_background_fetch.cjs
```

测试只生成 1 秒视频并在回环地址启动临时 HTTP 服务，不下载网站正片。覆盖真实 MP4、HLS、DASH、HTML video 解析、重复下载返回路径、加密拒绝、队列重试/取消及 Native Messaging 帧。测试还覆盖浏览器分块顺序、回压、取消、CORS/匿名读取失败边界、跨帧来源校验及真实 HLS/DASH/MP4 分块转发后合并解码。测试传输层把命名 fixture URL 映射到本机；生产代码拒绝本地地址，无测试绕过开关。

另已对用户指定的 Agedm 网页链接完成真实后台下载验收，保存完整 1080p 视频并全量解码通过，详情见 [1.5.0 验证记录](docs/BACKGROUND-LINK-1.5.0.md)。

## 便携构建

- Windows x64：PowerShell 执行 `scripts/build_windows.ps1`。可用 `$env:BUILD_PYTHON` 指定 Python，输出 `dist/BiliDownloader-windows-x64.zip`。
- macOS：`BUILD_PYTHON=/绝对路径/.venv/bin/python sh scripts/build_macos.sh`，或默认 `python3`。输出 `.app` 与 `dist/BiliDownloader-macos-<架构>.zip`。
- 构建先运行测试，PyInstaller 包含 yt-dlp、FFmpeg、Python 运行时、Host、扩展、安装脚本和兼容性说明。
- 两平台都生成 `BiliDownloader-source.zip` 和 `BiliDownloader-chrome-edge-extension.zip`。仅打源码/扩展可运行 `python scripts/package_source.py`。
- `.github/workflows/build-portable.yml` 保留 Windows x64、macOS arm64 与 x86_64 的原生 runner 构建。必须在目标 OS 构建；Mac 无法直接生成 Windows EXE。

产物未签名或公证。macOS 首次运行可能需要 Finder 中 Control 点击应用后选择“打开”。第三方信息见 `THIRD_PARTY_NOTICES.md`。
