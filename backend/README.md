# SliceDeck optional local clipping companion

GitHub Pages serves the interface only: it cannot run Python, yt-dlp, or FFmpeg. This optional program runs on **your own computer**, listens only on `127.0.0.1`, and serves the interface at `https://1animepahe-svg.github.io` when explicitly allowed. It is not hosted by GitHub Pages, a public proxy, or a cloud service. Keep the server running while using the feature. A phone browsing the site cannot reach a companion running on a separate computer via `127.0.0.1`: that address means the phone itself. There is no supported LAN or public deployment. Browsers, extensions, network policy, or private-network restrictions may block the connection; phone and Safari behavior is not verified.

## Install and start

Work in this `backend` directory from a local copy of the project. Install Python 3.10+ and FFmpeg (with the `libx264` and `aac` encoders), and ensure `ffmpeg` is on PATH. For full current YouTube support, yt-dlp also recommends Deno (2.3 or later) on PATH and its EJS scripts; the `yt-dlp[default]` install below includes the scripts. Deno is automatically selected by yt-dlp if available. See https://github.com/yt-dlp/yt-dlp/wiki/EJS and https://ffmpeg.org/download.html. Extraction might work without Deno, but some videos/formats may not.

**macOS/Linux** (use your package manager or the official FFmpeg and Deno instructions first):

```sh
cd path/to/slicedeck/backend
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -U 'yt-dlp[default]'
ffmpeg -version
yt-dlp --version
python server.py --origin https://1animepahe-svg.github.io
```

**Windows PowerShell** (install Python, FFmpeg and optionally Deno first; reopen PowerShell so PATH updates):

```powershell
cd path\to\slicedeck\backend
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U "yt-dlp[default]"
ffmpeg -version
yt-dlp --version
python server.py --origin https://1animepahe-svg.github.io
```

If PowerShell blocks activation, follow your organization's policy or use Command Prompt and `.venv\Scripts\activate.bat`. Start the frontend in a browser **on the same machine**, and enter `http://127.0.0.1:8765` as the backend address. Stop the server with Ctrl+C. To upgrade the downloader later, activate the environment and rerun the pip command. If a current version of yt-dlp changes its command-line options, reinstall/update it; the tested version was 2026.10.26.

## API and behavior

`POST http://127.0.0.1:8765/api/clip`, with `Origin: https://1animepahe-svg.github.io` and `Content-Type: application/json`:

```json
{"url":"https://www.youtube.com/watch?v=eRsGyueVLvQ","start":5,"end":7,"format":"mp4","quality":"720"}
```

`start` and `end` are numeric seconds. Successful requests return `video/mp4`; errors before streaming return a JSON object with an `error` message and an HTTP error status. A broken connection during streaming may produce a truncated response instead. `GET /api/health` requires the same allowed Origin and reports whether the `yt-dlp` and `ffmpeg` executables are on PATH; it does not check encoder availability, network access or whether YouTube extraction will succeed. Allowed CORS preflight also handles the private-network request header where supported.

The companion accepts only recognized HTTPS YouTube video URLs, extracts an 11-character video ID, and discards all query parameters other than that ID. It requests a section using yt-dlp, then encodes H.264/AAC MP4, fitting within 1280×720 without upscaling. Actual resolution may be lower; 720 means a maximum bounding box, not a guarantee. Ranges must be within the first 24 hours and at most 900 seconds. It handles one job at a time and at most six validated requests per minute. Output is rejected above 512 MiB. yt-dlp's 256 MB source limit is best-effort, not a hard disk/network quota: unknown-size streams, temporary files and re-encoding may consume substantially more disk and bandwidth before limits are checked. Jobs have timeouts; temporary files are normally deleted, but forced termination or a crash can leave them behind. Accuracy depends on source and downloader behavior. Private, age-restricted, live, unavailable or changed YouTube formats may fail; no login, cookies, bypass, queue or background processing is provided.

Only download or clip videos you own or are authorized to use, and follow YouTube's terms and applicable law. YouTube may change or block extraction even for public videos.

## Security and testing

`--origin` is an exact origin (no trailing slash or path), and may be repeated for additional trusted sites. Without it, only `http://localhost:8000` and `http://127.0.0.1:8000` are allowed. The GitHub Pages origin covers **every page under that origin**, so only allow origins you trust. CORS and Host checks help restrict ordinary browser access; they are not authentication and local programs can forge headers. Do not bind or forward this server to a network, expose it through a tunnel, or allow untrusted origins. Requests use no shell, and downloader config/plugins are disabled. Source retrieval still contacts YouTube and its delivery networks. No claim of a security audit or guaranteed protection from compromised local software is made.

From this directory, run `python -m unittest -v test_server`. The tests cover validation, allowed and rejected origins, limits, HTTP responses, and a real FFmpeg encode using a generated local input; YouTube retrieval requires a separate network test.
