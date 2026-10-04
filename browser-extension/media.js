/* Shared URL rules and a self-contained, read-only page probe. */
(function (root) {
  function isHttpUrl(value) {
    try {
      const u = new URL(value);
      return /^https?:$/.test(u.protocol) && !!u.hostname && !u.username && !u.password && value.length <= 8192;
    } catch (_) { return false; }
  }
  function isBilibiliUrl(value) {
    if (!isHttpUrl(value)) return false;
    const host = new URL(value).hostname.toLowerCase().replace(/\.$/, "");
    return host === "b23.tv" || host === "bilibili.com" || host.endsWith(".bilibili.com");
  }
  function isPublicHttpUrl(value) {
    if (!isHttpUrl(value) || /[\\\x00-\x1f]/.test(value)) return false;
    const host = new URL(value).hostname.toLowerCase().replace(/\.$/, "");
    if (host === "localhost" || /\.(?:localhost|local|internal)$/.test(host)) return false;
    if (host.startsWith("[")) {
      const prefix = parseInt(host.slice(1).split(":")[0], 16);
      return prefix >= 0x2000 && prefix < 0x4000;
    }
    if (/^\d+\.\d+\.\d+\.\d+$/.test(host)) {
      const [a,b] = host.split(".").map(Number);
      return a > 0 && a < 224 && ![10,127].includes(a) &&
        !(a === 169 && b === 254) && !(a === 172 && b >= 16 && b <= 31) &&
        !(a === 192 && [0,168].includes(b)) && !(a === 100 && b >= 64 && b <= 127) &&
        !(a === 198 && [18,19].includes(b));
    }
    return host.includes(".");
  }
  function mediaKind(value) {
    if (!isHttpUrl(value)) return null;
    const path = new URL(value).pathname.toLowerCase();
    if (/\.m3u8$/.test(path)) return "hls";
    if (/\.mpd$/.test(path)) return "dash";
    if (/\.(mp4|webm|mov|mkv|flv|m4v|ogv)$/.test(path)) return "direct";
    return null;
  }

  // No page globals, decryption, network fetches, cookies or player interception.
  // Keep dependencies inside this function: Chrome serializes it for injection.
  function probePage() {
    const candidates = new Map();
    const frames = new Set();
    let hasBlob = false;
    let protectedMedia = false;
    function add(value, source, direct = false) {
      try {
        const u = new URL(value, location.href);
        if (u.protocol === "blob:") { hasBlob = true; return; }
        if (!/^https?:$/.test(u.protocol) || u.username || u.password || u.href.length > 8192) return;
        const p = u.pathname.toLowerCase();
        const kind = /\.m3u8$/.test(p) ? "hls" : /\.mpd$/.test(p) ? "dash" :
          /\.(mp4|webm|mov|mkv|flv|m4v|ogv)$/.test(p) || direct ? "direct" : null;
        if (!kind || candidates.size >= 100) return;
        // A literal MP4 in configuration may be an unused ad/placeholder.
        // Direct media needs video/source, link or resource evidence; keep
        // literal manifests as a fallback for MSE players with blob handles.
        if (source === "script" && kind === "direct") return;
        if (!candidates.has(u.href)) candidates.set(u.href, {
          url: u.href, kind, source, referrer: location.href,
        });
      } catch (_) {}
    }
    document.querySelectorAll("video").forEach(video => {
      protectedMedia ||= !!video.mediaKeys;
      if (video.currentSrc || video.src) add(video.currentSrc || video.src, "video", true);
      video.querySelectorAll("source[src]").forEach(s => add(s.src, "source", true));
    });
    document.querySelectorAll("a[href]").forEach(a => add(a.href, "link"));
    performance.getEntriesByType("resource").forEach(r => add(r.name, "resource"));
    // Only literal public media URLs in inline scripts, never evaluate code.
    let scriptBudget = 1024 * 1024;
    document.querySelectorAll("script:not([src])").forEach(script => {
      const text = (script.textContent || "").slice(0, Math.max(0, scriptBudget));
      scriptBudget -= text.length;
      const literals = text.matchAll(/["']((?:https?:)?\/\/[^"'\s<>]+|\/[^"'\s<>]+\.(?:m3u8|mpd|mp4|webm)(?:\?[^"'\s<>]*)?)["']/g);
      for (const match of literals) add(match[1].replace(/\\\//g, "/"), "script");
    });
    document.querySelectorAll("iframe[src]").forEach(frame => {
      try {
        const u = new URL(frame.src, location.href);
        if (/^https?:$/.test(u.protocol) && u.origin !== location.origin) frames.add(`${u.origin}/*`);
      } catch (_) {}
    });
    return {
      candidates: Array.from(candidates.values()), frameOrigins: Array.from(frames),
      hasBlob, protectedMedia, title: document.title, pageUrl: location.href,
    };
  }
  root.MediaSupport = { isHttpUrl, isPublicHttpUrl, isBilibiliUrl, mediaKind, probePage };
})(globalThis);
