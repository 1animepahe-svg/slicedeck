import http.client
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import server

SECRET = 'A' * 43 # Test fixture only; never use in deployment.
DATA = {'url': 'https://youtu.be/abcdefghijk?t=2', 'start': 0, 'end': 2, 'format': 'mp4', 'quality': '1080'}

class Validation(unittest.TestCase):
    def test_links_and_ranges(self):
        self.assertEqual(server.validate(DATA)[0], 'https://www.youtube.com/watch?v=abcdefghijk')
        for value in ['http://youtube.com/watch?v=abcdefghijk', 'https://youtube.com.evil.test/watch?v=abcdefghijk',
                      'https://youtube.com@evil.test/watch?v=abcdefghijk', 'https://user@youtube.com/watch?v=abcdefghijk',
                      'https://youtube.com:444/watch?v=abcdefghijk', 'https://youtube.com/watch?v=abcdefghijk&v=12345678901',
                      'file:///etc/passwd', 'https://youtu.be/abcdefghijk/extra']:
            with self.subTest(value=value), self.assertRaises(server.APIError): server.canonical_url(value)
        for change in [{'start': True}, {'start': float('nan')}, {'start': -1}, {'end': 121},
                       {'end': 0}, {'start': 10**1000}, {'quality': 1080}, {'quality': '4k'},
                       {'format':'webm'}, {'end':14401}, {'extra': 'x'}]:
            with self.subTest(change=change), self.assertRaises(server.APIError): server.validate({**DATA, **change})
        server.validate({**DATA, 'end': 120, 'quality':'720'})

class HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.http = server.Server(('127.0.0.1', 0), SECRET, cls.tmp.name)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True); cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join(); cls.tmp.cleanup()
    def setUp(self):
        with server.RATE_LOCK: server.JOBS.clear(); server.REQUESTS.clear()
    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.http.server_address[1], timeout=10)
        h = {'Origin': server.ORIGIN, 'Authorization': 'Bearer ' + SECRET}
        h.update(headers or {})
        c.request(method, path, body, h); r = c.getresponse()
        result = (r.status, dict(r.getheaders()), r.read()); c.close(); return result
    def test_auth_origin_and_health(self):
        s,h,b = self.request('GET','/api/health')
        self.assertEqual(s,200); self.assertTrue(json.loads(b)['ok'])
        self.assertEqual(h['Access-Control-Allow-Origin'], server.ORIGIN)
        for token in ['', 'Bearer wrong', 'Basic ' + SECRET]:
            self.assertEqual(self.request('GET','/api/health',headers={'Authorization':token})[0],401)
        s,h,_=self.request('GET','/api/health',headers={'Origin':'https://evil.test'})
        self.assertEqual(s,403); self.assertNotIn('Access-Control-Allow-Origin', h)
        self.assertEqual(self.request('GET','/api/health?token=abc')[0],404)
    def test_duplicate_auth_and_origin(self):
        for name, value in [('Authorization', 'Bearer '+SECRET), ('Origin',server.ORIGIN)]:
            c=http.client.HTTPConnection('127.0.0.1',self.http.server_address[1])
            c.putrequest('GET','/api/health'); c.putheader('Origin',server.ORIGIN)
            c.putheader('Authorization','Bearer '+SECRET); c.putheader(name,value); c.endheaders()
            r=c.getresponse(); self.assertIn(r.status,(401,403)); r.read(); c.close()
    def test_preflight(self):
        h={'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type', 'Authorization':''}
        s,headers,_=self.request('OPTIONS','/api/clip',headers=h)
        self.assertEqual(s,204); self.assertIn('Authorization',headers['Access-Control-Allow-Headers'])
        self.assertEqual(self.request('OPTIONS','/api/clip',headers={**h,'Origin':'https://evil.test'})[0],403)
        self.assertEqual(self.request('OPTIONS','/api/clip',headers={**h,'Access-Control-Request-Headers':'x-evil'})[0],403)
    def test_body_validation(self):
        h={'Content-Type':'application/json'}
        self.assertEqual(self.request('POST','/api/clip','{}',h)[0],400)
        self.assertEqual(self.request('POST','/api/clip','x'*4097,h)[0],413)
        self.assertEqual(self.request('POST','/api/clip','{}',{'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json','Authorization':''})[0],401)
    def test_clip_response_cleanup_and_quota(self):
        paths=[]
        def fake(data, directory):
            p=Path(directory)/'clip.mp4'; p.write_bytes(b'mp4test'); paths.append(p); return p
        with patch.object(server,'make_clip',fake):
            for _ in range(3):
                s,h,b=self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})
                self.assertEqual((s,h['Content-Type'],b),(200,'video/mp4',b'mp4test'))
            self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})[0],429)
        for _ in range(50):
            if not any(p.exists() for p in paths): break
            time.sleep(.01)
        self.assertFalse(any(p.exists() for p in paths))
    def test_busy_and_error_cleanup(self):
        server.BUSY.acquire()
        try: self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})[0],429)
        finally: server.BUSY.release()
        with patch.object(server,'make_clip',side_effect=server.APIError(502,'failed')):
            self.assertEqual(self.request('POST','/api/clip',json.dumps(DATA),{'Content-Type':'application/json'})[0],502)
        self.assertTrue(server.BUSY.acquire(blocking=False)); server.BUSY.release()
        self.assertEqual(list(Path(self.tmp.name).iterdir()),[])
    def test_rate_limits(self):
        with server.RATE_LOCK:
            server.JOBS.extend([time.monotonic()-4000]*8)
        self.assertFalse(server.job_allowed())
        with server.RATE_LOCK:
            server.REQUESTS.extend([time.monotonic()]*100)
        self.assertFalse(server.request_allowed())

class Processing(unittest.TestCase):
    def test_timeout_and_temp_limit(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(server.APIError) as e:
                server.run_command([sys.executable,'-c','import time; time.sleep(4)'],d,time.monotonic()+0.2)
            self.assertEqual(e.exception.status,504)
            Path(d,'big').write_bytes(b'x'*100)
            with patch.object(server,'MAX_WORKSPACE',50), self.assertRaises(server.APIError) as e:
                server.run_command([sys.executable,'-c','import time; time.sleep(2)'],d,time.monotonic()+3)
            self.assertEqual(e.exception.status,413)
    def test_audio_only_output_is_rejected(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            output = Path(d, 'clip.mp4')
            def fake_command(command, directory, deadline): output.write_bytes(b'fake')
            probe = SimpleNamespace(stdout=json.dumps({'format': {'duration': '2'}, 'streams': [
                {'codec_type': 'audio', 'codec_name': 'aac'}]}).encode())
            with patch.object(server, 'run_command', fake_command), patch.object(server.subprocess, 'run', return_value=probe):
                with self.assertRaises(server.APIError) as e:
                    server.transcode(Path(d,'input'),output,'1080',2,d,time.monotonic()+5)
                self.assertEqual(e.exception.status, 502)
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'ffmpeg required')
    def test_real_1080p_and_no_upscale(self):
        with tempfile.TemporaryDirectory() as d:
            source=Path(d,'sample.mp4'); out=Path(d,'clip.mp4')
            subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-f','lavfi','-i','color=c=blue:s=1920x1080:r=5:d=2',
                            '-c:v','libx264','-threads','1','-preset','ultrafast','-y',str(source)],check=True,timeout=15)
            server.transcode(source,out,'1080',2,d,time.monotonic()+30)
            info=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries',
                                          'stream=codec_name,width,height','-of','json',str(out)])
            stream=json.loads(info)['streams'][0]
            self.assertEqual((stream['codec_name'],stream['width'],stream['height']),('h264',1920,1080))
            out.unlink(); server.transcode(source,out,'720',2,d,time.monotonic()+30)
            info=subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0','-show_entries',
                                          'stream=width,height','-of','json',str(out)])
            stream=json.loads(info)['streams'][0]
            self.assertEqual((stream['width'],stream['height']),(1280,720))

if __name__ == '__main__': unittest.main()
