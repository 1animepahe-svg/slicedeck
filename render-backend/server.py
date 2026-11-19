#!/usr/bin/env python3
"""Private, resource-constrained, single-process SliceDeck Render companion."""
import hmac
import json
import math
import os
import re
import resource
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ORIGIN = 'https://1animepahe-svg.github.io'
MAX_BODY = 4096
MAX_CLIP = 120
MAX_OUTPUT = 96 * 1024 * 1024
MAX_WORKSPACE = 192 * 1024 * 1024
PER_FILE_LIMIT = 128 * 1024 * 1024
JOB_SECONDS = 80
ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
TOKEN = re.compile(r'^[A-Za-z0-9_+/=-]{32,128}$')
BUSY = threading.Lock()
RATE_LOCK = threading.Lock()
REQUESTS = deque()
JOBS = deque()

class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise APIError(400, 'Invalid YouTube URL')
    try:
        u = urlsplit(value)
        host, port = (u.hostname or '').lower(), u.port
    except ValueError:
        raise APIError(400, 'Invalid YouTube URL')
    if u.scheme != 'https' or u.username is not None or u.password is not None or port not in (None, 443):
        raise APIError(400, 'Use an HTTPS YouTube link without credentials or custom port')
    video = ''
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if u.path == '/watch':
            ids = parse_qs(u.query, keep_blank_values=True).get('v', [])
            video = ids[0] if len(ids) == 1 else ''
        elif re.fullmatch(r'/(shorts|embed|live)/[A-Za-z0-9_-]{11}/?', u.path):
            video = u.path.strip('/').split('/')[1]
    elif host == 'youtu.be' and re.fullmatch(r'/[A-Za-z0-9_-]{11}/?', u.path):
        video = u.path.strip('/')
    if not ID.fullmatch(video):
        raise APIError(400, 'Use a valid YouTube video link')
    return 'https://www.youtube.com/watch?v=' + video

def validate(data):
    if not isinstance(data, dict) or set(data) != {'url', 'start', 'end', 'format', 'quality'}:
        raise APIError(400, 'Expected url, start, end, format and quality only')
    url = canonical_url(data['url'])
    start, end = data['start'], data['end']
    for n in (start, end):
        if isinstance(n, bool) or not isinstance(n, (int, float)):
            raise APIError(400, 'start and end must be finite seconds')
        if isinstance(n, float) and not math.isfinite(n):
            raise APIError(400, 'start and end must be finite seconds')
    if start < 0 or end > 14400 or end <= start or end - start > MAX_CLIP:
        raise APIError(400, 'Range must be at most 120 seconds within the first 4 hours')
    if data['format'] != 'mp4' or data['quality'] not in ('720', '1080'):
        raise APIError(400, 'Only MP4 at quality 720 or 1080 is supported')
    return url, float(start), float(end)

def request_allowed():
    now = time.monotonic()
    with RATE_LOCK:
        while REQUESTS and REQUESTS[0] <= now - 60: REQUESTS.popleft()
        if len(REQUESTS) >= 100: return False
        REQUESTS.append(now)
        return True

def job_allowed():
    now = time.monotonic()
    with RATE_LOCK:
        while JOBS and JOBS[0] <= now - 86400: JOBS.popleft()
        if len(JOBS) >= 8 or sum(t > now - 3600 for t in JOBS) >= 3: return False
        JOBS.append(now)
        return True

def workspace_size(directory):
    total = 0
    for p in Path(directory).rglob('*'):
        try:
            if p.is_file() and not p.is_symlink(): total += p.stat().st_size
        except FileNotFoundError: pass
    return total

def run_command(command, directory, deadline):
    # A minimal environment avoids passing the API secret or proxy configuration to tools.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': directory,
           'LANG': 'C.UTF-8', 'YT_DLP_NO_PLUGINS': '1'}
    try:
        proc = subprocess.Popen(command, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True, shell=False)
    except OSError:
        raise APIError(503, 'Processing tools unavailable')
    reason = None
    try:
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                reason = APIError(504, 'Processing time limit exceeded')
                break
            if workspace_size(directory) > MAX_WORKSPACE:
                reason = APIError(413, 'Temporary storage limit exceeded')
                break
            time.sleep(0.1)
        if reason:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()
            raise reason
        if proc.returncode != 0:
            raise APIError(502, 'Download or conversion failed; video may be restricted, blocked or unsupported')
        if workspace_size(directory) > MAX_WORKSPACE:
            raise APIError(413, 'Temporary storage limit exceeded')
    finally:
        if proc.poll() is None:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()

def transcode(source, output, quality, duration, directory, deadline):
    q = int(quality)
    # Never upscale; constrain both portrait and landscape to 1920x1080 or 1280x720.
    edge = 1920 if q == 1080 else 1280
    scale = ("scale=w='min(iw,if(gte(iw,ih),%d,%d))':"
             "h='min(ih,if(gte(iw,ih),%d,%d))':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2" % (edge,q,q,edge))
    run_command(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                 '-protocol_whitelist', 'file,pipe', '-threads', '1', '-filter_threads', '1',
                 '-i', str(source), '-t', '%.3f' % duration, '-map', '0:v:0', '-map', '0:a:0?',
                 '-vf', scale, '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                 '-crf', '25', '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart',
                 '-fs', str(MAX_OUTPUT), str(output)], directory, deadline)
    if not output.is_file() or not 0 < output.stat().st_size < MAX_OUTPUT:
        raise APIError(502, 'Output is empty or exceeded the size limit')
    # -fs can stop a render early while returning success. Reject truncated clips.
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': directory, 'LANG': 'C.UTF-8'}
    try:
        probe = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                                '-show_entries', 'format=duration:stream=codec_type,codec_name,width,height,nb_frames',
                                '-of', 'json', str(output)], cwd=directory, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
        info = json.loads(probe.stdout)
        actual = float(info['format']['duration'])
        videos = [s for s in info['streams'] if s.get('codec_type') == 'video']
        if not math.isfinite(actual) or actual < max(0.1, duration - 1.0) or len(videos) != 1:
            raise ValueError()
        v = videos[0]
        w, h = int(v['width']), int(v['height'])
        if v.get('codec_name') != 'h264' or int(v.get('nb_frames', 0)) < 1 or not (0 < min(w,h) <= q and 0 < max(w,h) <= edge):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise APIError(502, 'Output is incomplete or invalid')

def make_clip(data, directory):
    url, start, end = validate(data)
    if not shutil.which('yt-dlp') or not shutil.which('ffmpeg'):
        raise APIError(503, 'Processing tools unavailable')
    deadline = time.monotonic() + JOB_SECONDS
    # Explicit format dimensions cap the source, including portrait; yt-dlp chooses the
    # best available format and final scaling enforces the requested output quality.
    run_command(['yt-dlp', '--ignore-config', '--no-plugin-dirs', '--no-playlist',
                 '--no-progress', '--quiet', '--no-warnings', '--no-part', '--js-runtimes', 'node',
                 '--max-filesize', '96M', '--socket-timeout', '10', '--retries', '1',
                 '--fragment-retries', '1', '--concurrent-fragments', '1',
                 '--match-filter', 'duration <= 14400 & !is_live',
                 '--force-keyframes-at-cuts', '--downloader-args', 'ffmpeg_o:-threads 1',
                 '--download-sections', '*%.3f-%.3f' % (start, end),
                 '-f', 'bv*[height<=1920][width<=1920]+ba/b[height<=1920][width<=1920]',
                 '-S', 'res:%s' % data['quality'],
                 '-o', str(Path(directory) / 'input.%(ext)s'), '--', url], directory, deadline)
    inputs = [p for p in Path(directory).glob('input.*') if p.is_file() and not p.is_symlink()]
    if len(inputs) != 1: raise APIError(502, 'Could not obtain a single video section')
    output = Path(directory) / 'clip.mp4'
    transcode(inputs[0], output, data['quality'], end-start, directory, deadline)
    return output

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, secret, temp_root):
        self.secret = secret
        self.temp_root = temp_root
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)
    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try: super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise
    def process_request_thread(self, request, client_address):
        try: super().process_request_thread(request, client_address)
        finally: self.slots.release()
    def handle_error(self, request, client_address):
        pass # No request or secret logging.

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'SliceDeckRender'
    sys_version = ''
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def log_message(self, fmt, *args): pass
    def allowed_origin(self):
        return self.headers.get_all('Origin', []) == [ORIGIN]
    def cors(self):
        self.send_header('Access-Control-Allow-Origin', ORIGIN)
        self.send_header('Vary', 'Origin')
    def common(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.close_connection = True
    def error_json(self, status, message):
        body = json.dumps({'error': message}).encode()
        self.send_response(status)
        if self.allowed_origin(): self.cors()
        self.common()
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def checked(self, auth=True):
        if not request_allowed(): raise APIError(429, 'Request rate limit exceeded')
        if not self.allowed_origin(): raise APIError(403, 'Origin not allowed')
        if auth:
            vals = self.headers.get_all('Authorization', [])
            expected = 'Bearer ' + self.server.secret
            if len(vals) != 1 or not hmac.compare_digest(vals[0].encode('utf-8'), expected.encode('ascii')):
                raise APIError(401, 'Unauthorized')
    def do_OPTIONS(self):
        try:
            self.checked(auth=False)
            if self.path not in ('/api/clip', '/api/health'): raise APIError(404, 'Not found')
            method = self.headers.get('Access-Control-Request-Method', '')
            if method != ('POST' if self.path == '/api/clip' else 'GET'):
                raise APIError(403, 'Method not allowed')
            requested = self.headers.get('Access-Control-Request-Headers', '')
            headers = {h.strip().lower() for h in requested.split(',') if h.strip()}
            if headers - {'authorization', 'content-type'}: raise APIError(403, 'Header not allowed')
            self.send_response(204); self.cors(); self.common()
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
            self.send_header('Content-Length', '0'); self.end_headers()
        except APIError as e: self.error_json(e.status, e.message)
    def do_GET(self):
        try:
            self.checked()
            if self.path != '/api/health': raise APIError(404, 'Not found')
            body = json.dumps({'ok': True, 'yt_dlp': bool(shutil.which('yt-dlp')),
                               'ffmpeg': bool(shutil.which('ffmpeg'))}).encode()
            self.send_response(200); self.cors(); self.common()
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        except APIError as e: self.error_json(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, TimeoutError): self.close_connection = True
    def do_POST(self):
        response_started = False
        try:
            self.checked()
            if self.path != '/api/clip': raise APIError(404, 'Not found')
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get_all('Transfer-Encoding', []) or len(lengths) != 1:
                raise APIError(400, 'A single Content-Length is required')
            value = lengths[0]
            if not value.isascii() or not value.isdecimal() or len(value) > 8:
                raise APIError(400, 'Invalid Content-Length')
            length = int(value)
            if length < 1 or length > MAX_BODY: raise APIError(413, 'Body too large or empty')
            if self.headers.get_content_type() != 'application/json': raise APIError(415, 'Expected application/json')
            try: data = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError): raise APIError(400, 'Invalid JSON')
            validate(data)
            if not BUSY.acquire(blocking=False): raise APIError(429, 'A clip is already processing')
            try:
                if not job_allowed(): raise APIError(429, 'Clip quota reached; try later')
                with tempfile.TemporaryDirectory(prefix='job-', dir=self.server.temp_root) as directory:
                    path = make_clip(data, directory)
                    self.send_response(200); self.cors(); self.common(); response_started = True
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Disposition', 'attachment; filename="slicedeck-clip.mp4"')
                    self.send_header('Content-Length', str(path.stat().st_size)); self.end_headers()
                    with path.open('rb') as src: shutil.copyfileobj(src, self.wfile, 256 * 1024)
            finally: BUSY.release()
        except APIError as e:
            if not response_started: self.error_json(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, TimeoutError): self.close_connection = True
        except Exception:
            if response_started: self.close_connection = True
            else: self.error_json(500, 'Unexpected processing error')

def main():
    secret = os.environ.get('SLICEDECK_API_TOKEN', '')
    if not TOKEN.fullmatch(secret):
        raise SystemExit('SLICEDECK_API_TOKEN must be a strong random token of 32-128 characters')
    port = os.environ.get('PORT', '10000')
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise SystemExit('PORT must be a valid port')
    # Limit each file created by yt-dlp/ffmpeg, including their descendants.
    resource.setrlimit(resource.RLIMIT_FSIZE, (PER_FILE_LIMIT, PER_FILE_LIMIT))
    root = '/tmp/slicedeck-render-private'
    if os.path.lexists(root): shutil.rmtree(root)
    os.mkdir(root, 0o700)
    server = Server(('0.0.0.0', int(port)), secret, root)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)

if __name__ == '__main__': main()
