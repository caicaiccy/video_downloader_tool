const WAITING = "等待中";
const FAILED = "失败";
const CANCELLED = "已取消";
const ACTIVE_STATES = new Set(["下载中", "重试等待"]);

let jobs = [];
let queueStatus = "idle";
let queueMessage = "";
let activeProgress = "";
let scanTab = null;
let scannedMedia = [];
let pendingOrigins = [];
let scanning = false;
let directoryBusy = false;
const playingJobs = new Set();
const buttonFeedback = new Map();
const VIDEO_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="6" width="12" height="12" rx="2"/><path d="m15 10 6-3v10l-6-3Z"/></svg>';
const TRASH_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M5 6l1 14a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1l1-14M10 10v7m4-7v7"/></svg>';

const urlsInput = document.getElementById("urls");
const maxRetriesInput = document.getElementById("maxRetries");
const jobsList = document.getElementById("jobs");
const statusBox = document.getElementById("status");
const emptyQueue = document.getElementById("emptyQueue");

async function loadState() {
  const stored = await chrome.storage.local.get(["jobs", "queueStatus", "queueMessage", "downloadDirectory", "maxRetries"]);
  jobs = Array.isArray(stored.jobs) ? stored.jobs : [];
  queueStatus = stored.queueStatus || "idle";
  queueMessage = stored.queueMessage || "";
  showDirectory(stored.downloadDirectory);
  maxRetriesInput.value = String(Number.isInteger(stored.maxRetries) && stored.maxRetries >= 0 && stored.maxRetries <= 5 ? stored.maxRetries : 3);
  render();
}

function render() {
  jobsList.replaceChildren();
  document.getElementById("jobCount").textContent = `${jobs.length} 项`;
  emptyQueue.hidden = jobs.length > 0;
  for (const job of sortedJobs()) {
    const row = document.createElement("li");
    row.className = "job";
    const url = document.createElement("span");
    url.className = "job-url";
    url.textContent = job.title || job.url;
    url.title = job.media ? `${job.url}\n${job.media.kind}: ${job.media.url}` : job.url;
    const meta = document.createElement("span");
    meta.className = "job-meta";
    const state = document.createElement("span");
    state.className = "job-state";
    state.textContent = job.status || WAITING;
    meta.append(state);
    if (job.media) {
      const kind = document.createElement("span");
      kind.textContent = `${job.media.browser ? "浏览器" : "本机"} · ${job.media.kind.toUpperCase()}`;
      meta.append(kind);
    }
    if (job.error || job.outputPath) {
      const detail = document.createElement("span");
      detail.className = "job-detail";
      detail.textContent = job.error || job.outputPath;
      detail.title = detail.textContent;
      meta.append(detail);
    }
    const remove = document.createElement("button");
    remove.className = "icon-button remove-job";
    remove.innerHTML = TRASH_ICON;
    remove.ariaLabel = "移除任务";
    remove.title = "从队列移除";
    remove.disabled = queueStatus === "running" || ACTIVE_STATES.has(job.status);
    remove.addEventListener("click", () => removeJob(job.id));
    const left = document.createElement("div");
    left.className = "job-content";
    left.append(url, meta);
    const actions = document.createElement("div");
    actions.className = "job-actions";
    if (job.status === "完成" && job.outputPath) {
      const play = document.createElement("button");
      play.className = "icon-button play-job";
      play.innerHTML = VIDEO_ICON;
      play.ariaLabel = "播放视频";
      play.title = "使用默认播放器打开；Ctrl + 点击选择其他应用";
      play.disabled = playingJobs.has(job.id);
      play.addEventListener("click", event => playVideo(job.id, !!event.ctrlKey));
      actions.append(play);
    }
    actions.append(remove);
    row.append(left, actions);
    jobsList.append(row);
  }

  const running = queueStatus === "running";
  document.getElementById("startQueue").disabled = directoryBusy || running || !jobs.some(
    (job) => [WAITING, CANCELLED].includes(job.status || WAITING)
  );
  document.getElementById("cancelQueue").disabled = !running;
  document.getElementById("chooseDirectory").disabled = directoryBusy || running;
  document.getElementById("resetDirectory").disabled = directoryBusy || running;
  document.getElementById("openDirectory").disabled = directoryBusy;
  maxRetriesInput.disabled = running;
  document.getElementById("retryFailed").disabled = running || !jobs.some(
    (job) => job.status === FAILED
  );
  statusBox.textContent = activeProgress || queueMessage;
  statusBox.hidden = !statusBox.textContent;
  const needsPermission = jobs.some(job => job.status === FAILED && job.requiredOrigins?.length);
  document.getElementById("authorizeDownloads").hidden = !needsPermission;
  document.getElementById("authorizeDownloads").disabled = running;
}

async function persistJobs() {
  await chrome.storage.local.set({ jobs });
  render();
}

async function addLinks(values) {
  const incoming = values.map((value) => value.trim()).filter(Boolean);
  if (!incoming.length) {
    setMessage("先输入 HTTP(S) 视频或页面链接。", true);
    return;
  }
  const invalid = incoming.filter((url) => !MediaSupport.isHttpUrl(url));
  if (invalid.length) {
    setMessage("只接受不含账号密码的 HTTP(S) 链接；blob: 地址不能直接下载。", true);
    return;
  }
  const known = new Set(jobs.filter(job => !job.media).map((job) => job.url));
  let added = 0;
  for (const url of incoming) {
    if (known.has(url)) continue;
    jobs.push({ id: crypto.randomUUID(), url, status: WAITING, attempts: 0, error: "", createdAt: Date.now() });
    known.add(url);
    added += 1;
  }
  await persistJobs();
  urlsInput.value = "";
  resizeUrls();
  return added;
}

async function removeJob(id) {
  jobs = jobs.filter((job) => job.id !== id);
  await persistJobs();
}

async function startQueue(onlyIds = null) {
  const candidates = sortedJobs().filter((job) => {
    const eligible = [WAITING, CANCELLED].includes(job.status || WAITING);
    return eligible && (!onlyIds || onlyIds.has(job.id));
  });
  if (!candidates.length) {
    setQueueMessage("没有等待中的任务。", true);
    return;
  }
  const parsedRetries = Number(maxRetriesInput.value);
  if (!maxRetriesInput.value.trim() || !Number.isInteger(parsedRetries) || parsedRetries < 0 || parsedRetries > 5) {
    setQueueMessage("自动重试次数须为 0 到 5。", true);
    return;
  }
  const backgroundJobs = candidates.filter(job => !job.media && !MediaSupport.isBilibiliUrl(job.url));
  if (backgroundJobs.some(job => !MediaSupport.isPublicHttpUrl(job.url))) {
    setQueueMessage("后台下载只接受公开 HTTP(S) 网站链接。", true);
    return;
  }
  if (backgroundJobs.length) {
    const origins = [...new Set(backgroundJobs.flatMap(job => job.requiredOrigins || []))];
    if (origins.length) try {
      const granted = await chrome.permissions.request({ origins });
      if (!granted) { setQueueMessage("视频来源未授权，下载未启动。", true); return; }
    } catch (error) { setQueueMessage(error.message || "无法申请视频来源权限。", true); return; }
    for (const job of backgroundJobs) { job.transport = "background"; job.requiredOrigins = []; }
    await persistJobs();
  }
  activeProgress = "";
  queueStatus = "running";
  queueMessage = "正在连接本地下载组件…";
  await chrome.storage.local.set({ queueStatus, queueMessage });
  render();
  let response;
  try {
    response = await chrome.runtime.sendMessage({
      type: "start_queue",
      jobs: candidates.map((job) => ({ id: job.id, url: job.url, media: job.media, title: job.title, transport: job.transport })),
      maxRetries: parsedRetries,
    });
  } catch (error) {
    response = { ok: false, error: error?.message || "扩展后台连接失败，请重新加载扩展后重试。" };
  }
  if (!response || !response.ok) {
    if (response?.code === "queue_running") {
      queueStatus = "running";
      queueMessage = "已有任务正在下载；这些项目会在当前任务结束后自动继续。";
      await chrome.storage.local.set({ queueStatus, queueMessage });
      render();
      return;
    }
    queueStatus = "idle";
    queueMessage = response?.error || "无法连接本机 Native Host。请安装完整版下载器并注册当前扩展 ID。";
    const ids = new Set(candidates.map(job => job.id));
    for (const job of jobs) {
      if (ids.has(job.id) && [WAITING, CANCELLED].includes(job.status || WAITING)) {
        job.status = FAILED;
        job.error = queueMessage;
      }
    }
    await chrome.storage.local.set({ jobs, queueStatus, queueMessage });
    render();
  }
}

document.getElementById("useCurrent").addEventListener("click", async () => {
  const button = document.getElementById("useCurrent");
  button.disabled = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.url || !MediaSupport.isHttpUrl(tab.url)) {
      throw new Error("当前标签页不是可用的 HTTP(S) 页面。");
    }
    const added = await addLinks([tab.url]);
    showButtonFeedback("useCurrent", added ? "✓ 添加成功" : "已在队列中", added === 0);
  } catch (error) {
    setMessage(error.message || "添加失败，请重试。", true);
  } finally {
    button.disabled = false;
  }
});

async function scanCurrent(options = {}) {
  if (scanning) return;
  scanning = true;
  document.getElementById("scanMedia").disabled = true;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !MediaSupport.isHttpUrl(tab.url)) throw new Error("请在普通 HTTP(S) 视频页面扫描。");
    scanTab = { id: tab.id, url: tab.url };
    // activeTab covers the main frame; optional grants cover cross-origin frames.
    const top = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: MediaSupport.probePage });
    let results = top;
    try {
      results = await chrome.scripting.executeScript({ target: { tabId: tab.id, allFrames: true }, func: MediaSupport.probePage });
    } catch (_) { /* Keep the main-frame result when some frames are inaccessible. */ }
    if (!results.some(r => r.frameId === 0)) results = [...top, ...results];
    const unique = new Map();
    const origins = new Set();
    let protectedMedia = false;
    let hasBlob = false;
    for (const { result, frameId } of results) {
      if (!result) continue;
      protectedMedia ||= result.protectedMedia;
      hasBlob ||= result.hasBlob;
      for (const item of result.candidates || []) unique.set(item.url, { ...item,
        browser: { tabId: tab.id, frameId, frameUrl: result.pageUrl || item.referrer } });
      for (const origin of result.frameOrigins || []) origins.add(origin);
    }
    pendingOrigins = [];
    for (const origin of origins) {
      if (!await chrome.permissions.contains({ origins: [origin] })) pendingOrigins.push(origin);
    }
    scannedMedia = protectedMedia ? [] : [...unique.values()];
    const scanTitle = (top[0]?.result?.title || tab.title || "网页视频").slice(0, 200);
    scannedMedia = scannedMedia.map((media, i) => ({ ...media,
      title: scannedMedia.length === 1 ? scanTitle : `${scanTitle}（资源 ${i + 1}）` }));
    const select = document.getElementById("mediaChoice");
    select.replaceChildren();
    for (const [i, media] of scannedMedia.entries()) {
      const option = document.createElement("option");
      option.value = String(i);
      option.textContent = media.title;
      option.title = `${media.kind.toUpperCase()} · ${media.url}`;
      select.append(option);
    }
    document.getElementById("mediaSection").hidden = !scannedMedia.length;
    document.getElementById("mediaResults").hidden = !scannedMedia.length;
    document.getElementById("addMedia").disabled = !scannedMedia.length;
    document.getElementById("grantFrames").hidden = !pendingOrigins.length;
    document.getElementById("scanInfo").textContent = protectedMedia ? "检测到受保护媒体（EME），不提供下载。" :
      `发现 ${scannedMedia.length} 个候选资源；请确认属于目标视频。` +
      (hasBlob ? " blob: 仅是播放句柄，应选择底层 HTTP(S) 清单。" : "") +
      (pendingOrigins.length ? ` 跨站播放器待授权：${pendingOrigins.join("、")}` : "") +
      (!scannedMedia.length && !pendingOrigins.length ? " 可先播放再扫描；未公开地址、扩展名未知或受限资源可能无法识别。" : "");
  } catch (error) {
    document.getElementById("mediaSection").hidden = true;
    document.getElementById("grantFrames").hidden = true;
    if (!options.automatic) setMessage(error.message || "扫描失败。", true);
  } finally {
    scanning = false;
    document.getElementById("scanMedia").disabled = false;
  }
}

document.getElementById("scanMedia").addEventListener("click", () => scanCurrent());
document.getElementById("grantFrames").addEventListener("click", async () => {
  // Request only the displayed iframe origins, directly from this user gesture.
  try {
    const granted = await chrome.permissions.request({ origins: pendingOrigins });
    if (granted) await scanCurrent();
    else setMessage("播放器域名未授权；仍可添加页面链接交给本地解析。", true);
  } catch (error) { setMessage(error.message || "无法授权播放器。", true); }
});
document.getElementById("addMedia").addEventListener("click", async () => {
  const button = document.getElementById("addMedia");
  const media = scannedMedia[Number(document.getElementById("mediaChoice").value)];
  if (!scanTab || !media) return;
  button.disabled = true;
  try {
    const [current] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (current?.id !== scanTab.id || current?.url !== scanTab.url) {
      throw new Error("页面已变化，请重新扫描。");
    }
    const existing = jobs.find(job => job.media?.url === media.url);
    if (existing && (queueStatus === "running" || existing.status === "完成")) {
      showButtonFeedback("addMedia", "已在队列中", true); return;
    }
    const fields = { url: scanTab.url, title: media.title,
      media: { url: media.url, kind: media.kind, referrer: media.referrer, browser: media.browser },
      status: WAITING, attempts: 0, error: "", outputPath: "" };
    if (existing) Object.assign(existing, fields);
    else jobs.push({ id: crypto.randomUUID(), createdAt: Date.now(), ...fields });
    await persistJobs();
    showButtonFeedback("addMedia", existing ? "✓ 队列已更新" : "✓ 添加成功");
  } catch (error) { setMessage(error.message || "添加媒体失败，请重试。", true); }
  finally { button.disabled = !scannedMedia.length; }
});

document.getElementById("addLinks").addEventListener("click", async () => {
  try {
    const added = await addLinks(urlsInput.value.split(/\r?\n/));
    if (added !== undefined) showButtonFeedback("addLinks", added ? "✓ 添加成功" : "已在队列中", added === 0);
  } catch (error) { setMessage(error.message || "添加失败，请重试。", true); }
});

document.getElementById("startQueue").addEventListener("click", () => startQueue());

document.getElementById("cancelQueue").addEventListener("click", async () => {
  const response = await chrome.runtime.sendMessage({ type: "cancel_queue" });
  if (!response?.ok) setQueueMessage(response?.error || "无法取消队列。", true);
});

document.getElementById("retryFailed").addEventListener("click", async () => {
  const failed = jobs.filter((job) => job.status === FAILED);
  if (!failed.length) return;
  for (const job of failed) {
    job.status = WAITING;
    job.attempts = 0;
    job.error = "";
  }
  await persistJobs();
  await startQueue(new Set(failed.map((job) => job.id)));
});

document.getElementById("authorizeDownloads").addEventListener("click", async () => {
  const blocked = jobs.filter(job => job.status === FAILED && job.requiredOrigins?.length);
  const origins = [...new Set(blocked.flatMap(job => job.requiredOrigins))];
  if (!origins.length) return;
  try {
    const granted = await chrome.permissions.request({ origins });
    if (!granted) { setQueueMessage("视频来源未授权，可稍后继续。", true); return; }
    for (const job of blocked) { job.status = WAITING; job.error = ""; job.requiredOrigins = []; }
    await persistJobs();
    await startQueue(new Set(blocked.map(job => job.id)));
  } catch (error) { setQueueMessage(error.message || "无法授权视频来源。", true); }
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.jobs || changes.queueStatus || changes.queueMessage || changes.downloadDirectory || changes.maxRetries)) {
    loadState();
  }
});

chrome.runtime.onMessage.addListener((message) => {
  if (message?.type !== "host_event") return;
  const event = message.event || {};
  if (event.type === "progress") {
    const percent = Number(event.percent || 0).toFixed(1);
    activeProgress = event.total
      ? `下载中：${percent}%（${formatBytes(event.received)} / ${formatBytes(event.total)}）`
      : `已接收 ${formatBytes(event.received)}`;
    render();
  } else if (event.type === "log") {
    activeProgress = event.text || "";
    render();
  } else if (event.type === "status" || event.type === "error") {
    activeProgress = "";
    queueMessage = event.text || event.message || "";
    render();
  } else if (event.type === "progress_reset" || event.type === "queue_done") {
    activeProgress = "";
    loadState();
  }
});

function formatBytes(value) {
  let size = Number(value || 0);
  for (const unit of ["B", "KB", "MB", "GB"]) {
    if (size < 1024 || unit === "GB") return `${size.toFixed(1)} ${unit}`;
    size /= 1024;
  }
  return `${size.toFixed(1)} GB`;
}

function sortedJobs() {
  const priority = job => ACTIVE_STATES.has(job.status) ? 0 :
    [WAITING, CANCELLED].includes(job.status || WAITING) ? 1 : job.status === "完成" ? 3 : 2;
  return jobs.map((job, index) => ({ job, index })).sort((a, b) =>
    priority(a.job) - priority(b.job) ||
    Number(b.job.createdAt || 0) - Number(a.job.createdAt || 0) || b.index - a.index
  ).map(item => item.job);
}

function setMessage(message, isError = false) {
  const notice = document.getElementById("actionNotice");
  notice.textContent = isError ? message : "";
  notice.hidden = !isError;
}

function setQueueMessage(message, isError = false) {
  queueMessage = message;
  activeProgress = "";
  statusBox.textContent = message;
  statusBox.hidden = !message;
  statusBox.classList.toggle("error", isError);
  chrome.storage.local.set({ queueMessage: message });
}

function showButtonFeedback(id, message, duplicate = false) {
  const button = document.getElementById(id);
  const previous = buttonFeedback.get(id);
  if (previous) clearTimeout(previous.timer);
  const label = previous?.label || button.textContent;
  setMessage("");
  button.textContent = message;
  button.classList.toggle("button-feedback", true);
  button.classList.toggle("duplicate", duplicate);
  const timer = setTimeout(() => {
    button.textContent = label;
    button.classList.toggle("button-feedback", false);
    button.classList.toggle("duplicate", false);
    buttonFeedback.delete(id);
  }, 2500);
  buttonFeedback.set(id, { label, timer });
}

function showDirectory(path) {
  const input = document.getElementById("downloadDirectory");
  input.value = path || "下载";
  input.title = path || "系统下载目录";
}

async function directoryAction(action) {
  directoryBusy = true;
  render();
  try {
    const response = await chrome.runtime.sendMessage({ type: "desktop_action", action });
    if (!response?.ok) throw new Error(response?.error || "本地操作失败。");
    if (response.outputDirectory) showDirectory(response.outputDirectory);
  } catch (error) { setMessage(error.message || "无法操作下载目录。", true); }
  finally { directoryBusy = false; render(); }
}

async function playVideo(jobId, chooseApplication) {
  if (playingJobs.has(jobId)) return;
  playingJobs.add(jobId);
  render();
  try {
    const response = await chrome.runtime.sendMessage({ type: "desktop_action", action: "play_video", jobId, chooseApplication });
    if (!response?.ok) throw new Error(response?.error || "无法打开视频。");
  } catch (error) { setMessage(error.message || "无法打开视频。", true); }
  finally { playingJobs.delete(jobId); render(); }
}

document.getElementById("chooseDirectory").addEventListener("click", () => directoryAction("choose_directory"));
document.getElementById("openDirectory").addEventListener("click", () => directoryAction("open_directory"));
document.getElementById("resetDirectory").addEventListener("click", () => directoryAction("reset_directory"));

function showSettings(open) {
  document.getElementById("downloadPage").hidden = open;
  document.getElementById("settingsPage").hidden = !open;
  document.getElementById("brandMark").hidden = open;
  document.getElementById("backSettings").hidden = !open;
  document.getElementById("scanMedia").hidden = open;
  document.getElementById("openSettings").hidden = open;
  document.getElementById("openSettings").ariaExpanded = String(open);
  document.getElementById("pageTitle").textContent = open ? "设置" : "视频本地下载器";
  setMessage("");
  if (!open) resizeUrls();
}

function resizeUrls() {
  if (document.getElementById("downloadPage").hidden) return;
  const style = getComputedStyle(urlsInput);
  const lineHeight = Number.parseFloat(style.lineHeight);
  const padding = Number.parseFloat(style.paddingTop) + Number.parseFloat(style.paddingBottom);
  const border = Number.parseFloat(style.borderTopWidth) + Number.parseFloat(style.borderBottomWidth);
  const minimum = lineHeight + padding + border;
  const maximum = lineHeight * 5.5 + padding + border;
  urlsInput.style.height = "auto";
  const required = urlsInput.scrollHeight + border;
  urlsInput.style.height = `${Math.min(maximum, Math.max(minimum, required))}px`;
  urlsInput.style.overflowY = required > maximum ? "auto" : "hidden";
}

document.getElementById("openSettings").addEventListener("click", () => showSettings(true));
document.getElementById("backSettings").addEventListener("click", () => showSettings(false));
urlsInput.addEventListener("input", resizeUrls);
maxRetriesInput.addEventListener("change", async () => {
  const value = Number(maxRetriesInput.value);
  const valid = !!maxRetriesInput.value.trim() && Number.isInteger(value) && value >= 0 && value <= 5;
  maxRetriesInput.classList.toggle("invalid", !valid);
  if (!valid) { setMessage("失败重试次数须为 0 到 5。", true); return; }
  try {
    await chrome.storage.local.set({ maxRetries: value });
    setMessage("");
  } catch (error) { setMessage(error.message || "无法保存重试次数。", true); }
});

async function initialize() {
  await loadState();
  resizeUrls();
  // Reuse existing grants; opening the popup never prompts for new access.
  const scan = scanCurrent({ automatic: true });
  try {
    const response = await chrome.runtime.sendMessage({ type: "desktop_action", action: "get_directory" });
    if (response?.ok && response.outputDirectory) showDirectory(response.outputDirectory);
  } catch (_) { /* Directory actions show actionable Host errors when clicked. */ }
  await scan;
}
initialize();
