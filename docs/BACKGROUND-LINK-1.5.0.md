# 网页链接后台下载 1.5.0

1.4.0 只增加网页资源解析，媒体仍由 Python HTTP 下载，未解决给定 Agedm 链接的 403。本版将静态 HTML / iframe 解析与媒体传输分开：Host 读取网页，扩展后台正常匿名 fetch 读取清单、分段或直链，Host 校验及合并。没有创建或导航网站页签，不依赖播放器帧。不导出 Cookie、伪造 UA/Origin/Referer 或更换代理。

Chrome 支持扩展后台网络请求；未授权域名仍可通过服务端正常允许的 CORS 读取，只有 fetch 失败且域名未授权才提供指定域名授权及重试。参见 [Chrome 网络请求文档](https://developer.chrome.com/docs/extensions/develop/concepts/network-requests) 和 [可选权限文档](https://developer.chrome.com/docs/extensions/reference/api/permissions)。HTTP 401/403 返回原始状态和正文交给 Host 判断，不能用授权请求掩盖服务端拒绝。

新增 background_http 能力与 transport=background 任务字段。后台直接使用 Native Messaging 的分块/ACK 协议，不接受内容脚本代传后台响应；启动下载仅接受自身弹窗来源。64 KiB 分块、2 GiB 单资源上限、ACK 背压、超时及取消清理均沿用明确边界。未加密/非直播检查不变。

## 验证

- 27 项 Python 测试通过；新增 URL → HTML → iframe → HLS 的后台媒体转发完整下载、合并与解码，同时检查媒体不回退到 Native HTTP。
- 扩展集成测试通过，无 tabs / scripting mock 的后台请求也能完成分块传输；拒绝非弹窗发起下载。独立后台模块测试覆盖 CORS 允许但未授权、HEAD、Range、重定向、权限提示、401/403 原样传递、取消、背压、大小及超时清理。
- 用户在下载器自己的测试页执行普通 fetch，未打开 Agedm 网站，报告 ffzyread 主 HLS 与子 HLS 都为 HTTP 200，长度分别 97 和 33,287 字节，host permission=false。说明这些清单无需额外授权即可从浏览器读取；不等同于完整视频下载成功。
- 已编译并更新已注册 Native Host 1.5.0，启动握手声明 background_http。保留现有 Chrome 扩展 ID 和权限。

## Agedm 完整视频验收通过

用户在下载器自身的临时验证页点击“验证完整视频下载”，输入原始网页链接 `https://www.agedm.io/play/20230189/2/1`。Host 自动读取 HTML / iframe，解析出单集名称及 HLS；生产后台模块 `background_fetch.js` 使用普通匿名 fetch 与 Native Messaging 分块/ACK 传输。此路径没有使用网站标签或播放器帧，也不依赖它们保持打开。

- 最终文件：`~/Downloads/捡走被人悔婚的千金，教会她坏坏的幸福生活 第01集 - 在线播放 - AGE动漫 [047352ebda60].mp4`。
- 大小 211,452,711 字节；时长 00:23:59.76；1920×1080；H.264 视频、AAC 音频。
- FFmpeg 对完整音视频解码，退出码 0，错误输出为空；耗时 17.70 秒。保留输入时间基准，避免 null 输出量化时间戳造成伪 DTS 警告。
- SHA-256：`1e1d4c818a81a549d01946b8a1b8da38f1062850abbb21c40bd3b14102be3154`，与 1.2.0 从原播放器页面下载的完整文件逐字节一致。
- 机器可读记录：[agedm-background-download-validation.json](agedm-background-download-validation.json)。视频保存在用户下载目录，不随安装包分发。

本次真实 Chrome 验证覆盖网页自动解析、生产后台读取模块、已安装 Host 的全部下载/合并流程。正式弹窗与 service worker 的队列及消息集成由自动化测试覆盖；用户需重新加载扩展后使用新版弹窗。临时验证页在验收后移出生产扩展，所有发布包均重新生成。此次验收针对 macOS arm64、Chrome 和给定线路，不推断其他站点或环境。
