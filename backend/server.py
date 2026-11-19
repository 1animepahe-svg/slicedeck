#!/usr/bin/env python3
"""Optional loopback-only SliceDeck clipping companion. Python 3.9+."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_BODY = 8192
MAX_CLIP = 900
MAX_OUTPUT = 512 * 1024 * 1024
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
BUSY = threading.Lock()
RATE_LOCK = threading.Lock()
RECENT = deque()

class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def canonical_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise APIError(400, 'Invalid YouTube URL')
    try:
        u = urlsplit(value)
        host = (u.hostname or '').lower()
        port = u.port
    except ValueError:
        raise APIError(400, 'Invalid YouTube URL')
    if u.scheme != 'https' or u.username is not None or u.password is not None or port not in (None, 443):
        raise APIError(400, 'Use an HTTPS YouTube URL without credentials or a custom port')
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        if u.path == '/watch':
            ids = parse_qs(u.query, keep_blank_values=True).get('v', [])
            video = ids[0] if len(ids) == 1 else ''
        elif re.fullmatch(r'/(shorts|embed|live)/[A-Za-z0-9_-]{11}/?', u.path):
            video = u.path.strip('/').split('/')[1]
        else:
            video = ''
    elif host == 'youtu.be':
        video = u.path.strip('/') if re.fullmatch(r'/[A-Za-z0-9_-]{11}/?', u.path) else ''
    else:
        video = ''
    if not VIDEO_ID.fullmatch(video):
        raise APIError(400, 'Use a valid YouTube video link')
    # Discard all user-controlled parameters, fragments and host aliases.
    return 'https://www.youtube.com/watch?v=' + video

def validate(data):
    if not isinstance(data, dict):
        raise APIError(400, 'Expected a JSON object')
    url = canonical_url(data.get('url'))
    start, end = data.get('start'), data.get('end')
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or (isinstance(x, float) and not math.isfinite(x)) for x in (start, end)):
        raise APIError(400, 'start and end must be finite seconds')
    if start < 0 or start > 86400 or end <= start or end > 86400 or end - start > MAX_CLIP:
        raise APIError(400, 'Choose a valid range no longer than 900 seconds within the first 24 hours')
    if data.get('format') != 'mp4' or data.get('quality') not in ('720', '1080'):
        raise APIError(400, 'Only format mp4 and quality 720 or 1080 are supported')
    return url, float(start), float(end)

def run_command(command, timeout, cwd):
    # A process group allows timeout to terminate yt-dlp's own ffmpeg children.
    kw = {'cwd': cwd, 'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
          'stderr': subprocess.DEVNULL, 'shell': False}
    if os.name == 'nt':
        kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw['start_new_session'] = True
    try:
        proc = subprocess.Popen(command, **kw)
    except OSError:
        raise APIError(503, 'Required executable could not be started')
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        else:
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        proc.wait()
        raise APIError(504, 'Clip processing timed out')
    if code:
        raise APIError(502, 'Download or conversion failed; the video may be unavailable, restricted, or unsupported')

def make_clip(data, directory):
    url, start, end = validate(data)
    quality = int(data['quality'])
    ytdlp, ffmpeg = shutil.which('yt-dlp'), shutil.which('ffmpeg')
    if not ytdlp or not ffmpeg:
        raise APIError(503, 'Install yt-dlp and ffmpeg and add them to PATH')
    # Ignore user yt-dlp config/plugins so unrelated options cannot change the job.
    run_command([ytdlp, '--ignore-config', '--no-plugin-dirs', '--no-playlist', '--no-progress', '--quiet',
                 '--no-warnings', '--no-part', '--max-filesize', '256M', '--socket-timeout', '20',
                 '--retries', '2', '--fragment-retries', '2', '--force-keyframes-at-cuts', '--download-sections',
                 '*%.3f-%.3f' % (start, end), '-f', 'bv*+ba/b', '-S', 'res:%d' % quality,
                 '-o', str(Path(directory) / 'input.%(ext)s'), '--', url], 480, directory)
    inputs = [p for p in Path(directory).glob('input.*') if p.is_file()]
    if len(inputs) != 1:
        raise APIError(502, 'Could not obtain a single video file for that range')
    output = Path(directory) / 'clip.mp4'
    # Limit long and short edges according to orientation; never upscale.
    # yt-dlp's res sorting also uses the shorter edge, including portraits.
    long_edge = 1920 if quality == 1080 else 1280
    scale = ("scale=w='min(iw,if(gte(iw,ih),%d,%d))':"
             "h='min(ih,if(gte(iw,ih),%d,%d))':"
             "force_original_aspect_ratio=decrease:force_divisible_by=2"
             % (long_edge, quality, quality, long_edge))
    run_command([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(inputs[0]),
                 '-t', '%.3f' % (end-start), '-map', '0:v:0', '-map', '0:a:0?',
                 '-vf', scale, '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '23',
                 '-c:a', 'aac', '-b:a', '128k', '-movflags', '+faststart', str(output)], 600, directory)
    if not output.is_file() or not 0 < output.stat().st_size <= MAX_OUTPUT:
        raise APIError(502, 'Output is empty or exceeds the 512 MiB limit')
    return output

def rate_allowed():
    now = time.monotonic()
    with RATE_LOCK:
        while RECENT and RECENT[0] <= now - 60:
            RECENT.popleft()
        if len(RECENT) >= 6:
            return False
        RECENT.append(now)
        return True

class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    def __init__(self, address, origins):
        self.origins = set(origins)
        super().__init__(address, Handler)

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'SliceDeckLocal/1.0'
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def log_message(self, fmt, *args):
        pass  # Do not log video URLs.
    def origin_allowed(self):
        return len(self.headers.get_all('Origin', [])) == 1 and self.headers.get('Origin', '') in self.server.origins
    def host_allowed(self):
        port = self.server.server_address[1]
        return len(self.headers.get_all('Host', [])) == 1 and self.headers.get('Host', '').lower() in ('localhost:%s' % port, '127.0.0.1:%s' % port)
    def cors(self):
        self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.send_header('Vary', 'Origin')
        self.send_header('Cache-Control', 'no-store')
    def error_json(self, status, message):
        body = json.dumps({'error': message}).encode()
        self.send_response(status)
        if self.origin_allowed(): self.cors()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)
    def checked(self):
        if not self.host_allowed():
            raise APIError(403, 'Invalid Host')
        if not self.origin_allowed():
            raise APIError(403, 'Origin is not allowed')
    def do_OPTIONS(self):
        try:
            self.checked()
            if self.path not in ('/api/clip', '/api/health'):
                raise APIError(404, 'Not found')
            method = self.headers.get('Access-Control-Request-Method', '')
            if method not in ('GET', 'POST'):
                raise APIError(403, 'Method not allowed')
            headers = [x.strip().lower() for x in self.headers.get('Access-Control-Request-Headers', '').split(',') if x.strip()]
            if any(h != 'content-type' for h in headers):
                raise APIError(403, 'Header not allowed')
            self.send_response(204)
            self.cors()
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            if self.headers.get('Access-Control-Request-Private-Network', '').lower() == 'true':
                self.send_header('Access-Control-Allow-Private-Network', 'true')
            self.send_header('Content-Length', '0')
            self.end_headers()
        except APIError as e: self.error_json(e.status, e.message)
    def do_GET(self):
        try:
            self.checked()
            if self.path != '/api/health': raise APIError(404, 'Not found')
            body = json.dumps({'ok': True, 'yt_dlp': bool(shutil.which('yt-dlp')), 'ffmpeg': bool(shutil.which('ffmpeg'))}).encode()
            self.send_response(200); self.cors()
            self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
        except APIError as e: self.error_json(e.status, e.message)
    def do_POST(self):
        response_started = False
        try:
            self.checked()
            if self.path != '/api/clip': raise APIError(404, 'Not found')
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                raise APIError(400, 'A single Content-Length is required')
            length_text = self.headers.get('Content-Length', '')
            if not length_text.isascii() or not length_text.isdecimal(): raise APIError(400, 'Invalid Content-Length')
            length = int(length_text)
            if length <= 0 or length > MAX_BODY: raise APIError(413, 'Request body too large or empty')
            if self.headers.get_content_type() != 'application/json': raise APIError(415, 'Content-Type must be application/json')
            try:
                data = json.loads(self.rfile.read(length).decode('utf-8'))
            except (UnicodeError, ValueError): raise APIError(400, 'Invalid JSON')
            validate(data)
            if not rate_allowed(): raise APIError(429, 'Too many requests; wait one minute')
            if not BUSY.acquire(blocking=False): raise APIError(429, 'Another clip is already processing')
            try:
                with tempfile.TemporaryDirectory(prefix='slicedeck-') as directory:
                    path = make_clip(data, directory)
                    self.send_response(200); self.cors()
                    response_started = True
                    self.send_header('Content-Type', 'video/mp4')
                    self.send_header('Content-Disposition', 'attachment; filename="slicedeck-clip.mp4"')
                    self.send_header('Content-Length', str(path.stat().st_size))
                    self.end_headers()
                    with path.open('rb') as src:
                        shutil.copyfileobj(src, self.wfile, length=1024*1024)
            finally: BUSY.release()
        except APIError as e: self.error_json(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, TimeoutError): self.close_connection = True
        except Exception:
            if response_started:
                self.close_connection = True
            else:
                self.error_json(500, 'Unexpected processing error')

def parse_origin(value):
    try:
        u = urlsplit(value)
        port = u.port
    except ValueError:
        raise argparse.ArgumentTypeError('Invalid origin')
    if u.scheme not in ('http', 'https') or not u.hostname or u.username is not None or u.password is not None or u.path or u.query or u.fragment:
        raise argparse.ArgumentTypeError('Origin must look like https://NAME.github.io with no path')
    if u.scheme == 'http' and u.hostname not in ('localhost', '127.0.0.1'):
        raise argparse.ArgumentTypeError('Non-local origins must use HTTPS')
    if port is not None and not 1 <= port <= 65535: raise argparse.ArgumentTypeError('Invalid port')
    return value

def main():
    parser = argparse.ArgumentParser(description='SliceDeck optional local clip companion')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--origin', type=parse_origin, action='append', default=[], help='Exact allowed web origin; repeat to add more')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535: parser.error('Invalid port')
    origins = args.origin or ['http://localhost:8000', 'http://127.0.0.1:8000']
    server = Server(('127.0.0.1', args.port), origins)
    print('Listening on http://127.0.0.1:%s; allowed origins: %s' % (args.port, ', '.join(origins)), flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

if __name__ == '__main__': main()
