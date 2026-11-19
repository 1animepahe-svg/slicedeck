import http.client
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import server

ORIGIN = 'https://example.github.io'
DATA = {'url':'https://youtu.be/abcdefghijk?t=2', 'start': 3, 'end': 5, 'format':'mp4', 'quality':'720'}

class ValidationTests(unittest.TestCase):
    def test_canonical_urls(self):
        for url in ['https://youtu.be/abcdefghijk?t=2', 'https://www.youtube.com/watch?v=abcdefghijk&list=foo', 'https://m.youtube.com/shorts/abcdefghijk']:
            self.assertEqual(server.canonical_url(url), 'https://www.youtube.com/watch?v=abcdefghijk')
    def test_rejected_urls(self):
        for url in ['http://youtube.com/watch?v=abcdefghijk', 'https://youtube.com.evil.test/watch?v=abcdefghijk', 'https://youtube.com@evil.test/watch?v=abcdefghijk', 'https://user@youtube.com/watch?v=abcdefghijk', 'https://www.youtube.com:444/watch?v=abcdefghijk', 'https://youtu.be/abcdefghijk/extra', 'https://www.youtube.com/watch?v=abcdefghijk&v=lmnopqrstuv', 'file:///etc/passwd']:
            with self.subTest(url=url), self.assertRaises(server.APIError): server.canonical_url(url)
    def test_clip_validation(self):
        for change in [{'start': True}, {'start': float('nan')}, {'start': -1}, {'end': 905}, {'end': 3}, {'start': 10**1000}, {'format': 'webm'}, {'quality': 720}]:
            with self.subTest(change=change), self.assertRaises(server.APIError): server.validate({**DATA, **change})
        server.validate({**DATA, 'start': 0, 'end': 900})
    def test_limits_and_config(self):
        with self.assertRaises(Exception): server.parse_origin('https://example.github.io/path')
        with self.assertRaises(Exception): server.parse_origin('http://evil.example')
        self.assertEqual(server.parse_origin(ORIGIN), ORIGIN)

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.Server(('127.0.0.1', 0), [ORIGIN])
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join()
    def setUp(self):
        with server.RATE_LOCK: server.RECENT.clear()
    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
        headers = {'Origin': ORIGIN, **(headers or {})}
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse(); result = (r.status, dict(r.getheaders()), r.read()); conn.close()
        return result
    def test_health_and_cors(self):
        status, headers, body = self.request('GET', '/api/health')
        self.assertEqual(status, 200); self.assertTrue(json.loads(body)['ok'])
        self.assertEqual(headers['Access-Control-Allow-Origin'], ORIGIN)
    def test_origin_and_host(self):
        self.assertEqual(self.request('GET','/api/health',headers={'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('GET','/api/health',headers={'Origin':''})[0],403)
        self.assertEqual(self.request('GET','/api/health',headers={'Host':'evil.test'})[0],403)
    def test_duplicate_origin_and_host_rejected(self):
        for name, value in [('Origin', ORIGIN), ('Host', '127.0.0.1:%s' % self.http.server_address[1])]:
            conn = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=5)
            conn.putrequest('GET', '/api/health', skip_host=True)
            conn.putheader('Host', '127.0.0.1:%s' % self.http.server_address[1])
            conn.putheader('Origin', ORIGIN)
            conn.putheader(name, value)
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 403)
            response.read(); conn.close()
    def test_preflight(self):
        status, h, _ = self.request('OPTIONS','/api/clip',headers={'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type','Access-Control-Request-Private-Network':'true'})
        self.assertEqual(status,204); self.assertEqual(h['Access-Control-Allow-Private-Network'],'true')
        self.assertEqual(self.request('OPTIONS','/api/clip',headers={'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization'})[0],403)
    def test_post_validation(self):
        h={'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/clip','not json',h)[0],400)
        self.assertEqual(self.request('POST','/api/clip',json.dumps({**DATA,'end':999}),h)[0],400)
        self.assertEqual(self.request('POST','/api/clip','x'*8193,h)[0],413)
        self.assertEqual(self.request('POST','/api/clip','{}',{'Content-Type':'text/plain'})[0],415)
    def test_mp4_response_and_cleanup(self):
        seen=[]
        def fake(data, directory):
            p=Path(directory)/'clip.mp4'; p.write_bytes(b'fake mp4'); seen.append(p); return p
        with patch.object(server, 'make_clip', fake):
            status, h, body = self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})
        self.assertEqual((status,h['Content-Type'],body),(200,'video/mp4',b'fake mp4'))
        # Server cleanup occurs just after the client receives the final bytes.
        for _ in range(100):
            if not seen[0].exists(): break
            server.time.sleep(0.01)
        self.assertFalse(seen[0].exists())
    def test_subprocess_error_as_json_and_unlock(self):
        with patch.object(server, 'make_clip', side_effect=server.APIError(502,'download failed')):
            status,h,body=self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})
        self.assertEqual(status,502); self.assertEqual(json.loads(body)['error'],'download failed')
        self.assertTrue(server.BUSY.acquire(False)); server.BUSY.release()
    def test_busy_and_rate(self):
        server.BUSY.acquire()
        try: self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})[0],429)
        finally: server.BUSY.release()
        with server.RATE_LOCK:
            server.RECENT.clear()
            for _ in range(6): server.RECENT.append(server.time.monotonic())
        self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})[0],429)

class PipelineTests(unittest.TestCase):
    def test_command_and_failure(self):
        with patch.object(server.shutil,'which',side_effect=lambda n: '/fake/'+n), patch.object(server,'run_command',side_effect=server.APIError(502,'failed')) as run, tempfile.TemporaryDirectory() as d:
            with self.assertRaises(server.APIError): server.make_clip(DATA,d)
            args=run.call_args.args[0]
            self.assertIn('--download-sections',args); self.assertIn('*3.000-5.000',args)
            self.assertEqual(args[-1],'https://www.youtube.com/watch?v=abcdefghijk')
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'ffmpeg and ffprobe required')
    def test_real_ffmpeg_pipeline_with_mock_download(self):
        original_run = server.run_command
        original_which = shutil.which
        def fake_which(name): return '/mock/yt-dlp' if name == 'yt-dlp' else original_which(name)
        def fake_download(cmd, timeout, cwd):
            if cmd[0] == '/mock/yt-dlp':
                subprocess.run([original_which('ffmpeg'),'-v','error','-f','lavfi','-i','testsrc2=size=1920x1080:rate=10','-f','lavfi','-i','sine=frequency=440','-t','2','-c:v','mpeg4','-c:a','aac','-y',str(Path(cwd)/'input.mp4')],check=True)
            else: original_run(cmd,timeout,cwd)
        with tempfile.TemporaryDirectory() as d, patch.object(server.shutil,'which',side_effect=fake_which), patch.object(server,'run_command',side_effect=fake_download):
            output=server.make_clip(DATA,d)
            info=json.loads(subprocess.check_output([original_which('ffprobe'),'-v','error','-show_streams','-show_format','-of','json',str(output)]))
            video=next(s for s in info['streams'] if s['codec_type']=='video')
            audio=next(s for s in info['streams'] if s['codec_type']=='audio')
            self.assertEqual((video['codec_name'],video['width'],video['height']),('h264',1280,720))
            self.assertEqual(audio['codec_name'],'aac'); self.assertLessEqual(float(info['format']['duration']),2.1)

if __name__ == '__main__': unittest.main()
