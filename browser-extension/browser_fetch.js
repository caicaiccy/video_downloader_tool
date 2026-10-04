/* Runs only in a user-selected, already-authorized player frame. */
async function fetchBrowserResource(requestId, url, method, range, expectedPage) {
  let seq = 0;
  const controller = new AbortController();
  let idleTimer;
  function resetTimeout() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => controller.abort(), 60000);
  }
  const onAbort = message => {
    if (message?.type === "browser_abort" && message.requestId === requestId) controller.abort();
  };
  chrome.runtime.onMessage.addListener(onAbort);
  async function send(fields) {
    const result = await chrome.runtime.sendMessage({ type: "browser_response", requestId, seq: seq++, ...fields });
    if (!result?.ok) throw new Error(result?.error || "本地传输已断开。");
    resetTimeout();
  }
  try {
    if (location.href !== expectedPage) throw new Error("原播放器页面已变化，请重新扫描。");
    if (!["GET", "HEAD"].includes(method) || !/^https?:\/\//i.test(url)) throw new Error("不支持此浏览器请求。");
    resetTimeout();
    // The browser uses the frame's normal Origin/Referer/UA and enforces CORS.
    // Do not copy credentials, forge headers or change proxy/network settings.
    const response = await fetch(url, { method, credentials: "omit", signal: controller.signal,
      headers: range ? { Range: range } : {} });
    const headers = {};
    for (const name of ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]) {
      const value = response.headers.get(name);
      if (value !== null) headers[name] = value;
    }
    // Fetch transparently decompresses bodies. The compressed wire length is
    // not an expected byte count for the resulting stream.
    if (response.headers.get("Content-Encoding")) delete headers["Content-Length"];
    await send({ phase: "headers", status: response.status, url: response.url, headers });
    if (method !== "HEAD" && response.body) {
      const reader = response.body.getReader();
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        resetTimeout();
        for (let offset = 0; offset < value.length; offset += 65536) {
          const bytes = value.subarray(offset, offset + 65536);
          let binary = "";
          for (let start = 0; start < bytes.length; start += 8192) {
            binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
          }
          await send({ phase: "data", data: btoa(binary) });
        }
      }
    }
    await send({ phase: "end", method });
  } catch (error) {
    try {
      await send({ phase: "error", error: `浏览器匿名请求失败：${error.message || error}。原页面需保持打开，资源必须允许匿名请求及 CORS。` });
    } catch (_) {}
  } finally {
    clearTimeout(idleTimer);
    chrome.runtime.onMessage.removeListener(onAbort);
  }
}
