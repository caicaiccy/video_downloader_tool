"""Offline policy, real-media integration, queue and native framing checks."""
import contextlib
import http.server
import io
import json
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import imageio_ffmpeg
import yt_dlp
from yt_dlp.networking import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import downloader_core as core
import native_host as host
from media_policy import MediaPolicyError, is_public_web_url, validate_manifest
from browser_transport import BrowserTransport
from web_page import VideoPage, scan_page, LinkedPageIE
from media_policy import PublicMediaDL
from yt_dlp.networking.exceptions import HTTPError
import urllib.request


class PolicyTests(unittest.TestCase):
    def test_bilibili_keeps_its_dedicated_extractor(self):
        with PublicMediaDL({'quiet':True},threading.Event()) as downloader:
            downloader.add_info_extractor(LinkedPageIE(downloader,lambda text:None))
            extractor=downloader.get_info_extractor('BiliBili')
            with patch.object(extractor,'_real_extract',return_value={'id':'test','title':'Bilibili 测试','url':'https://cdn.example.com/sample.mp4'}) as bili, patch.object(LinkedPageIE,'_real_extract',side_effect=AssertionError('Bilibili must not use generic page scan')):
                info=downloader.extract_info('https://www.bilibili.com/video/BV1xx411c7mD',download=False,process=False)
            self.assertEqual(info['title'],'Bilibili 测试')
            bili.assert_called_once()

    def test_page_parser_does_not_execute_code_or_infer_unused_mp4(self):
        page = VideoPage('https://example.com/watch')
        page.feed('''<title>Episode &amp; title</title><video></video>
            <iframe src="http://127.0.0.1/private"></iframe>
            <script>var adposter='https://cdn.example.com/adposter.mp4';
            var Vurl='https://cdn.example.com/main.mp4'; var evil='https://u:p@example.com/private.mp4';
            const settings={url:Vurl};</script>''')
        self.assertEqual(page.title,'Episode & title')
        self.assertEqual(list(page.candidates),['https://cdn.example.com/main.mp4'])
        self.assertFalse(page.frames)

    def test_page_scan_has_request_and_depth_bounds(self):
        from yt_dlp.networking import Response
        requests=[]
        class Pages:
            def urlopen(self, request):
                requests.append(request.url)
                n=int(request.url.rsplit('/',1)[-1])
                html=f'<iframe src="/{n+1}"></iframe><iframe src="/{n}"></iframe>'
                return Response(io.BytesIO(html.encode()),request.url,{'Content-Type':'text/html'},200)
        self.assertIsNone(scan_page(Pages(),'https://example.com/0'))
        self.assertLessEqual(len(requests),4)
        self.assertEqual(len(requests),len(set(requests)))

    def test_browser_transport_bounds_and_cancellation(self):
        events = []
        transport = BrowserTransport(events.append, threading.Event())
        def send(message):
            events.append(message)
            if message['type'] == 'browser_request':
                transport.receive({'requestId':message['requestId'], 'seq':0, 'phase':'headers',
                    'status':200, 'url':message['url'], 'headers':{'Content-Type':'video/mp4','Set-Cookie':'never-forward'}})
                transport.receive({'requestId':message['requestId'], 'seq':2, 'phase':'data','data':'YQ=='})
        transport.send_message = send
        with self.assertRaisesRegex(MediaPolicyError, '顺序'):
            transport.urlopen(Request('https://example.com/video.mp4'), 'job')
        self.assertFalse(transport.pending)
        transport.send_message = lambda msg: transport.cancel() if msg['type']=='browser_request' else None
        with self.assertRaisesRegex(MediaPolicyError, '取消'):
            transport.urlopen(Request('https://example.com/video.mp4'), 'job')
        self.assertFalse(transport.pending)

    def test_url_boundaries(self):
        for url in ("https://www.bilibili.com/video/BV123", "https://cdn.example.com/a.m3u8?token=public"):
            self.assertTrue(is_public_web_url(url))
        for url in ("file:///etc/passwd", "blob:https://example.com/id", "http://127.0.0.1/a", "http://127.1/a",
                    "http://2130706433/a", "http://0x7f000001/a", "http://[::1]/a", "http://host.local/a",
                    "https://u:p@example.com/a", "https://example.com:bad/a", "https://example.com/a\r\nCookie:x"):
            self.assertFalse(is_public_web_url(url), url)
        self.assertTrue(core.is_bilibili_url("https://www.bilibili.com/video/BV1"))
        self.assertFalse(core.is_bilibili_url("https://bilibili.com.evil.test/video/BV1"))

    def test_manifests(self):
        validate_manifest(b"#EXTM3U\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST", "hls")
        for text in (b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"',
                     b'#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="key"',
                     b'#EXTM3U\n#EXTINF:1,\na.ts', b'<html>denied</html>'):
            with self.assertRaises(MediaPolicyError): validate_manifest(text, "hls")
        validate_manifest(b"#EXTM3U\n#EXT-X-KEY:METHOD=NONE\n#EXTINF:1,\na.ts\n#EXT-X-ENDLIST", "hls")
        for text in (b'<MPD><ContentProtection/></MPD>', b'<MPD type="dynamic"/>'):
            with self.assertRaises(MediaPolicyError): validate_manifest(text, "dash")

    def test_native_job_validation_and_legacy(self):
        self.assertEqual(host.normalize_job({"id": "old", "url": "https://www.bilibili.com/video/BV1"})["id"], "old")
        job = host.normalize_job({"id": "new", "url": "https://example.com/watch", "media": {
            "url": "https://cdn.example.com/a.m3u8", "kind": "hls", "referrer": "https://player.example.com/play",
            "headers": {"Cookie": "must-not-forward"}}, "title": "test"})
        self.assertNotIn("headers", job["media"])
        with self.assertRaises(ValueError): host.normalize_job({"id": "x", "url": "https://example.com", "media": {"url": "blob:abc", "kind": "direct"}})
        self.assertEqual(host.normalize_job({'id':'background','url':'https://example.com/watch','transport':'background'})['transport'],'background')
        with self.assertRaises(ValueError):host.normalize_job({'id':'x','url':'https://example.com/watch','transport':'arbitrary'})

    def test_queue_retry_and_restriction(self):
        host.cancel_event.clear()
        messages = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "success.mp4"
            path.write_bytes(b"result")
            with patch.object(host, "send_message", messages.append), patch.object(host, "download_video", side_effect=[
                RuntimeError("connection reset"), path, RuntimeError("HTTP Error 403: Forbidden"), path,
            ]) as download, patch.object(host.cancel_event, "wait", return_value=False):
                host.run_queue([{"id": str(i), "url": "https://example.com/video"} for i in range(3)], Path(tmp), 3)
            self.assertEqual(download.call_count, 4)
            self.assertEqual([m["status"] for m in messages if m["type"] == "job_state" and m["id"] == "1"], ["下载中", "失败"])
            self.assertEqual(messages[-1], {"type": "queue_done", "cancelled": False})
        host.cancel_event.clear()
        messages.clear()
        def cancel(**kwargs):
            host.cancel_event.set()
            raise RuntimeError("cancelled")
        with patch.object(host, "send_message", messages.append), patch.object(host, "download_video", side_effect=cancel) as download:
            host.run_queue([{"id": "a", "url": "https://example.com/a"}, {"id": "b", "url": "https://example.com/b"}], Path("."), 3)
            self.assertEqual(download.call_count, 1)
            self.assertTrue(messages[-1]["cancelled"])
        host.cancel_event.clear()

    def test_native_wire_protocol(self):
        body = json.dumps({"type": "start_queue", "jobs": [{"id": "bad", "url": "file:///etc/passwd"}]}).encode()
        result = subprocess.run([sys.executable, str(Path(host.__file__))], input=struct.pack("<I", len(body)) + body,
                                capture_output=True, check=True)
        stream = io.BytesIO(result.stdout)
        def read():
            size = struct.unpack("<I", stream.read(4))[0]
            return json.loads(stream.read(size))
        ready = read()
        self.assertIn("public_web_media", ready["capabilities"])
        self.assertIn("page_scan", ready["capabilities"])
        self.assertIn("background_http", ready["capabilities"])
        self.assertEqual(read()["type"], "error")
        self.assertEqual(stream.read(), b"")

    def test_policy_failures_do_not_retry(self):
        host.cancel_event.clear()
        with patch.object(host, "send_message"), patch.object(host, "download_video", side_effect=MediaPolicyError("响应不是有效的 HLS 清单。")) as download:
            host.run_queue([{"id": "a", "url": "https://example.com/a"}], Path("."), 5)
            self.assertEqual(download.call_count, 1)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args): pass


class RealMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        subprocess.run(base + ["-f", "lavfi", "-i", "testsrc=size=160x90:rate=10", "-f", "lavfi", "-i", "sine=frequency=440",
                              "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(cls.root / "sample.mp4")], check=True)
        subprocess.run(base + ["-i", str(cls.root / "sample.mp4"), "-c", "copy", "-hls_time", "0.5", "-hls_playlist_type", "vod",
                              str(cls.root / "clear.m3u8")], check=True)
        subprocess.run(base + ["-i", str(cls.root / "sample.mp4"), "-c", "copy", "-f", "dash", str(cls.root / "clear.mpd")], check=True)
        (cls.root / "page.html").write_text('<html><title>Fixture</title><video src="sample.mp4"></video></html>')
        (cls.root / "nested.html").write_text('<html><title>单集标题</title><iframe src="player.html"></iframe></html>')
        (cls.root / "player.html").write_text('''<title>播放器</title><video></video><script>
            var adposter="http://fixtures.example.test/adposter.mp4";
            var Vurl="http://fixtures.example.test/clear.m3u8"; const config={url:Vurl};</script>''')
        alternate_root = cls.root / "play" / "show" / "1"
        alternate_page = cls.root / "play" / "show" / "2"
        alternate_root.mkdir(parents=True)
        alternate_page.mkdir(parents=True)
        alternate_root.joinpath("3").write_text('''<title>同集备用来源</title><iframe src="/opaque-player.html"></iframe>
            <a class="video_detail_spisode_link" href="/play/show/1/3">第03集</a>
            <a class="video_detail_spisode_link" href="/play/show/2/3">第03集</a>''')
        alternate_page.joinpath("3").write_text('<title>同集备用来源</title><iframe src="/player.html"></iframe>')
        (cls.root / "opaque-player.html").write_text('<script>var Vurl="AFCCB3B6EADFEB701DE7E752A5EA7CFD";</script>')
        (cls.root / "stream").write_bytes((cls.root / "sample.mp4").read_bytes())
        (cls.root / "extensionless.html").write_text('<title>无扩展名视频</title><video src="stream"></video>')
        (cls.root / "metadata.html").write_text('''<title>结构化视频</title><script type="application/ld+json">
            {"@type":"VideoObject","contentUrl":"http://fixtures.example.test/sample.mp4"}</script>''')
        (cls.root / "multiple.html").write_text('<video src="sample.mp4"></video><video src="stream"></video>')
        (cls.root / "encrypted.m3u8").write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="secret.key"\n#EXTINF:1,\nclear0.ts\n#EXT-X-ENDLIST')
        (cls.root / "master.m3u8").write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000\nencrypted.m3u8')
        (cls.root / "drm.mpd").write_text('<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period><ContentProtection/></Period></MPD>')
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **kw: QuietHandler(*a, directory=cls.root, **kw))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        self.requests = []
        original = yt_dlp.YoutubeDL.urlopen
        def fixture_transport(downloader, request):
            # Only the test transport maps a named public fixture URL to localhost.
            # Production policy and all actual extraction/download code still run.
            request = Request(request) if isinstance(request, str) else request
            public_url = request.url
            self.assertTrue(public_url.startswith("http://fixtures.example.test/"), public_url)
            self.requests.append((public_url, dict(request.headers)))
            local = request.copy()
            local.url = public_url.replace("http://fixtures.example.test", f"http://127.0.0.1:{self.server.server_port}", 1)
            response = original(downloader, local)
            response.url = public_url
            return response
        self.transport = patch.object(yt_dlp.YoutubeDL, "urlopen", fixture_transport)
        self.transport.start()
        self.addCleanup(self.transport.stop)

    def download(self, name, media=True):
        with tempfile.TemporaryDirectory() as tmp:
            url = "http://fixtures.example.test/" + name
            titles=[]
            saved = core.download_video(url if not media else "https://example.com/watch", Path(tmp), threading.Event(),
                lambda *a: None, lambda *a: None, lambda *a: None,
                media={"url": url, "kind": "hls" if name.endswith("m3u8") else "dash" if name.endswith("mpd") else "direct",
                       "referrer": "https://player.example.com/watch"} if media else None, title="测试视频", on_metadata=titles.append)
            self.assertGreater(saved.stat().st_size, 1000)
            if name == "nested.html":
                self.assertEqual(titles,['单集标题'])
                self.assertTrue(saved.name.startswith('单集标题'))
            result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(saved), "-f", "null", "-"], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Downloading the same task again must return the exact existing file.
            again = core.download_video(url if not media else "https://example.com/watch", Path(tmp), threading.Event(),
                lambda *a: None, lambda *a: None, lambda *a: None,
                media={"url": url, "kind": "hls" if name.endswith("m3u8") else "dash" if name.endswith("mpd") else "direct",
                       "referrer": "https://player.example.com/watch"} if media else None, title="测试视频")
            self.assertEqual(saved, again)

    def test_direct(self): self.download("sample.mp4")
    def test_hls(self): self.download("clear.m3u8")
    def test_dash(self): self.download("clear.mpd")
    def test_generic_page(self): self.download("page.html", media=False)

    def test_nested_player_link_download_without_browser(self):
        self.download('nested.html', media=False)
        self.assertFalse(any('adposter' in url for url,_ in self.requests))
        self.assertTrue(any(url.endswith('/player.html') and headers.get('Referer')=='http://fixtures.example.test/nested.html' for url,headers in self.requests))
        self.assertTrue(any(url.endswith('/clear.m3u8') and headers.get('Referer')=='http://fixtures.example.test/player.html' for url,headers in self.requests))

    def test_same_episode_alternate_source_fallback(self):
        self.download('play/show/1/3', media=False)
        self.assertTrue(any(url.endswith('/opaque-player.html') for url,_ in self.requests))
        self.assertTrue(any(url.endswith('/play/show/2/3') for url,_ in self.requests))
        self.assertTrue(any(url.endswith('/clear.m3u8') for url,_ in self.requests))

    def test_extensionless_video_page(self): self.download('extensionless.html', media=False)
    def test_structured_video_page(self): self.download('metadata.html', media=False)

    def test_multiple_resources_stop_before_download(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(MediaPolicyError,'多个视频'):
            core.download_video('http://fixtures.example.test/multiple.html',Path(tmp),threading.Event(),lambda *a:None,lambda *a:None,lambda *a:None)
        self.assertFalse(any(url.endswith(('sample.mp4','/stream')) for url,_ in self.requests))

    def test_browser_relay_real_hls_and_dash(self):
        import base64
        browser_requests = []
        transport = BrowserTransport(lambda m: None, threading.Event())
        def send(message):
            if message['type'] != 'browser_request': return
            request_id = message['requestId']
            public_url = message['url']
            self.assertTrue(public_url.startswith('http://fixtures.example.test/'))
            browser_requests.append(public_url)
            local = public_url.replace('http://fixtures.example.test', f'http://127.0.0.1:{self.server.server_port}', 1)
            request = urllib.request.Request(local, method=message['method'])
            with urllib.request.urlopen(request) as response:
                headers = {k:v for k,v in response.headers.items() if k.lower() in {'content-type','content-length','content-range'}}
                seq = 0
                transport.receive({'requestId':request_id,'seq':seq,'phase':'headers','url':public_url,'status':response.status,'headers':headers})
                if message['method'] != 'HEAD':
                    while chunk := response.read(65536):
                        seq += 1
                        transport.receive({'requestId':request_id,'seq':seq,'phase':'data','data':base64.b64encode(chunk).decode()})
                transport.receive({'requestId':request_id,'seq':seq+1,'phase':'end','method':message['method']})
        transport.send_message = send
        for name in ('clear.m3u8','clear.mpd','sample.mp4','nested.html'):
            with tempfile.TemporaryDirectory() as tmp:
                saved = core.download_video('http://fixtures.example.test/nested.html' if name == 'nested.html' else 'https://example.com/watch', Path(tmp), threading.Event(), lambda *a:None,lambda *a:None,lambda *a:None,
                    media={'url':'http://fixtures.example.test/'+name,'kind':'hls' if name.endswith('m3u8') else 'dash' if name.endswith('mpd') else 'direct'} if name != 'nested.html' else None,
                    browser_open=lambda req:transport.urlopen(req, 'fixture'))
                result = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),'-v','error','-i',str(saved),'-f','null','-'],capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(all(url.endswith('.html') for url,_ in self.requests), 'Only static page discovery uses native HTTP; all media stays in browser transport')
        self.assertTrue(any(url.endswith('player.html') for url,_ in self.requests))
        self.assertTrue(any(url.endswith('.ts') for url in browser_requests))
        browser_requests.clear()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(MediaPolicyError, '加密'):
            core.download_video('https://example.com/watch',Path(tmp),threading.Event(),lambda *a:None,lambda *a:None,lambda *a:None,
                media={'url':'http://fixtures.example.test/master.m3u8','kind':'hls'},
                browser_open=lambda req:transport.urlopen(req,'fixture'))
        self.assertFalse(any(url.endswith(('secret.key','clear0.ts')) for url in browser_requests))
        self.assertFalse(transport.pending)

    def test_http_region_refusal_fails_before_download(self):
        from yt_dlp.networking import Response
        calls = []
        def rejected(req):
            url = req if isinstance(req,str) else req.url
            calls.append(url)
            raise HTTPError(Response(io.BytesIO(b'The region has been denied.'),url,{'Content-Type':'text/html'},403))
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(MediaPolicyError, '地区'):
            core.download_video('https://example.com/master.m3u8',Path(tmp),threading.Event(),lambda *a:None,lambda *a:None,lambda *a:None,browser_open=rejected)
            self.assertFalse(list(Path(tmp).iterdir()))
        self.assertEqual(len(calls),1)

    def test_encryption_never_fetches_keys_or_segments(self):
        for name in ("encrypted.m3u8", "master.m3u8", "drm.mpd"):
            with tempfile.TemporaryDirectory() as tmp, self.assertRaises((MediaPolicyError, yt_dlp.utils.DownloadError)):
                core.download_video("http://fixtures.example.test/" + name, Path(tmp), threading.Event(), lambda *a: None, lambda *a: None, lambda *a: None)
        self.assertFalse(any(url.endswith(("secret.key", "clear0.ts")) for url, _ in self.requests))


if __name__ == "__main__": unittest.main()
