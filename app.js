'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const pages = [...document.querySelectorAll('[data-page]')];
  const nav = $('navigation'), menu = $('menu-toggle');
  function closeMenu() { nav.classList.remove('open'); menu.setAttribute('aria-expanded', 'false'); menu.setAttribute('aria-label', 'Open navigation'); }
  function route() {
    let hash = location.hash.slice(1).toLowerCase();
    const target = hash === 'workspace' ? 'home' : hash;
    const page = pages.some(p => p.dataset.page === target) ? target : 'home';
    pages.forEach(p => { p.hidden = p.dataset.page !== page; });
    document.querySelectorAll('[data-nav]').forEach(a => {
      const active = a.dataset.nav === page;
      a.classList.toggle('active', active);
      if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    closeMenu();
    if (hash === 'workspace') requestAnimationFrame(() => $('workspace').scrollIntoView());
    else window.scrollTo(0, 0);
  }
  menu.addEventListener('click', () => {
    const opening = !nav.classList.contains('open');
    nav.classList.toggle('open', opening);
    menu.setAttribute('aria-expanded', String(opening));
    menu.setAttribute('aria-label', opening ? 'Close navigation' : 'Open navigation');
  });
  nav.addEventListener('click', e => { if (e.target.closest('a')) closeMenu(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeMenu(); });
  document.addEventListener('click', e => { if (!nav.contains(e.target) && !menu.contains(e.target)) closeMenu(); });
  window.addEventListener('hashchange', route); route();

  function status(id, message, error = false) { const el = $(id); el.textContent = message; el.classList.toggle('error', error); }
  function parseTime(value) {
    const v = value.trim();
    if (/^\d+(?:\.\d+)?$/.test(v)) { const n = Number(v); return Number.isFinite(n) ? n : NaN; }
    if (!/^\d+:\d{2}(?::\d{2})?(?:\.\d+)?$/.test(v)) return NaN;
    const parts = v.split(':').map(Number);
    if (parts[parts.length - 1] >= 60 || (parts.length === 3 && parts[1] >= 60)) return NaN;
    const n = parts.reduce((total, part) => total * 60 + part, 0);
    return Number.isFinite(n) ? n : NaN;
  }
  function getRange(prefix, duration = Infinity) {
    const start = parseTime($(prefix + '-start').value), end = parseTime($(prefix + '-end').value);
    if (!Number.isFinite(start) || !Number.isFinite(end)) throw Error('Enter valid non-negative seconds, MM:SS, or HH:MM:SS (seconds and minutes below 60).');
    if (end <= start) throw Error('End time must be after start time.');
    if (end > duration || start >= duration) throw Error('The selected range must fit within the video duration.');
    return { start, end };
  }
  function formatTime(n) {
    n = Math.max(0, Number(n) || 0);
    const hours = Math.floor(n / 3600), minutes = Math.floor(n / 60) % 60;
    const sec = (n % 60).toFixed(3).replace(/\.?0+$/, '').padStart(2, '0');
    return hours ? `${hours}:${String(minutes).padStart(2,'0')}:${sec}` : `${minutes}:${sec}`;
  }
  function youtubeId(value) {
    let u; try { u = new URL(value.trim()); } catch { throw Error('Enter a full YouTube URL beginning with https://.'); }
    if (u.protocol !== 'https:' || u.username || u.password || u.port) throw Error('Use a standard HTTPS YouTube link.');
    const host = u.hostname.toLowerCase(); let id;
    if (host === 'youtu.be' || host === 'www.youtu.be') id = u.pathname.split('/').filter(Boolean)[0];
    else if (['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(host)) {
      const parts = u.pathname.split('/').filter(Boolean);
      if (u.pathname === '/watch') id = u.searchParams.get('v');
      else if (['shorts','embed','live'].includes(parts[0])) id = parts[1];
    }
    if (!id || !/^[a-zA-Z0-9_-]{11}$/.test(id)) throw Error('Enter a valid YouTube watch, Shorts, live, embed, or youtu.be video link.');
    return id;
  }
  let ytBlobUrl = null;
  function clearDownload(id, url) { if (url) URL.revokeObjectURL(url); const a = $(id); a.hidden = true; a.removeAttribute('href'); }
  function showYT(start = 0) {
    const id = youtubeId($('yt-url').value);
    const iframe = document.createElement('iframe');
    const url = new URL(`https://www.youtube-nocookie.com/embed/${id}`);
    if (start > 0) url.searchParams.set('start', String(Math.floor(start)));
    iframe.src = url.href; iframe.title = 'YouTube video preview';
    iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share';
    iframe.allowFullscreen = true; iframe.referrerPolicy = 'strict-origin-when-cross-origin';
    $('yt-frame').replaceChildren(iframe);
  }
  $('load-yt').addEventListener('click', () => { try { showYT(); status('yt-status', 'Preview loaded. Some videos may not allow embedding.'); } catch(e) { status('yt-status', e.message, true); } });
  $('seek-yt').addEventListener('click', () => { try { const {start} = getRange('yt'); showYT(start); status('yt-status', 'Preview begins at the nearest whole second at or before your start. Press play; export uses the exact entered times.'); } catch(e) { status('yt-status', e.message, true); } });
  $('export-yt').addEventListener('click', async () => {
    const button = $('export-yt');
    try {
      youtubeId($('yt-url').value);
      const {start, end} = getRange('yt');
      let backend; try { backend = new URL($('backend-url').value.trim()); } catch { throw Error('Enter the URL of your running, compatible backend.'); }
      if (!['http:', 'https:'].includes(backend.protocol) || backend.username || backend.password || backend.search || backend.hash) throw Error('Use an HTTP(S) backend URL without credentials, query or fragment.');
      const endpoint = new URL(backend.href.replace(/\/$/, '') + '/api/clip');
      clearDownload('yt-download', ytBlobUrl); ytBlobUrl = null;
      button.disabled = true;
      status('yt-status', 'Contacting your backend and preparing the clip. This may take a while.');
      const response = await fetch(endpoint, { method: 'POST', headers: {'Content-Type':'application/json', 'Accept':'video/mp4, application/json'}, body: JSON.stringify({url:$('yt-url').value.trim(), start, end, format:'mp4', quality:'720'}) });
      if (!response.ok) {
        let details = ''; try { const json = await response.json(); details = typeof json.error === 'string' ? json.error : typeof json.message === 'string' ? json.message : ''; } catch {}
        throw Error(`Backend returned HTTP ${response.status}${details ? ': ' + details.slice(0, 300) : '.'}`);
      }
      const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
      if (!['video/mp4','application/octet-stream'].includes(type)) throw Error('Backend did not return an MP4 file. Check the backend response and configuration.');
      const blob = await response.blob();
      if (!blob.size) throw Error('Backend returned an empty file.');
      ytBlobUrl = URL.createObjectURL(blob); $('yt-download').href = ytBlobUrl; $('yt-download').hidden = false;
      status('yt-status', 'Clip received. Tap Download MP4 to save it.');
    } catch(e) { status('yt-status', e instanceof TypeError ? 'Could not reach the backend. Check that it is running on this device or reachable, permits CORS, and uses HTTPS if required.' : e.message, true); }
    finally { button.disabled = false; }
  });

  const video = $('local-video'), fileInput = $('local-file'), scrub = $('local-scrub');
  let sourceUrl = null, localBlobUrl = null, session = null, sourceVersion = 0;
  const localControls = ['local-file','local-start','local-end','set-start','set-end','local-scrub'];
  function lockLocal(locked) {
    localControls.forEach(id => { $(id).disabled = locked || (id !== 'local-file' && !Number.isFinite(video.duration)); });
    $('export-local').disabled = locked || !Number.isFinite(video.duration);
    $('cancel-local').hidden = !locked;
    video.controls = !locked;
  }
  function updatePlayhead() { scrub.value = String(video.currentTime || 0); $('playhead').textContent = formatTime(video.currentTime); }
  function rangeDuration() {
    const s = parseTime($('local-start').value), e = parseTime($('local-end').value);
    $('range-duration').textContent = Number.isFinite(s) && Number.isFinite(e) && e > s ? `Selection: ${formatTime(e-s)}` : 'Selection: —';
  }
  fileInput.addEventListener('change', () => {
    const f = fileInput.files[0]; if (!f) return;
    sourceVersion++;
    if (session) session.abort('Recording cancelled because the source changed.');
    clearDownload('local-download', localBlobUrl); localBlobUrl = null;
    video.pause(); video.removeAttribute('src'); video.load();
    if (sourceUrl) URL.revokeObjectURL(sourceUrl);
    sourceUrl = URL.createObjectURL(f); video.src = sourceUrl; video.hidden = false;
    $('file-name').textContent = f.name + ' — stays in this browser.';
    $('video-duration').textContent = 'LOADING VIDEO'; scrub.max = '0'; scrub.value = '0';
    $('local-start').value = '00:00'; $('local-end').value = '00:00'; updatePlayhead(); rangeDuration(); lockLocal(false);
    status('local-status', 'Loading video metadata…');
    video.load();
  });
  video.addEventListener('loadedmetadata', () => {
    if (!Number.isFinite(video.duration) || video.duration <= 0) { status('local-status', 'This file has no usable duration. Try another video.', true); lockLocal(false); return; }
    scrub.max = String(video.duration); $('video-duration').textContent = 'DURATION ' + formatTime(video.duration);
    $('local-start').value = '00:00'; $('local-end').value = formatTime(Math.min(video.duration, 30));
    rangeDuration(); updatePlayhead(); lockLocal(false);
    status('local-status', 'Video ready. Select a range of up to 120 seconds.');
  });
  video.addEventListener('error', () => { if (video.getAttribute('src')) { status('local-status', 'Unable to load this video. Try another file or browser.', true); lockLocal(false); } });
  video.addEventListener('timeupdate', updatePlayhead);
  scrub.addEventListener('input', () => { if (Number.isFinite(video.duration)) video.currentTime = Number(scrub.value); });
  $('set-start').addEventListener('click', () => { $('local-start').value = formatTime(video.currentTime); rangeDuration(); });
  $('set-end').addEventListener('click', () => { $('local-end').value = formatTime(video.currentTime); rangeDuration(); });
  $('local-start').addEventListener('input', rangeDuration); $('local-end').addEventListener('input', rangeDuration);
  // wait for a seek to complete before capturing, with a timeout and cancellation.
  function seekTo(time, signal) {
    return new Promise((resolve, reject) => {
      let timer;
      const finish = err => { clearTimeout(timer); video.removeEventListener('seeked', onSeek); video.removeEventListener('error', onError); signal.removeEventListener('abort', onAbort); err ? reject(err) : resolve(); };
      const onSeek = () => finish(), onError = () => finish(Error('Unable to seek in this file.'));
      const onAbort = () => finish(Error('Recording cancelled.'));
      if (signal.aborted) return onAbort();
      video.addEventListener('seeked', onSeek, {once:true}); video.addEventListener('error', onError, {once:true}); signal.addEventListener('abort', onAbort, {once:true});
      timer = setTimeout(() => finish(Error('Seeking timed out. Try a different video.')), 10000);
      try { if (Math.abs(video.currentTime - time) < 0.005 && video.readyState >= 2) finish(); else video.currentTime = time; } catch { finish(Error('Unable to seek in this file.')); }
    });
  }
  $('export-local').addEventListener('click', async () => {
    let stream = null, sourceStream = null, recorder = null, monitor = null, timeout = null, frame = null, cleanupEvents = () => {};
    let controller = null;
    try {
      if (session) return;
      if (!sourceUrl || !Number.isFinite(video.duration) || video.duration <= 0) throw Error('Choose a video and wait for it to load first.');
      const {start, end} = getRange('local', video.duration);
      if (end - start > 120) throw Error('For reliability, select no more than 120 seconds. Recording takes the full selected duration.');
      const capture = video.captureStream || video.mozCaptureStream;
      if (typeof capture !== 'function' || typeof MediaRecorder === 'undefined') throw Error('This browser cannot record video to WebM. Try a recent Chromium browser; Safari support is limited.');
      const candidates = ['video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus','video/webm'];
      const mime = candidates.find(t => typeof MediaRecorder.isTypeSupported === 'function' && MediaRecorder.isTypeSupported(t));
      if (!mime) throw Error('This browser does not support WebM recording. Try a recent Chromium browser; Safari support is limited.');
      clearDownload('local-download', localBlobUrl); localBlobUrl = null;
      controller = new AbortController(); session = controller; const signal = controller.signal, version = sourceVersion;
      lockLocal(true); video.pause();
      status('local-status', `Preparing to record ${formatTime(end-start)} in real time. Keep this tab open and playback uninterrupted.`);
      await seekTo(start, signal);
      if (signal.aborted || version !== sourceVersion) throw Error('Recording cancelled.');
      // Capture canvas frames rather than the video element's sparse Chromium video track.
      const canvas = document.createElement('canvas');
      canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      if (!canvas.width || !canvas.height || typeof canvas.captureStream !== 'function') throw Error('This browser could not capture the video frames.');
      const context = canvas.getContext('2d');
      if (!context) throw Error('Could not prepare the video renderer.');
      const draw = () => { context.drawImage(video, 0, 0, canvas.width, canvas.height); frame = requestAnimationFrame(draw); };
      draw();
      sourceStream = capture.call(video);
      const audioTracks = sourceStream.getAudioTracks();
      const canvasStream = canvas.captureStream(30);
      stream = new MediaStream([...canvasStream.getVideoTracks(), ...audioTracks]);
      if (!stream.getVideoTracks().length) throw Error('This browser could not capture the video track.');
      const audioPresent = audioTracks.length > 0;
      recorder = new MediaRecorder(stream, {mimeType:mime});
      const chunks = [];
      const result = new Promise((resolve, reject) => {
        let failure = null, stopping = false, played = false, lastProgress = performance.now(), previous = start;
        const fail = message => { failure = message; stop(); };
        const stop = () => { if (stopping) return; stopping = true; if (recorder.state !== 'inactive') { try { recorder.stop(); } catch { reject(Error(failure || 'Could not stop recording.')); } } else reject(Error(failure || 'Recording did not start.')); };
        const onAbort = () => fail('Recording cancelled.');
        const onPause = () => { if (played && video.currentTime < end - 0.05) fail('Playback was interrupted. Try recording again.'); };
        const onSeek = () => { if (played) fail('Playback was interrupted by a seek. Try again.'); };
        const onEnded = () => { if (video.currentTime >= end - 0.12) stop(); else fail('The video ended before the selected end.'); };
        const onError = () => fail('Playback failed during recording.');
        const onHidden = () => { if (document.hidden) fail('The tab was hidden during recording. Keep it visible and try again.'); };
        signal.addEventListener('abort', onAbort); video.addEventListener('pause', onPause); video.addEventListener('seeking', onSeek); video.addEventListener('ended', onEnded); video.addEventListener('error', onError); document.addEventListener('visibilitychange', onHidden);
        cleanupEvents = () => { signal.removeEventListener('abort', onAbort); video.removeEventListener('pause', onPause); video.removeEventListener('seeking', onSeek); video.removeEventListener('ended', onEnded); video.removeEventListener('error', onError); document.removeEventListener('visibilitychange', onHidden); };
        recorder.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
        recorder.onerror = () => { failure = 'Recording failed on this device.'; if (recorder.state !== 'inactive') stop(); else reject(Error(failure)); };
        recorder.onstop = () => { if (failure) reject(Error(failure)); else { const blob = new Blob(chunks, {type:'video/webm'}); blob.size ? resolve(blob) : reject(Error('Recording produced an empty file.')); } };
        try { recorder.start(500); } catch { reject(Error('Could not start the WebM recorder.')); return; }
        video.play().then(() => { played = true; lastProgress = performance.now(); }).catch(() => fail('Playback could not start. Try again or use a different browser.'));
        monitor = setInterval(() => {
          if (video.currentTime >= end) return stop();
          if (video.currentTime > previous + 0.01) { previous = video.currentTime; lastProgress = performance.now(); }
          if (performance.now() - lastProgress > 10000) fail('Playback stalled. Try again or use a different file.');
        }, 50);
        timeout = setTimeout(() => fail('Recording timed out. Try again with a shorter range.'), (end-start+20)*1000);
      });
      const blob = await result;
      if (signal.aborted || version !== sourceVersion) throw Error('Recording cancelled.');
      localBlobUrl = URL.createObjectURL(blob); $('local-download').href = localBlobUrl; $('local-download').hidden = false;
      status('local-status', `Recording ready. Tap Download WebM. ${audioPresent ? 'An audio track was captured if the source provided one.' : 'No audio track was captured; this clip is silent.'} Timing is approximate.`);
    } catch(e) { status('local-status', e.message || 'Recording failed.', true); }
    finally {
      clearInterval(monitor); clearTimeout(timeout); cleanupEvents();
      if (frame !== null) cancelAnimationFrame(frame);
      if (recorder && recorder.state !== 'inactive') { try { recorder.stop(); } catch {} }
      if (stream) stream.getTracks().forEach(t => t.stop());
      if (sourceStream) sourceStream.getTracks().forEach(t => t.stop());
      if (controller) { video.pause(); if (session === controller) session = null; }
      lockLocal(false);
    }
  });
  $('cancel-local').addEventListener('click', () => { if (session) session.abort(); });
  window.addEventListener('pagehide', () => { if (session) session.abort(); if (sourceUrl) URL.revokeObjectURL(sourceUrl); if (localBlobUrl) URL.revokeObjectURL(localBlobUrl); if (ytBlobUrl) URL.revokeObjectURL(ytBlobUrl); });
  lockLocal(false); rangeDuration();
})();
