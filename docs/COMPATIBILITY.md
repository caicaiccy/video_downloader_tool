# 兼容性分析与验证（2026-10-04，Asia/Singapore）

## 1.1.0 阶段：Agedm 实际媒体

示例：[https://www.agedm.io/play/20230189/2/1](https://www.agedm.io/play/20230189/2/1)。检查通过正常浏览器页面及其 DOM 完成，没有解析/解密站点的 `age_...` 私有参数，没有登录或导出 Cookie。

- 顶层没有 video 标签，存在 `iframe#iframeForVideo`，播放器域名为 `https://jx.wuzhoupai.com:8443`，路径 `/m3u8/`。
- iframe 公开内联脚本声明 `Vurl = 'https://vip.ffzyread.com/20231004/17545_73bf4be7/index.m3u8'`，载入 hls.js，调用 `hls.loadSource(url)` 并 attachMedia 到 Artplayer 的 video。
- iframe 的 video.currentSrc 为 `blob:https://jx.wuzhoupai.com:8443/...`，readyState=4，duration≈1439.688 秒，mediaKeys=false。由公开脚本及播放方式可确认这是 HLS 经 MediaSource 播放；mediaKeys=false 不足以证明 HLS 未加密。
- 本机匿名请求这个公开 HLS 地址返回 **HTTP 403 Forbidden，正文 “The region has been denied.”**，已停止。未请求分段、密钥或下载正片，也未更换代理/身份。

因此旧版不兼容（弹窗与 Host 都限定 Bilibili）。1.1.0 能通过跨站 iframe 授权后的通用扫描找到公开 HLS 字面值，不需要 Agedm 专有解密适配；blob: 不会传给 Host。当前环境尚不能确认该 CDN 清单是否加密，也不能声明 Agedm 下载成功。真实匿名请求仍可能返回地区限制；Host 对该错误只尝试一次。

网站及线路可变化，以上仅针对检查时该集的第二条线路，不推断其他线路是否公开或可下载。

## 1.1.0 阶段：已执行验证

Python 3.12.14、yt-dlp 2026.8.19、imageio-ffmpeg 0.6.0、Node.js；macOS arm64。

- 11 项 Python unittest 通过。真实生成 1 秒 MP4、HLS VOD、DASH 音视频，使用生产提取/下载/合并路径保存，再用 FFmpeg 解码检查；通用 HTML video 页面及重复任务的输出路径也通过。
- 加密 HLS、主清单指向加密子清单、DASH ContentProtection 均拒绝；确认未请求 key 和媒体分段。HLS METHOD=NONE 可接受，直播清单拒绝。
- URL/旧 Bilibili任务验证、Host 参数白名单、队列临时错误重试、403 不重试、取消与剩余任务停止、Native Messaging 长度帧和能力握手通过。
- Node 扩展测试通过：候选去重、blob 提示、公开脚本字面值（使用上述 Agedm Vurl 样例）、EME 标记、跨站域名提示、HTTP(S) 与 Bilibili 域名边界、无默认全站/Cookie 权限；弹窗扫描→指定域名授权→媒体去重入队→Host 参数传递、旧 Host 提示、连续消息的状态保存也通过。

测试服务器仅在回环地址提供本地生成的视频；命名 fixture URL 通过测试传输层映射，不存在生产本地地址绕过开关。

macOS arm64 完整包已重新编译，打包后的 Native Host 二进制启动及 1.1.0 能力握手、非法 URL 拒绝均通过。源码、扩展 ZIP 与完整包已更新；未自动安装或注册到浏览器。

实现依据：[Chrome scripting 官方文档](https://developer.chrome.com/docs/extensions/reference/api/scripting)、[Chrome match patterns 官方文档](https://developer.chrome.com/docs/extensions/develop/concepts/match-patterns)。

## 1.1.0 阶段：尚未验证的范围

Chrome/Edge 真实安装、域名授权弹窗和扩展到 Host 的完整交互需各浏览器验收；测试使用实际消息帧与模拟扩展 API，不等同于全流程浏览器安装验证。Windows EXE 与 macOS x86_64 需相应 runner 构建与运行验证；本机为 macOS arm64。Bilibili 原有域名识别和队列兼容已回归，未额外下载网站正片。

匿名请求权限、清单过期、CDN网络环境和站点变化都可能影响兼容性。浏览器公开资源发现与 Native Host 匿名下载分别验证，发现资源不意味着下载可用。


## 1.2.0 修复与本次复测

用户截图中的错误为下载阶段 HTTP 403。复测时主 HLS 清单可以读取，二级清单 `/3000k/hls/mixed.m3u8` 返回 “The region has been denied.”。旧版不会区分失败阶段，且媒体发现后全部交给本机独立网络请求，未使用当前播放器的正常浏览器传输。

1.2.0 的新扫描任务带上当前标签与播放器帧信息。扩展在已授权的原帧中执行普通 GET/HEAD fetch，明确 credentials=omit，不携带 Cookie/账号认证，不伪造 Origin、Referer 或 UA，不修改代理/路由/地区设置，并保留浏览器 CORS 检查。清单与分段以有序分块、有接收确认和内存/大小上限的方式传给 Host；Host 的加密/DRM/直播检查不变。浏览器匿名请求被拒绝时直接停止，不回退到另一网络身份。旧失败媒体通过重新扫描并加入同一 URL 更新为浏览器任务。

已知 HLS/DASH 清单使用播放器的正常 GET 流程；不额外执行不必要的 HEAD。401/403 错误现在在首次拒绝时明确显示清单/资源及地区拒绝原因，避免 yt-dlp 非致命预探测继续到下载阶段后只显示泛化 Forbidden。

本次 14 项 Python 测试通过，新增浏览器转发的真实 MP4/HLS/DASH 合并解码、取消/乱序拒绝、地区拒绝及时停止；Node 测试通过匿名 fetch、分块接收确认、帧来源校验、跨页面失效及旧任务更新。

已在现有 Chrome 授权 ID 下更新已注册 Host，并验证已安装位置返回 1.2.0 / browser_http。修复 macOS 安装器直接覆盖已签名可执行文件后出现 exit -9 的问题：改为临时新文件再原子替换，安装后启动通过。扩展已加载路径由用户确认为项目 browser-extension 目录，源文件已更新；扩展管理页被浏览器安全策略禁止自动访问，重新加载需用户完成。

## 1.2.0：网站完整下载验收通过

用户完成扩展重新加载后，在现有 Chrome 页面扫描媒体，将同一资源更新为浏览器任务并启动队列。扩展与已注册 Native Host 完成了此集的真实下载与合并，弹窗显示“完成 浏览器 · HLS”，尝试次数为 1，队列处理结束。完成截图保存为 `agedm-download-verified.png`。

- 文件：`~/Downloads/捡走被人悔婚的千金，教会她坏坏的幸福生活 第01集 - 在线播放 - AGE动漫 [2c822f97141c].mp4`
- 大小：211,452,711 字节；时长 00:23:59.76；1920×1080；H.264 视频、AAC 音频。
- 使用 FFmpeg 对整个文件的音视频解码，`-v error -xerror -fps_mode passthrough -enc_time_base:v -1 -f null -`，退出码 0，标准错误为空。保留输入时间基准，避免 null 输出量化时间戳造成伪 DTS 警告。
- 机器可读结果：`agedm-download-validation.json`。视频保存在用户下载目录，不随源码或安装包分发。

此次确认的是 macOS arm64、Chrome、所给链接第二条线路的完整流程。Edge、Windows EXE、macOS x86_64 尚未进行实体环境验收。站点或 CDN 后续拒绝匿名请求时仍应停止，不能由本次成功推断所有资源均可下载。

## 1.4.0：无需打开网站的网页链接解析

Host 新增独立 HTML/iframe 视频解析，Bilibili 专用提取器保持原行为。输入链接后启动队列即可自动解析下载；只有原浏览器扫描任务需要保持播放器页打开。27 项 Python 与扩展回归测试通过，真实生成的视频完成下载和解码。

Agedm 同一示例通过 Host 成功读取页面和 iframe，找到视频清单及单集名称；媒体请求仍收到 CDN 地区限制 403，本次没有完成独立 HTTP 下载。详细支持范围、测试及安装验证见 [网页链接解析记录](PAGE-LINK-1.4.0.md)。

## 1.5.0：网页链接后台完整下载验收通过

网页及 iframe 由 Host 解析，媒体改为扩展后台的正常匿名 fetch 传输，无需打开对应网站或使用播放器帧。未授权域名仍可通过服务端允许的 CORS 读取；只有请求失败且域名未授权才提供指定域名授权重试。

用户通过下载器自身的临时验证页，以原始 Agedm 网页链接完成真实下载与合并。最终 MP4 为 211,452,711 字节、00:23:59.76、1920×1080、H.264 / AAC；完整音视频解码退出码 0，错误输出为空。SHA-256 与 1.2.0 的播放器页面下载文件相同。真实验收使用生产后台读取模块及已安装 Host；正式弹窗和 service worker 的消息集成测试通过，使用新版弹窗需重新加载扩展。详细实现、范围和机器可读结果见 [网页链接后台下载记录](BACKGROUND-LINK-1.5.0.md)。

## 1.5.1：下载中新增任务自动续接

Chrome 扩展存储记录显示，复现任务在前一项完成前约 2 分 24 秒加入。旧后台把点击“开始队列”时的任务作为固定批次交给 Host，运行中新增的链接不在该批次内；批次结束后也没有继续读取等待项，因此任务一直显示“等待中”。

1.5.1 在每个正常批次结束后重新读取队列，并自动启动运行期间新增的等待项。网页链接在续接前补全 `background` 传输标记；需要域名授权的失败项不会在无用户手势时反复启动。队列取消不会自动续接。启动 Native Host 或版本检查失败时，任务改为“失败”并在任务行显示具体错误，不再保留误导性的“等待中”。

实际等待任务 `https://www.agedm.io/play/20250198/1/3` 启动后进一步暴露线路解析问题：所选西瓜线路的 iframe 仅公开不透明运行时令牌，同集非凡线路公开标准 HLS。Host 现在识别网页明确列出的同集备用来源并逐个受限尝试；找到公开媒体即交给后台匿名传输，不执行、还原或解码私有播放器代码。

扩展集成测试新增两项回归：运行中加入网页链接后，前一批次结束会发送第二个 Host 队列且使用后台传输；启动失败会把任务状态和错误写回存储。Python 新增同集备用来源测试，并对解析所得 HLS 完整下载、合并和解码。扩展模块测试及 28 项 Python 下载、合并、策略与解码测试全部通过。备用线路解析需要 Host 1.5.1。

正式 Chrome 弹窗随后重试上述实际等待任务并完成下载。关闭诊断网站页签后传输继续，最终文件 238,286,634 字节、00:24:27.27、1920×1080、H.264/AAC；完整解码退出码 0、错误输出为空。扩展存储包含最终文件路径且队列恢复为空闲。详细记录见 [队列续接修复](QUEUE-CONTINUATION-1.5.1.md)。
