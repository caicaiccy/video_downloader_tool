/* Anonymous HTTP transfer from the extension worker; no website tab is needed. */
async function fetchBackgroundResource(message, state, postMessage, checkPermission) {
  const MAX_CHUNK_BYTES = 64 * 1024;
  const MAX_RESOURCE_BYTES = 2 * 1024 * 1024 * 1024;
  const controller = state.controller;
  state.nextSeq = 0;
  let received = 0;
  let idleTimer;
  let reader;
  let bodyDone = false;

  function cancelled() {
    return controller.signal.reason || new Error("后台网页请求已取消。");
  }
  function ensureActive() {
    if (controller.signal.aborted) throw cancelled();
  }
  function resetTimeout() {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => controller.abort(new Error("后台网页资源读取超时，请重试。")), 60000);
  }
  function requirePublicUrl(url) {
    if (!MediaSupport.isPublicHttpUrl(url)) throw new Error("后台传输只接受公开 HTTP(S) 地址。");
    ensureActive();
  }
  function onAbort() {
    if (state.ack) state.ack.reject(cancelled());
  }
  async function send(fields) {
    ensureActive();
    const currentSeq = state.nextSeq++;
    await new Promise((resolve, reject) => {
      let settled = false;
      const ack = {
        seq: currentSeq,
        resolve(value) {
          if (settled) return;
          settled = true;
          clearTimeout(ack.timer);
          if (state.ack === ack) state.ack = null;
          resolve(value);
        },
        reject(error) {
          if (settled) return;
          settled = true;
          clearTimeout(ack.timer);
          if (state.ack === ack) state.ack = null;
          reject(error);
        },
        timer: null,
      };
      ack.timer = setTimeout(() => ack.reject(new Error("本地传输响应超时。")), 30000);
      // Register before sending so an immediate native ACK cannot be lost.
      state.ack = ack;
      try {
        postMessage({ type: "browser_response", requestId: message.requestId, seq: currentSeq, ...fields });
      } catch (error) {
        ack.reject(error);
      }
    });
    ensureActive();
    resetTimeout();
  }

  controller.signal.addEventListener("abort", onAbort);
  try {
    ensureActive();
    if (!["GET", "HEAD"].includes(message.method)) throw new Error("后台传输仅支持 GET/HEAD。");
    const range = message.range ?? "";
    if (typeof range !== "string" || range.length > 4096 ||
        range && !/^bytes=(?:\d+-\d*|-\d+)$/.test(range)) {
      throw new Error("媒体 Range 请求无效。");
    }
    if (range) {
      const match = /^bytes=(\d+)-(\d+)$/.exec(range);
      if (match && BigInt(match[1]) > BigInt(match[2])) throw new Error("媒体 Range 请求无效。");
      if (/^bytes=-0+$/.test(range)) throw new Error("媒体 Range 请求无效。");
    }
    resetTimeout();
    requirePublicUrl(message.url);
    let response;
    try {
      // Ordinary CORS may allow anonymous reads without a host permission.
      response = await fetch(message.url, {
        method: message.method,
        credentials: "omit",
        signal: controller.signal,
        headers: range ? { Range: range } : {},
      });
    } catch (error) {
      ensureActive();
      if (error?.name === "TypeError") {
        const granted = await checkPermission(message.url);
        ensureActive();
        if (!granted) {
          const parsed = new URL(message.url);
          const permissionError = new Error("浏览器未能读取资源，可授权此媒体域名后重试。");
          permissionError.origin = `${parsed.protocol}//${parsed.hostname}/*`;
          throw permissionError;
        }
      }
      throw error;
    }
    if (message.method !== "HEAD" && response.body) reader = response.body.getReader();
    resetTimeout();
    requirePublicUrl(response.url);
    if (!Number.isInteger(response.status) || response.status < 200 || response.status > 599) {
      throw new Error("后台网页响应状态无效。");
    }
    const headers = {};
    for (const name of ["Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"]) {
      const value = response.headers.get(name);
      if (value !== null) headers[name] = value;
    }
    if (response.headers.get("Content-Encoding")) delete headers["Content-Length"];
    await send({ phase: "headers", status: response.status, url: response.url, headers });
    if (reader) {
      while (true) {
        ensureActive();
        const { value, done } = await reader.read();
        ensureActive();
        if (done) { bodyDone = true; break; }
        resetTimeout();
        received += value.byteLength;
        if (received > MAX_RESOURCE_BYTES) throw new Error("浏览器资源超过 2 GiB 大小限制。");
        for (let offset = 0; offset < value.byteLength; offset += MAX_CHUNK_BYTES) {
          const bytes = value.subarray(offset, offset + MAX_CHUNK_BYTES);
          let binary = "";
          for (let start = 0; start < bytes.length; start += 8192) {
            binary += String.fromCharCode(...bytes.subarray(start, start + 8192));
          }
          await send({ phase: "data", data: btoa(binary) });
        }
      }
    }
    await send({ phase: "end", method: message.method });
  } finally {
    clearTimeout(idleTimer);
    controller.signal.removeEventListener("abort", onAbort);
    if (state.ack) state.ack.reject(new Error("后台资源传输已结束。"));
    if (reader) {
      if (!bodyDone) {
        try { await reader.cancel(); } catch (_) {}
      }
      reader.releaseLock();
    }
  }
}
