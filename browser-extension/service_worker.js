importScripts("media.js");
importScripts("browser_fetch.js");
importScripts("background_fetch.js");
const HOST_NAME = "com.codex.bili_downloader";
const HOST_READY_TIMEOUT_MS = 30000;
let hostCapabilities = [];

let nativePort = null;
let nativeReady = null;
let queueRunning = false;
let queueStarting = false;
let hostEvents = Promise.resolve();
const browserJobs = new Map();
const browserRequests = new Map();
const desktopRequests = new Map();

function rejectDesktopRequests(error) {
  for (const request of desktopRequests.values()) {
    clearTimeout(request.timer);
    request.resolve({ ok: false, error });
  }
  desktopRequests.clear();
}

async function desktopAction(message) {
  const allowed = ["get_directory", "choose_directory", "reset_directory", "open_directory", "play_video"];
  if (!allowed.includes(message.action)) return { ok: false, error: "不支持的文件操作。" };
  if (["choose_directory", "reset_directory"].includes(message.action) && (queueRunning || queueStarting)) {
    return { ok: false, error: "请在队列结束后更改下载目录。" };
  }
  try {
    let path = "";
    if (message.action === "play_video") {
      const stored = await chrome.storage.local.get("jobs");
      const job = (stored.jobs || []).find(item => item.id === message.jobId);
      if (!job || job.status !== "完成" || !job.outputPath) throw new Error("请先完成视频下载。");
      path = job.outputPath;
    }
    const port = await connectHost();
    if (!hostCapabilities.includes("desktop_actions")) throw new Error("请更新本地下载组件以使用目录设置与播放功能。");
    const requestId = crypto.randomUUID();
    return await new Promise(resolve => {
      const timer = setTimeout(() => {
        desktopRequests.delete(requestId);
        resolve({ ok: false, error: "系统文件操作超时，请重试。" });
      }, 610000);
      desktopRequests.set(requestId, { resolve, timer, action: message.action });
      try { port.postMessage({ type: "desktop_action", requestId, action: message.action, path, chooseApplication: message.chooseApplication === true }); }
      catch (error) {
        clearTimeout(timer);
        desktopRequests.delete(requestId);
        resolve({ ok: false, error: error.message || "本地连接已断开。" });
      }
    });
  } catch (error) { return { ok: false, error: error.message || "无法执行本地操作。" }; }
}

async function openBrowserResource(message) {
  const job = browserJobs.get(String(message.jobId));
  if (job?.transport === "background") {
    const state = { mode: "background", controller: new AbortController(), ack: null, nextSeq: 0 };
    browserRequests.set(message.requestId, state);
    try {
      await fetchBackgroundResource(message, state, value => nativePort.postMessage(value),
        url => chrome.permissions.contains({ origins: [urlOrigin(url)] }));
    } catch (error) {
      if (error.origin) await recordRequiredOrigin(job.id, error.origin).catch(() => {});
      try { nativePort?.postMessage({ type: "browser_response", requestId: message.requestId,
        seq: state.nextSeq || 0, phase: "error", error: error.message || "后台读取失败。" }); } catch (_) {}
    } finally {
      browserRequests.delete(message.requestId);
    }
    return;
  }
  const context = job?.media?.browser;
  if (!context || !MediaSupport.isHttpUrl(message.url) || !["GET", "HEAD"].includes(message.method)) {
    nativePort?.postMessage({ type: "browser_response", requestId: message.requestId, seq: 0,
      phase: "error", error: "缺少当前播放器上下文，请重新扫描媒体。" });
    return;
  }
  const state = { context, ack: null };
  browserRequests.set(message.requestId, state);
  try {
    const tab = await chrome.tabs.get(context.tabId);
    if (tab.url !== job.url) throw new Error("原视频页面已变化，请重新扫描媒体。");
    await chrome.scripting.executeScript({ target: { tabId: context.tabId, frameIds: [context.frameId] },
      func: fetchBrowserResource, args: [message.requestId, message.url, message.method, message.range || "", context.frameUrl] });
  } catch (error) {
    try { nativePort?.postMessage({ type: "browser_response", requestId: message.requestId, seq: 0,
      phase: "error", error: error.message || "无法读取原播放器页面，请重新扫描。" }); } catch (_) {}
  } finally {
    if (state.ack) { clearTimeout(state.ack.timer); state.ack.reply({ ok: false, error: "浏览器传输已结束。" }); }
    browserRequests.delete(message.requestId);
  }
}

function abortBrowserResources() {
  for (const [requestId, state] of browserRequests) {
    if (state.mode === "background") { state.controller?.abort(); continue; }
    chrome.tabs.sendMessage(state.context.tabId, { type: "browser_abort", requestId }, { frameId: state.context.frameId }).catch(() => {});
    if (state.ack) { clearTimeout(state.ack.timer); state.ack.reply({ ok: false, error: "本地下载已停止。" }); state.ack = null; }
  }
  browserRequests.clear();
  browserJobs.clear();
}

function urlOrigin(url) {
  const u = new URL(url);
  return `${u.protocol}//${u.hostname}/*`;
}

async function recordRequiredOrigin(jobId, origin) {
  hostEvents = hostEvents.then(async () => {
    const stored = await chrome.storage.local.get("jobs");
    const jobs = stored.jobs || [];
    const job = jobs.find(item => item.id === jobId);
    if (job) {
      job.requiredOrigins = [...new Set([...(job.requiredOrigins || []), origin])];
      await chrome.storage.local.set({ jobs });
    }
  });
  await hostEvents;
}

function connectHost() {
  if (nativePort && nativeReady) {
    return nativeReady.then(() => nativePort);
  }

  let port;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (error) {
    return Promise.reject(new Error(describeHostError(error?.message)));
  }

  nativePort = port;
  let settleReady;
  let rejectReady;
  let readySettled = false;
  const ready = new Promise((resolve, reject) => {
    settleReady = resolve;
    rejectReady = reject;
  });
  nativeReady = ready;

  const readyTimer = setTimeout(() => {
    if (readySettled) return;
    readySettled = true;
    nativePort = null;
    nativeReady = null;
    rejectReady(new Error("本机 Native Host 未响应。请确认已安装，并已将当前扩展 ID 注册到 Chrome。"));
    try { port.disconnect(); } catch (_) {}
  }, HOST_READY_TIMEOUT_MS);

  port.onMessage.addListener((message) => {
    if (message?.type === "browser_request") { openBrowserResource(message); return; }
    if (message?.type === "browser_ack") {
      const state = browserRequests.get(message.requestId);
      if (state?.ack?.seq === message.seq) {
        clearTimeout(state.ack.timer);
        if (state.mode === "background") {
          if (message.ok) state.ack.resolve();
          else state.ack.reject(new Error(message.error || "本地传输已停止。"));
        } else state.ack.reply({ ok: !!message.ok, error: message.error });
        state.ack = null;
      }
      return;
    }
    if (message?.type === "browser_abort") {
      const state = browserRequests.get(message.requestId);
      if (state?.mode === "background") state.controller?.abort();
      else if (state) chrome.tabs.sendMessage(state.context.tabId, message, { frameId: state.context.frameId }).catch(() => {});
      return;
    }
    if (message?.type === "host_ready") {
      hostCapabilities = Array.isArray(message.capabilities) ? message.capabilities : [];
      if (!readySettled) {
        readySettled = true;
        clearTimeout(readyTimer);
        settleReady();
      }
      return;
    }
    if (message?.type === "desktop_result") {
      const request = desktopRequests.get(message.requestId);
      if (!request) return;
      clearTimeout(request.timer);
      desktopRequests.delete(message.requestId);
      hostEvents = hostEvents.then(async () => {
        const update = {};
        if (message.ok && typeof message.outputDirectory === "string") update.downloadDirectory = message.outputDirectory;
        if (Object.keys(update).length) await chrome.storage.local.set(update);
      }).catch(() => {}).finally(() => request.resolve(message));
      return;
    }
    hostEvents = hostEvents.then(() => handleHostMessage(message)).catch(() => {});
  });
  port.onDisconnect.addListener(() => {
    clearTimeout(readyTimer);
    const error = describeHostError(chrome.runtime.lastError?.message);
    if (nativePort === port) {
      nativePort = null;
      nativeReady = null;
    }
    if (!readySettled) {
      readySettled = true;
      rejectReady(new Error(error));
    }
    const wasRunning = queueRunning || queueStarting;
    queueRunning = false;
    rejectDesktopRequests(error);
    abortBrowserResources();
    hostEvents = hostEvents.then(() => handleHostDisconnect(error, wasRunning)).catch(() => {});
  });
  return ready.then(() => {
    if (nativePort !== port) {
      throw new Error("本机 Native Host 在就绪后断开，请检查安装和扩展 ID 授权。");
    }
    return port;
  });
}

function describeHostError(rawError) {
  const error = String(rawError || "本机 Native Host 已断开。");
  const normalized = error.toLowerCase();
  if (normalized.includes("specified native messaging host not found")) {
    return "Chrome 找不到本机 Native Host。请安装完整版下载器，并运行其中的 Native Host 安装脚本；仅安装扩展还不能下载。";
  }
  if (normalized.includes("forbidden")) {
    return "Chrome 拒绝连接本机 Native Host：当前扩展 ID 未授权。请用 Chrome 扩展详情页显示的 ID 重新运行安装脚本。";
  }
  if (normalized.includes("native host has exited")) {
    return "本机 Native Host 启动后退出。请重新安装完整版下载器中的 Native Host，再试一次。";
  }
  return `本机 Native Host 连接失败：${error}。请确认已安装 Host，且安装时使用了当前扩展 ID。`;
}

async function startQueue(message) {
  if (queueRunning || queueStarting) return { ok: false, code: "queue_running", error: "下载队列正在运行。" };
  if ([...desktopRequests.values()].some(request => ["choose_directory", "reset_directory"].includes(request.action))) {
    return { ok: false, error: "请先完成下载目录设置。" };
  }
  const jobs = Array.isArray(message.jobs) ? message.jobs : [];
  if (!jobs.length) return { ok: false, error: "没有等待中的任务。" };

  queueStarting = true;
  try {
    const port = await connectHost();
    if (nativePort !== port) {
      throw new Error("本机 Native Host 已断开，请确认安装后重试。");
    }
    if (jobs.some(job => job.media || !MediaSupport.isBilibiliUrl(job.url)) &&
        !hostCapabilities.includes("public_web_media")) {
      throw new Error("本机 Native Host 版本过旧。请安装 1.1.0 或更新版本的完整包，并重新运行 Host 安装脚本。");
    }
    if (jobs.some(job => job.media?.browser) && !hostCapabilities.includes("browser_http")) {
      throw new Error("浏览器媒体传输需要 Native Host 1.2.0，请更新 Host 后重试。");
    }
    if (jobs.some(job => !job.media && !MediaSupport.isBilibiliUrl(job.url)) && !hostCapabilities.includes("page_scan")) {
      throw new Error("网页链接自动解析需要 Native Host 1.4.0，请更新 Host 后重试。");
    }
    if (jobs.some(job => job.transport === "background") && !hostCapabilities.includes("background_http")) {
      throw new Error("后台网页下载需要 Native Host 1.5.0，请更新 Host 后重试。");
    }
    browserJobs.clear();
    for (const job of jobs) if (job.media?.browser || job.transport === "background") browserJobs.set(job.id, job);
    queueRunning = true;
    const persistRunning = chrome.storage.local.set({
      queueStatus: "running",
      queueMessage: "已连接本地下载组件，正在启动队列…",
    });
    port.postMessage({
      type: "start_queue",
      jobs,
      maxRetries: Math.max(0, Math.min(5, Number(message.maxRetries) || 0)),
    });
    persistRunning.catch(() => {});
    return { ok: true };
  } catch (error) {
    queueRunning = false;
    return { ok: false, error: error?.message || "无法连接本地下载组件。" };
  } finally {
    queueStarting = false;
  }
}

async function continueWaitingQueue() {
  if (queueRunning || queueStarting) return;
  const stored = await chrome.storage.local.get(["jobs", "maxRetries"]);
  const jobs = Array.isArray(stored.jobs) ? stored.jobs : [];
  const waiting = jobs.filter(job => (job.status || "等待中") === "等待中" && !(job.requiredOrigins?.length));
  if (!waiting.length) return;

  for (const job of waiting) {
    if (!job.media && !MediaSupport.isBilibiliUrl(job.url)) {
      if (!MediaSupport.isPublicHttpUrl(job.url)) continue;
      job.transport = "background";
    }
  }
  const runnable = waiting.filter(job => job.media || MediaSupport.isBilibiliUrl(job.url) || job.transport === "background");
  if (!runnable.length) return;
  await chrome.storage.local.set({ jobs });
  const response = await startQueue({
    jobs: runnable.map(job => ({ id: job.id, url: job.url, media: job.media,
      title: job.title, transport: job.transport })),
    maxRetries: Number.isInteger(stored.maxRetries) ? stored.maxRetries : 3,
  });
  if (!response.ok && response.code !== "queue_running") {
    const latest = await chrome.storage.local.get("jobs");
    const current = Array.isArray(latest.jobs) ? latest.jobs : [];
    const ids = new Set(runnable.map(job => job.id));
    for (const job of current) {
      if (ids.has(job.id) && (job.status || "等待中") === "等待中") {
        job.status = "失败";
        job.error = response.error || "无法启动后续下载任务。";
      }
    }
    await chrome.storage.local.set({ jobs: current, queueStatus: "idle",
      queueMessage: response.error || "无法启动后续下载任务。" });
  }
}

async function handleHostMessage(message) {
  if (!message || typeof message !== "object") return;
  const update = {};
  if (message.type === "queue_started") {
    queueRunning = true;
    update.queueStatus = "running";
    update.queueMessage = `队列已启动，共 ${message.count} 项。`;
  } else if (message.type === "queue_done") {
    queueRunning = false;
    abortBrowserResources();
    update.queueStatus = "idle";
    update.queueMessage = message.cancelled ? "队列已取消。" : "队列处理结束。";
  } else if (message.type === "status") {
    update.queueMessage = message.text || "";
  } else if (message.type === "error") {
    queueRunning = false;
    update.queueStatus = "idle";
    update.queueMessage = message.message || "本地下载组件发生错误。";
  }

  if (["job_state", "job_success", "job_metadata"].includes(message.type)) {
    const stored = await chrome.storage.local.get("jobs");
    const jobs = Array.isArray(stored.jobs) ? stored.jobs : [];
    const job = jobs.find((item) => item.id === String(message.id));
    if (job && message.type === "job_state") {
      job.status = message.status;
      job.attempts = message.attempts || job.attempts || 0;
      job.error = message.error || "";
    } else if (job && message.type === "job_success") {
      job.outputPath = message.path || "";
      job.status = "完成";
    } else if (job && message.type === "job_metadata" && typeof message.title === "string") {
      job.title = message.title.slice(0, 200);
    }
    update.jobs = jobs;
  }

  if (Object.keys(update).length) await chrome.storage.local.set(update);
  chrome.runtime.sendMessage({ type: "host_event", event: message }).catch(() => {});
  if (message.type === "queue_done" && !message.cancelled) {
    // Native Host processes a snapshot. Pick up links added while that
    // snapshot was running so they cannot remain waiting indefinitely.
    setTimeout(() => continueWaitingQueue().catch(async error => {
      await chrome.storage.local.set({ queueStatus: "idle",
        queueMessage: error?.message || "无法继续等待中的下载任务。" });
    }), 0);
  }
}

async function handleHostDisconnect(error, wasRunning) {
  const stored = await chrome.storage.local.get("jobs");
  const jobs = Array.isArray(stored.jobs) ? stored.jobs : [];
  let changed = false;
  if (wasRunning) {
    for (const job of jobs) {
      if (["下载中", "重试等待"].includes(job.status)) {
        job.status = "失败";
        job.error = error;
        changed = true;
      }
    }
  }
  await chrome.storage.local.set({
    ...(changed ? { jobs } : {}),
    queueStatus: "idle",
    queueMessage: wasRunning ? `本地下载组件断开：${error}` : "",
  });
  chrome.runtime.sendMessage({
    type: "host_event",
    event: { type: "error", message: error },
  }).catch(() => {});
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "desktop_action") {
    // Content scripts cannot launch local files; only our popup can request it.
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) {
      sendResponse({ ok: false, error: "本地操作来源无效。" }); return false;
    }
    desktopAction(message).then(sendResponse);
    return true;
  }
  if (message?.type === "browser_response") {
    const state = browserRequests.get(message.requestId);
    if (!state || state.mode === "background" || !nativePort || sender.tab?.id !== state.context.tabId || sender.frameId !== state.context.frameId ||
        sender.url !== state.context.frameUrl || state.ack) {
      sendResponse({ ok: false, error: "浏览器传输来源无效或任务已结束。" }); return false;
    }
    const timer = setTimeout(() => {
      if (state.ack) { state.ack.reply({ ok: false, error: "本地传输响应超时。" }); state.ack = null; }
    }, 30000);
    state.ack = { seq: message.seq, reply: sendResponse, timer };
    try { nativePort.postMessage(message); }
    catch (error) { clearTimeout(timer); state.ack = null; sendResponse({ ok: false, error: error.message }); }
    return true;
  }
  if (message?.type === "start_queue") {
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) {
      sendResponse({ ok: false, error: "下载请求来源无效。" }); return false;
    }
    startQueue(message).then(sendResponse);
    return true;
  }
  if (message?.type === "cancel_queue") {
    if (!nativePort || !queueRunning) {
      sendResponse({ ok: false, error: "当前没有运行中的队列。" });
      return false;
    }
    try {
      nativePort.postMessage({ type: "cancel_queue" });
      abortBrowserResources();
      sendResponse({ ok: true });
    } catch (error) {
      sendResponse({ ok: false, error: error?.message || "无法取消队列。" });
    }
    return false;
  }
  return false;
});
