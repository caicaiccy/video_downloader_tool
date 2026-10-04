/* Worker HTTP transfer tests. No Chrome tabs, frames or content scripts exist. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../browser-extension/background_fetch.js'), 'utf8');
const baseMessage = { requestId: 'fixture-request', url: 'https://cdn.example.com/video.mp4', method: 'GET', range: '' };

function publicUrl(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password &&
      url.hostname.includes('.') && !['127.0.0.1', '10.0.0.1'].includes(url.hostname);
  } catch (_) { return false; }
}

function responseFor(url, body = new Uint8Array(0), headers = {}, status = 200) {
  const response = new Response(body, { status, headers });
  Object.defineProperty(response, 'url', { value: url });
  return response;
}

function fixture(fetchImpl, options = {}) {
  const timers = new Map();
  const sent = [];
  const calls = [];
  let activeSends = 0;
  const state = { mode: 'background', controller: new AbortController(), ack: null };
  const setTrackedTimeout = (fn, ms) => {
    let timer;
    timer = setTimeout(() => { timers.delete(timer); fn(); }, ms);
    timers.set(timer, { fn, ms });
    return timer;
  };
  const clearTrackedTimeout = timer => { timers.delete(timer); clearTimeout(timer); };
  const context = vm.createContext({
    URL, AbortController,
    MediaSupport: { isPublicHttpUrl: publicUrl },
    setTimeout: setTrackedTimeout, clearTimeout: clearTrackedTimeout,
    btoa: value => Buffer.from(value, 'binary').toString('base64'),
    fetch: async (url, args) => { calls.push({ url, args }); return fetchImpl(url, args); },
  });
  vm.runInContext(source, context);
  const postMessage = message => {
    assert.equal(activeSends++, 0, 'Only one native message may await ACK');
    assert.equal(state.ack?.seq, message.seq, 'ACK listener must exist before native message delivery');
    sent.push(message);
    if (options.stallAck) return;
    const acknowledge = () => {
      activeSends--;
      const ack = state.ack;
      clearTrackedTimeout(ack.timer);
      state.ack = null;
      if (options.rejectAck) ack.reject(new Error('Native Host rejected chunk'));
      else ack.resolve();
    };
    if (options.immediateAck) acknowledge();
    else setTimeout(acknowledge, 1);
  };
  const permissionChecks = [];
  const checkPermission = async url => {
    permissionChecks.push(url);
    return options.permission ? options.permission(url) : true;
  };
  const run = message => context.fetchBackgroundResource({ ...baseMessage, ...message }, state, postMessage, checkPermission);
  return { state, run, sent, calls, timers, permissionChecks };
}

async function streamingAndBackpressure() {
  const bytes = new Uint8Array(180123);
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251;
  const test = fixture(async url => responseFor(url, bytes, {
    'Content-Type': 'video/mp4', 'Content-Length': String(bytes.length),
    'Content-Range': `bytes 0-${bytes.length - 1}/${bytes.length}`, 'Accept-Ranges': 'bytes',
    'X-Private-Header': 'not-forwarded',
  }));
  await test.run({ range: 'bytes=0-' });
  assert.equal(test.calls.length, 1);
  assert.equal(test.calls[0].args.credentials, 'omit');
  assert.equal(test.calls[0].args.method, 'GET');
  assert.deepEqual(Object.keys(test.calls[0].args.headers), ['Range']);
  assert.equal(test.calls[0].args.headers.Range, 'bytes=0-');
  assert.equal(test.calls[0].args.signal, test.state.controller.signal);
  assert.equal(test.sent[0].phase, 'headers');
  assert.equal(test.sent[0].headers['Content-Type'], 'video/mp4');
  assert.equal(test.sent[0].headers['Content-Length'], String(bytes.length));
  assert.equal(test.sent[0].headers['X-Private-Header'], undefined);
  const parts = test.sent.filter(message => message.phase === 'data').map(message => Buffer.from(message.data, 'base64'));
  assert(parts.every(part => part.length <= 65536));
  assert.deepEqual(Buffer.concat(parts), Buffer.from(bytes));
  assert.equal(test.sent.at(-1).phase, 'end');
  test.sent.forEach((message, index) => {
    assert.equal(message.seq, index);
    assert.equal(message.type, 'browser_response');
    assert.equal(message.requestId, baseMessage.requestId);
  });
  assert.equal(test.state.nextSeq, test.sent.length);
  assert.equal(test.state.ack, null);
  assert.equal(test.timers.size, 0);
  assert.equal(test.permissionChecks.length, 0);
}

async function headsAndCompression() {
  const head = fixture(async url => responseFor(url, new Uint8Array(10), { 'Content-Type': 'video/mp4' }), { immediateAck: true });
  await head.run({ method: 'HEAD' });
  assert.deepEqual(head.sent.map(message => message.phase), ['headers', 'end']);
  assert.equal(head.calls[0].args.method, 'HEAD');
  assert.deepEqual(Object.keys(head.calls[0].args.headers), []);
  assert.equal(head.timers.size, 0);
  const compressed = fixture(async url => responseFor(url, new Uint8Array(15), { 'Content-Encoding': 'gzip', 'Content-Length': '8' }));
  await compressed.run();
  assert.equal(compressed.sent[0].headers['Content-Length'], undefined);
  assert.equal(compressed.timers.size, 0);
}

async function permissionsAndUrls() {
  const bytes = new Uint8Array(145123).fill(9);
  const cors = fixture(async url => responseFor(url, bytes), { permission: () => false });
  await cors.run({ url: 'https://player.example.com:8443/watch' });
  assert.equal(cors.calls.length, 1);
  assert.equal(cors.sent[0].phase, 'headers');
  assert.equal(cors.sent.at(-1).phase, 'end');
  assert.deepEqual(Buffer.concat(cors.sent.filter(message => message.phase === 'data').map(message => Buffer.from(message.data, 'base64'))), Buffer.from(bytes));
  assert.equal(cors.permissionChecks.length, 0, 'Normal CORS success must not trigger a permission check');
  assert.equal(cors.timers.size, 0);
  const redirected = fixture(async () => responseFor('https://other.example.com:8443/video.mp4', new Uint8Array([1, 2, 3])), { permission: () => false });
  await redirected.run();
  assert.equal(redirected.sent[0].url, 'https://other.example.com:8443/video.mp4');
  assert.equal(redirected.sent.at(-1).phase, 'end');
  assert.equal(redirected.permissionChecks.length, 0);
  assert.equal(redirected.timers.size, 0);
  const missingPermission = fixture(async () => { throw new TypeError('Failed to fetch'); }, { permission: () => false });
  await assert.rejects(missingPermission.run({ url: 'https://player.example.com:8443/watch' }), error => {
    assert.equal(error.origin, 'https://player.example.com/*'); return true;
  });
  assert.deepEqual(missingPermission.permissionChecks, ['https://player.example.com:8443/watch']);
  assert.equal(missingPermission.sent.length, 0);
  assert.equal(missingPermission.timers.size, 0);
  const grantedFailure = fixture(async () => { throw new TypeError('Failed to fetch'); }, { permission: () => true });
  await assert.rejects(grantedFailure.run(), error => {
    assert.equal(error.name, 'TypeError');
    assert.equal(error.origin, undefined); return true;
  });
  assert.equal(grantedFailure.permissionChecks.length, 1);
  assert.equal(grantedFailure.timers.size, 0);
  for (const status of [401, 403]) {
    const rejected = fixture(async url => responseFor(url, new TextEncoder().encode('access denied'), { 'Content-Type': 'text/plain' }, status), { permission: () => false });
    await rejected.run();
    assert.equal(rejected.sent[0].status, status);
    assert.equal(rejected.sent[0].origin, undefined);
    assert.equal(rejected.sent.at(-1).phase, 'end');
    assert.equal(rejected.permissionChecks.length, 0, 'HTTP denials must reach the Host without permission errors');
    assert.equal(rejected.timers.size, 0);
  }
  for (const url of ['file:///tmp/video.mp4', 'http://127.0.0.1/video.mp4', 'https://user:pass@example.com/video.mp4']) {
    const invalid = fixture(async target => responseFor(target));
    await assert.rejects(invalid.run({ url }), /公开 HTTP/);
    assert.equal(invalid.calls.length, 0);
    assert.equal(invalid.timers.size, 0);
  }
  const privateRedirect = fixture(async () => responseFor('http://10.0.0.1/video.mp4'));
  await assert.rejects(privateRedirect.run(), /公开 HTTP/);
  assert.equal(privateRedirect.sent.length, 0);
  assert.equal(privateRedirect.timers.size, 0);
}

async function invalidRequests() {
  for (const range of ['bytes=9-2', 'bytes=-0', 'bytes=0-\r\nOrigin: forged', 'bytes=0-1,2-3', 0, 42]) {
    const invalid = fixture(async url => responseFor(url));
    await assert.rejects(invalid.run({ range }), /Range/);
    assert.equal(invalid.calls.length, 0);
    assert.equal(invalid.timers.size, 0);
  }
  const invalidMethod = fixture(async url => responseFor(url));
  await assert.rejects(invalidMethod.run({ method: 'POST' }), /GET\/HEAD/);
  assert.equal(invalidMethod.calls.length, 0);
  assert.equal(invalidMethod.timers.size, 0);
}

async function cancellationAndCleanup() {
  const pending = fixture(async url => responseFor(url, new Uint8Array(20)), { stallAck: true });
  const running = pending.run();
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.equal(pending.sent.length, 1);
  pending.state.controller.abort();
  await assert.rejects(running, error => error.name === 'AbortError');
  assert.equal(pending.state.ack, null);
  assert.equal(pending.timers.size, 0);
  assert.equal(pending.state.nextSeq, 1, 'Caller error message must continue after the sent header');
  const stopped = fixture(async url => responseFor(url));
  stopped.state.controller.abort();
  await assert.rejects(stopped.run(), error => error.name === 'AbortError');
  assert.equal(stopped.calls.length, 0);
  assert.equal(stopped.timers.size, 0);
  const rejected = fixture(async url => responseFor(url), { rejectAck: true });
  await assert.rejects(rejected.run(), /rejected chunk/);
  assert.equal(rejected.sent.length, 1);
  assert.equal(rejected.state.ack, null);
  assert.equal(rejected.timers.size, 0);
  const timeout = fixture(async (_url, args) => new Promise((_resolve, reject) => {
    args.signal.addEventListener('abort', () => reject(args.signal.reason), { once: true });
  }));
  const timed = timeout.run();
  await new Promise(resolve => setTimeout(resolve, 0));
  const idle = [...timeout.timers.values()].find(timer => timer.ms === 60000);
  assert(idle);
  idle.fn();
  await assert.rejects(timed, /超时/);
  assert.equal(timeout.timers.size, 0);
}

async function resourceSizeLimit() {
  let cancelled = false;
  let released = false;
  const huge = fixture(async url => ({
    status: 200, url, headers: new Headers(),
    body: { getReader: () => ({
      read: async () => ({ done: false, value: { byteLength: 2 * 1024 * 1024 * 1024 + 1 } }),
      cancel: async () => { cancelled = true; }, releaseLock: () => { released = true; },
    }) },
  }));
  await assert.rejects(huge.run(), /2 GiB/);
  assert.equal(huge.sent.length, 1);
  assert.equal(huge.state.nextSeq, 1);
  assert(cancelled && released);
  assert.equal(huge.timers.size, 0);
}

(async () => {
  await streamingAndBackpressure();
  await headsAndCompression();
  await permissionsAndUrls();
  await invalidRequests();
  await cancellationAndCleanup();
  await resourceSizeLimit();
  console.log('Background HTTP streaming, permissions, backpressure, cancellation and cleanup passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
