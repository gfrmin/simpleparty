#!/usr/bin/env python3
"""SimpleParty - Video directory browser with shuffle and fscrypt support."""

import argparse
import json
import os
import re
import shutil
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

BROWSER_NATIVE = frozenset({'.mp4', '.webm', '.ogv', '.m4v'})

HAS_FFMPEG = shutil.which('ffmpeg') is not None
HAS_VLC = shutil.which('cvlc') is not None

MIME_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.m4v': 'video/mp4',
    '.ogv': 'video/ogg',
}


# --- Pure functions: filesystem ---

def is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def safe_resolve(root, relative):
    root_resolved = Path(root).resolve()
    resolved = (root_resolved / relative).resolve()
    root_str = str(root_resolved)
    resolved_str = str(resolved)
    if resolved_str != root_str and not resolved_str.startswith(root_str + os.sep):
        return None
    return resolved


def list_directory(root, rel_path):
    resolved = safe_resolve(root, rel_path)
    if resolved is None:
        return {'error': 'Invalid path'}

    if not resolved.exists():
        locked = find_locked_ancestor(root, rel_path)
        if locked is not None:
            return {'locked': True, 'path': rel_path, 'encryptedDir': locked}
        return {'error': 'Not found'}

    if not resolved.is_dir():
        return {'error': 'Not a directory'}

    status = get_fscrypt_status(resolved)
    if status['encrypted'] and not status['unlocked']:
        return {'locked': True, 'path': rel_path, 'encryptedDir': rel_path}

    try:
        entries = sorted(os.listdir(resolved))
    except (PermissionError, OSError):
        return {'error': 'Cannot read directory'}

    encrypted_root = find_encrypted_ancestor(root, rel_path)

    dirs = []
    videos = []
    for name in entries:
        if name.startswith('.'):
            continue
        full = resolved / name
        if full.is_dir():
            dir_status = get_fscrypt_status(full)
            dirs.append({
                'name': name,
                'path': os.path.join(rel_path, name) if rel_path else name,
                'encrypted': dir_status['encrypted'],
                'unlocked': dir_status['unlocked'],
            })
        elif full.is_file() and is_video(name):
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            videos.append({
                'name': name,
                'path': os.path.join(rel_path, name) if rel_path else name,
                'size': size,
            })

    return {
        'path': rel_path,
        'dirs': dirs,
        'videos': videos,
        'encryptedDir': encrypted_root,
    }


# --- Pure functions: fscrypt ---

def get_fscrypt_status(dir_path):
    try:
        result = subprocess.run(
            ['fscrypt', 'status', str(dir_path)],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout + result.stderr
        if 'is encrypted with fscrypt' not in output:
            return {'encrypted': False, 'unlocked': True}
        unlocked = bool(re.search(r'Unlocked:\s*Yes', output))
        return {'encrypted': True, 'unlocked': unlocked}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return {'encrypted': False, 'unlocked': True}


def fscrypt_unlock(dir_path, passphrase):
    try:
        proc = subprocess.Popen(
            ['fscrypt', 'unlock', str(dir_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(
            input=(passphrase + '\n').encode('utf-8'),
            timeout=10,
        )
        return proc.returncode == 0, (stdout.decode() + stderr.decode()).strip()
    except subprocess.TimeoutExpired:
        proc.kill()  # noqa: F821 - proc is always bound when TimeoutExpired fires
        return False, 'Timed out'
    except FileNotFoundError:
        return False, 'fscrypt not found'


def fscrypt_lock(dir_path):
    try:
        result = subprocess.run(
            ['fscrypt', 'lock', str(dir_path)],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def find_encrypted_ancestor(root, rel_path):
    if not rel_path or rel_path == '.':
        status = get_fscrypt_status(Path(root))
        return '' if status['encrypted'] else None
    parts = Path(rel_path).parts
    for i in range(len(parts)):
        ancestor_rel = os.path.join(*parts[:i + 1])
        ancestor_abs = Path(root) / ancestor_rel
        if ancestor_abs.is_dir():
            status = get_fscrypt_status(ancestor_abs)
            if status['encrypted']:
                return ancestor_rel
    return None


def find_locked_ancestor(root, rel_path):
    if not rel_path or rel_path == '.':
        status = get_fscrypt_status(Path(root))
        if status['encrypted'] and not status['unlocked']:
            return ''
        return None
    parts = Path(rel_path).parts
    for i in range(len(parts)):
        ancestor_rel = os.path.join(*parts[:i + 1])
        ancestor_abs = Path(root) / ancestor_rel
        if ancestor_abs.is_dir():
            status = get_fscrypt_status(ancestor_abs)
            if status['encrypted'] and not status['unlocked']:
                return ancestor_rel
    return None


# --- HTTP helpers ---

def send_json(handler, data, status=200):
    body = json.dumps(data).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'application/json')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def read_json_body(handler):
    length = int(handler.headers.get('Content-Length', 0))
    return json.loads(handler.rfile.read(length))


# --- Route handlers ---

def serve_browse(handler, root):
    parsed = urllib.parse.urlparse(handler.path)
    params = urllib.parse.parse_qs(parsed.query)
    rel_path = params.get('path', [''])[0]
    result = list_directory(root, rel_path)
    status = 403 if result.get('locked') else 400 if 'error' in result else 200
    send_json(handler, result, status)


def serve_unlock(handler, root):
    body = read_json_body(handler)
    rel_path = body.get('path', '')
    passphrase = body.get('passphrase', '')
    resolved = safe_resolve(root, rel_path)
    if resolved is None:
        send_json(handler, {'ok': False, 'error': 'Invalid path'}, 400)
        return
    ok, msg = fscrypt_unlock(resolved, passphrase)
    del passphrase, body
    send_json(handler, {'ok': ok, 'error': msg if not ok else None})


def serve_lock(handler, root):
    body = read_json_body(handler)
    rel_path = body.get('path', '')
    resolved = safe_resolve(root, rel_path)
    if resolved is None:
        send_json(handler, {'ok': False, 'error': 'Invalid path'}, 400)
        return
    ok, msg = fscrypt_lock(resolved)
    send_json(handler, {'ok': ok, 'error': msg if not ok else None})


def serve_delete(handler, root):
    body = read_json_body(handler)
    rel_path = body.get('path', '')
    resolved = safe_resolve(root, rel_path)
    if resolved is None or not resolved.is_file() or not is_video(resolved.name):
        send_json(handler, {'ok': False, 'error': 'Invalid video path'}, 400)
        return
    try:
        os.remove(resolved)
        send_json(handler, {'ok': True})
    except OSError as e:
        send_json(handler, {'ok': False, 'error': str(e)}, 500)


def _needs_transcode(path):
    return path.suffix.lower() not in BROWSER_NATIVE


def serve_video(handler, root):
    parsed = urllib.parse.urlparse(handler.path)
    rel_path = urllib.parse.unquote(parsed.path[len('/video/'):])
    resolved = safe_resolve(root, rel_path)

    if resolved is None or not resolved.is_file():
        handler.send_error(404)
        return

    # Transcode non-browser-native formats if possible
    if _needs_transcode(resolved) and (HAS_FFMPEG or HAS_VLC):
        _serve_transcoded(handler, resolved)
        return

    file_size = resolved.stat().st_size
    content_type = MIME_TYPES.get(resolved.suffix.lower(), 'application/octet-stream')
    range_header = handler.headers.get('Range')

    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{file_size}')
                handler.end_headers()
                return
            length = end - start + 1
            handler.send_response(206)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            handler.send_header('Content-Length', str(length))
            handler.send_header('Accept-Ranges', 'bytes')
            handler.end_headers()
            if handler.command != 'HEAD':
                _stream_range(handler, resolved, start, length)
            return

    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(file_size))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.end_headers()
    if handler.command != 'HEAD':
        _stream_file(handler, resolved)


def _serve_transcoded(handler, path):
    if HAS_FFMPEG:
        cmd = [
            'ffmpeg', '-i', str(path), '-c:v', 'copy', '-c:a', 'aac',
            '-movflags', 'frag_keyframe+empty_moov',
            '-f', 'mp4', '-loglevel', 'error', 'pipe:1',
        ]
    else:
        cmd = [
            'cvlc', str(path),
            '--sout', '#transcode{acodec=mpga}:std{access=file,mux=mp4,dst=-}',
            'vlc://quit', '--no-repeat', '--no-loop',
        ]

    handler.send_response(200)
    handler.send_header('Content-Type', 'video/mp4')
    handler.send_header('Transfer-Encoding', 'chunked')
    handler.end_headers()

    if handler.command == 'HEAD':
        return

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            handler.wfile.write(f'{len(chunk):x}\r\n'.encode())
            handler.wfile.write(chunk)
            handler.wfile.write(b'\r\n')
        handler.wfile.write(b'0\r\n\r\n')
        proc.wait()
    except BrokenPipeError:
        proc.kill()
    except Exception:
        if proc.poll() is None:
            proc.kill()


def _stream_range(handler, path, start, length):
    try:
        with open(path, 'rb') as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except BrokenPipeError:
        pass


def _stream_file(handler, path):
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except BrokenPipeError:
        pass


# --- Request handler ---

class RequestHandler(BaseHTTPRequestHandler):
    def __init__(self, root, *args, **kwargs):
        self.root = root
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/':
            body = SPA_HTML.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path.startswith('/api/browse'):
            serve_browse(self, self.root)
        elif path.startswith('/video/'):
            serve_video(self, self.root)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/api/unlock':
            serve_unlock(self, self.root)
        elif path == '/api/lock':
            serve_lock(self, self.root)
        elif path == '/api/delete':
            serve_delete(self, self.root)
        else:
            self.send_error(404)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/video/'):
            serve_video(self, self.root)
        else:
            self.do_GET()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# --- SPA HTML ---
# Note: All user-provided data (filenames, paths) is escaped via esc() which
# handles &, <, >, ", and ' before insertion into the DOM. The only unescaped
# HTML consists of hardcoded icon entities and structural markup.

SPA_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>SimpleParty</title>
<style>
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#0f0f1a;color:#e2e8f0;min-height:100%;overflow-x:hidden;max-width:100vw;
}
nav{
  position:sticky;top:0;background:#1a1a2e;padding:12px 16px;
  display:flex;align-items:center;gap:4px;
  border-bottom:1px solid #2d2d44;z-index:10;flex-wrap:wrap;min-height:48px;
  overflow:hidden;max-width:100vw;
}
.crumb{
  color:#94a3b8;cursor:pointer;padding:4px 6px;border-radius:4px;
  font-size:15px;white-space:nowrap;
}
.crumb:hover{color:#c4b5fd;background:rgba(167,139,250,0.1)}
.crumb-sep{color:#4a4a6a;padding:0 2px;user-select:none}
.nav-spacer{flex:1}

.btn{
  background:#16213e;color:#e2e8f0;border:1px solid #2d2d44;
  padding:8px 14px;border-radius:6px;cursor:pointer;font-size:14px;
  min-height:40px;white-space:nowrap;transition:all .15s;
}
.btn:hover{background:#1e3054;border-color:#a78bfa}
.btn.active{background:#7c3aed;color:#fff;border-color:#7c3aed}
.btn-lock{border-color:#991b1b}
.btn-lock:hover{background:#7f1d1d;border-color:#dc2626}

#player-area{display:none;background:#000}
#player-area.visible{display:block}
video{width:100%;max-height:70vh;display:block;background:#000}
#controls{
  display:flex;align-items:center;padding:8px 16px;gap:8px;
  background:#1a1a2e;border-bottom:1px solid #2d2d44;
}
#now-playing{
  flex:1;text-align:center;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:#94a3b8;font-size:13px;padding:0 8px;
}

#file-list{
  padding:16px;
  display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px;
}
.item{
  display:flex;align-items:center;gap:10px;padding:12px 14px;
  background:#16213e;border-radius:8px;cursor:pointer;min-height:48px;
  transition:background .15s;border:2px solid transparent;min-width:0;overflow:hidden;
}
.item:hover{background:#1e3054}
.item.playing{border-color:#7c3aed}
.item-icon{font-size:18px;flex-shrink:0;line-height:1}
.item-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;flex:1}
.item-size{color:#64748b;font-size:12px;flex-shrink:0}
.empty{grid-column:1/-1;color:#64748b;text-align:center;padding:40px 20px;font-size:15px}
.action-bar{grid-column:1/-1;display:flex;gap:8px;padding-bottom:4px}

.modal{
  display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);
  align-items:center;justify-content:center;z-index:100;
}
.modal.visible{display:flex}
.modal-box{
  background:#1a1a2e;border:1px solid #2d2d44;border-radius:12px;
  padding:24px;width:90%;max-width:380px;
}
.modal-box h3{margin-bottom:16px;font-size:18px}
.modal-box input[type="password"]{
  width:100%;padding:12px;background:#0f0f1a;border:1px solid #2d2d44;
  border-radius:6px;color:#e2e8f0;font-size:16px;outline:none;
}
.modal-box input:focus{border-color:#7c3aed}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.modal-error{color:#f87171;font-size:13px;margin-top:10px;min-height:1.2em}

.loading{opacity:.5;pointer-events:none}

#shortcuts{
  position:fixed;bottom:12px;right:12px;background:#1a1a2e;
  border:1px solid #2d2d44;border-radius:8px;padding:10px 14px;
  font-size:12px;color:#64748b;line-height:1.6;
  opacity:0;transition:opacity .2s;pointer-events:none;
}
#shortcuts.visible{opacity:1}
kbd{
  background:#0f0f1a;border:1px solid #2d2d44;border-radius:3px;
  padding:1px 5px;font-family:monospace;font-size:11px;
}

@media(max-width:640px){
  #file-list{grid-template-columns:1fr;padding:8px;gap:6px}
  nav{padding:8px 12px}
  #controls{padding:6px 12px;flex-wrap:wrap;justify-content:center}
}
</style>
</head>
<body>
<nav id="nav"></nav>
<div id="player-area">
  <video id="video" controls playsinline></video>
  <div id="controls">
    <button class="btn" id="prev-btn" title="Previous (p)">&#9664; Prev</button>
    <span id="now-playing"></span>
    <button class="btn" id="next-btn" title="Next (n)">Next &#9654;</button>
    <button class="btn" id="shuffle-btn" title="Shuffle (s)">&#8645; Shuffle</button>
    <button class="btn btn-lock" id="delete-btn" title="Delete (d)">&#128465;</button>
  </div>
</div>
<div id="file-list"></div>

<div class="modal" id="modal">
  <div class="modal-box">
    <h3 id="modal-title">Unlock Directory</h3>
    <input type="password" id="modal-pass" placeholder="Passphrase" autocomplete="off">
    <div class="modal-error" id="modal-error"></div>
    <div class="modal-actions">
      <button class="btn" id="modal-cancel">Cancel</button>
      <button class="btn active" id="modal-submit">Unlock</button>
    </div>
  </div>
</div>

<div id="shortcuts">
  <kbd>n</kbd> next &middot; <kbd>p</kbd> prev &middot; <kbd>s</kbd> shuffle<br>
  <kbd>f</kbd> fullscreen &middot; <kbd>space</kbd> play/pause &middot; <kbd>esc</kbd> back<br>
  <kbd>?</kbd> toggle this help
</div>

<script>
'use strict';

let state = {
  path: '', dirs: [], videos: [], encryptedDir: null,
  currentVideo: -1, playlist: [], playlistPos: -1,
  shuffled: false, loading: false,
  modalPath: null, modalError: null, pendingPath: null,
};

const $ = id => document.getElementById(id);
const video = $('video');

// All user-provided strings (filenames, paths) pass through esc() before
// DOM insertion, preventing script injection from crafted filenames.
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function fmtSize(b) {
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b / 1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1073741824).toFixed(1) + ' GB';
}

function videoUrl(path) {
  return '/video/' + path.split('/').map(encodeURIComponent).join('/');
}

function fisherYates(arr) {
  const a = [...arr];
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function update(changes) {
  Object.assign(state, changes);
  render();
}

// --- Navigation ---

async function navigateTo(path) {
  update({ loading: true });
  try {
    const resp = await fetch('/api/browse?path=' + encodeURIComponent(path));
    const data = await resp.json();

    if (data.locked) {
      update({ loading: false, modalPath: data.encryptedDir, modalError: null, pendingPath: path });
      return;
    }
    if (data.error) {
      update({ loading: false });
      return;
    }

    video.pause();
    video.removeAttribute('src');

    const playlist = data.videos.map((_, i) => i);
    update({
      path: data.path || '', dirs: data.dirs || [], videos: data.videos || [],
      encryptedDir: data.encryptedDir, currentVideo: -1,
      playlist, playlistPos: -1, shuffled: false,
      loading: false, modalPath: null, pendingPath: null,
    });

    history.replaceState(null, '', '#' + (data.path || ''));
    document.title = data.path ? 'SimpleParty - ' + data.path.split('/').pop() : 'SimpleParty';
    window.scrollTo(0, 0);
  } catch (e) {
    update({ loading: false });
  }
}

function selectVideo(index) {
  const pos = state.playlist.indexOf(index);
  update({ currentVideo: index, playlistPos: pos >= 0 ? pos : 0 });
  video.src = videoUrl(state.videos[index].path);
  video.play();
  $('player-area').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function nextVideo() {
  if (!state.playlist.length) return;
  let pos = state.playlistPos + 1;
  if (pos >= state.playlist.length) pos = 0;
  const idx = state.playlist[pos];
  update({ currentVideo: idx, playlistPos: pos });
  video.src = videoUrl(state.videos[idx].path);
  video.play();
}

function prevVideo() {
  if (!state.playlist.length) return;
  let pos = state.playlistPos - 1;
  if (pos < 0) pos = state.playlist.length - 1;
  const idx = state.playlist[pos];
  update({ currentVideo: idx, playlistPos: pos });
  video.src = videoUrl(state.videos[idx].path);
  video.play();
}

function toggleShuffle() {
  if (!state.videos.length) return;
  if (state.shuffled) {
    const playlist = state.videos.map((_, i) => i);
    const pos = state.currentVideo >= 0 ? state.currentVideo : -1;
    update({ shuffled: false, playlist, playlistPos: pos });
  } else {
    let playlist = fisherYates(state.videos.map((_, i) => i));
    if (state.currentVideo >= 0) {
      const idx = playlist.indexOf(state.currentVideo);
      if (idx > 0) [playlist[0], playlist[idx]] = [playlist[idx], playlist[0]];
    }
    update({ shuffled: true, playlist, playlistPos: state.currentVideo >= 0 ? 0 : -1 });
  }
}

function shufflePlay() {
  if (!state.videos.length) return;
  const playlist = fisherYates(state.videos.map((_, i) => i));
  const idx = playlist[0];
  update({ shuffled: true, playlist, playlistPos: 0, currentVideo: idx });
  video.src = videoUrl(state.videos[idx].path);
  video.play();
  $('player-area').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function doDelete() {
  if (state.currentVideo < 0) return;
  const v = state.videos[state.currentVideo];
  if (!confirm('Delete ' + v.name + '?')) return;
  try {
    const resp = await fetch('/api/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: v.path }),
    });
    const data = await resp.json();
    if (!data.ok) return;

    video.pause();
    video.removeAttribute('src');

    const removedIdx = state.currentVideo;
    const videos = state.videos.filter((_, i) => i !== removedIdx);
    const playlist = state.playlist
      .filter(i => i !== removedIdx)
      .map(i => i > removedIdx ? i - 1 : i);

    if (!videos.length) {
      update({ videos, playlist, playlistPos: -1, currentVideo: -1, shuffled: false });
      return;
    }

    const pos = Math.min(state.playlistPos, playlist.length - 1);
    const newIdx = playlist[pos];
    update({ videos, playlist, playlistPos: pos, currentVideo: newIdx });
    video.src = videoUrl(videos[newIdx].path);
    video.play();
  } catch (e) {}
}

// --- fscrypt ---

async function doUnlock() {
  const pass = $('modal-pass').value;
  if (!pass) return;
  update({ loading: true, modalError: null });
  try {
    const resp = await fetch('/api/unlock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: state.modalPath, passphrase: pass }),
    });
    const data = await resp.json();
    $('modal-pass').value = '';
    if (data.ok) {
      const pending = state.pendingPath || state.modalPath;
      update({ modalPath: null, modalError: null, loading: false, pendingPath: null });
      navigateTo(pending);
    } else {
      update({ modalError: data.error || 'Unlock failed', loading: false });
    }
  } catch (e) {
    $('modal-pass').value = '';
    update({ modalError: 'Network error', loading: false });
  }
}

async function doLock(path) {
  if (!confirm('Lock this directory?')) return;
  try {
    const resp = await fetch('/api/lock', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const data = await resp.json();
    if (!data.ok) {
      alert('Lock failed: ' + (data.error || 'unknown error'));
      return;
    }
    video.pause();
    video.removeAttribute('src');
    const parent = path.includes('/') ? path.substring(0, path.lastIndexOf('/')) : '';
    navigateTo(parent);
  } catch (e) {
    alert('Lock failed: network error');
  }
}

function goBack() {
  if (state.modalPath) {
    update({ modalPath: null, modalError: null, pendingPath: null });
    return;
  }
  if (!state.path) return;
  const parent = state.path.includes('/')
    ? state.path.substring(0, state.path.lastIndexOf('/'))
    : '';
  navigateTo(parent);
}

// --- Render ---

function render() {
  renderNav();
  renderPlayer();
  renderList();
  renderModal();
}

function renderNav() {
  const parts = state.path ? state.path.split('/') : [];
  let html = '<span class="crumb" data-nav="">SimpleParty</span>';
  let acc = '';
  for (const part of parts) {
    acc += (acc ? '/' : '') + part;
    html += '<span class="crumb-sep">/</span>';
    html += '<span class="crumb" data-nav="' + esc(acc) + '">' + esc(part) + '</span>';
  }
  html += '<span class="nav-spacer"></span>';
  if (state.encryptedDir != null) {
    html += '<button class="btn btn-lock" data-lock="' + esc(state.encryptedDir) + '">Lock</button>';
  }
  $('nav').innerHTML = html;
}

function renderPlayer() {
  const area = $('player-area');
  if (state.currentVideo < 0) { area.classList.remove('visible'); return; }
  area.classList.add('visible');
  const v = state.videos[state.currentVideo];
  $('now-playing').textContent = v.name + ' (' + (state.playlistPos + 1) + '/' + state.playlist.length + ')';
  $('shuffle-btn').classList.toggle('active', state.shuffled);
}

function renderList() {
  const list = $('file-list');
  let html = '';

  if (state.videos.length) {
    html += '<div class="action-bar">'
      + '<button class="btn" id="shuffle-play-btn" title="Shuffle Play">&#8645; Shuffle Play</button>'
      + '</div>';
  }

  for (const dir of state.dirs) {
    const icon = dir.encrypted ? (dir.unlocked ? '&#128275;' : '&#128274;') : '&#128193;';
    html += '<div class="item" data-nav="' + esc(dir.path) + '">'
      + '<span class="item-icon">' + icon + '</span>'
      + '<span class="item-name">' + esc(dir.name) + '</span></div>';
  }

  for (let i = 0; i < state.videos.length; i++) {
    const v = state.videos[i];
    const cls = i === state.currentVideo ? ' playing' : '';
    html += '<div class="item' + cls + '" data-video="' + i + '">'
      + '<span class="item-icon">&#127916;</span>'
      + '<span class="item-name">' + esc(v.name) + '</span>'
      + '<span class="item-size">' + fmtSize(v.size) + '</span></div>';
  }

  if (!state.dirs.length && !state.videos.length && !state.loading) {
    html = '<div class="empty">Empty directory</div>';
  }

  list.innerHTML = html;
  list.classList.toggle('loading', state.loading);
}

function renderModal() {
  const modal = $('modal');
  if (state.modalPath != null) {
    modal.classList.add('visible');
    $('modal-error').textContent = state.modalError || '';
    $('modal-title').textContent = 'Unlock ' + (state.modalPath || 'directory');
    setTimeout(() => $('modal-pass').focus(), 50);
  } else {
    modal.classList.remove('visible');
    $('modal-pass').value = '';
  }
}

// --- Events ---

document.addEventListener('click', e => {
  if (e.target.closest('#shuffle-play-btn')) { shufflePlay(); return; }
  const nav = e.target.closest('[data-nav]');
  if (nav) { navigateTo(nav.dataset.nav); return; }
  const vid = e.target.closest('[data-video]');
  if (vid) { selectVideo(parseInt(vid.dataset.video)); return; }
  const lock = e.target.closest('[data-lock]');
  if (lock) { doLock(lock.dataset.lock); return; }
});

$('prev-btn').addEventListener('click', prevVideo);
$('next-btn').addEventListener('click', nextVideo);
$('shuffle-btn').addEventListener('click', toggleShuffle);
$('delete-btn').addEventListener('click', doDelete);
$('modal-submit').addEventListener('click', doUnlock);
$('modal-cancel').addEventListener('click', () => update({ modalPath: null, modalError: null, pendingPath: null }));
$('modal-pass').addEventListener('keydown', e => { if (e.key === 'Enter') doUnlock(); });

video.addEventListener('ended', nextVideo);

const shortcuts = {
  'n': nextVideo, 'ArrowRight': nextVideo,
  'p': prevVideo, 'ArrowLeft': prevVideo,
  's': toggleShuffle, 'd': doDelete,
  'f': () => { if (video.src) { document.fullscreenElement ? document.exitFullscreen() : video.requestFullscreen(); } },
  ' ': () => { if (video.src) { video.paused ? video.play() : video.pause(); } },
  'm': () => { if (video.src) video.muted = !video.muted; },
  'Escape': goBack,
  '?': () => $('shortcuts').classList.toggle('visible'),
};

document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const action = shortcuts[e.key];
  if (action) { e.preventDefault(); action(); }
});

// Init
navigateTo(decodeURIComponent(window.location.hash.slice(1)) || '');
</script>
</body>
</html>"""


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description='SimpleParty - Video directory browser')
    parser.add_argument('root', help='Root directory to serve')
    parser.add_argument('-p', '--port', type=int, default=1312, help='Port (default: 1312)')
    args = parser.parse_args()

    root = str(Path(args.root).resolve())
    if not Path(root).is_dir():
        print(f'Error: {root} is not a directory', file=__import__('sys').stderr)
        raise SystemExit(1)

    from functools import partial
    handler = partial(RequestHandler, root)
    server = ThreadedServer(('0.0.0.0', args.port), handler)
    transcoder = 'ffmpeg' if HAS_FFMPEG else 'cvlc' if HAS_VLC else 'none'
    print(f'SimpleParty serving {root} on http://0.0.0.0:{args.port} (transcoder: {transcoder})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
