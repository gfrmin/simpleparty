#!/usr/bin/env python3
"""SimpleParty - Easily enjoy your private video collection."""

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.parse
from functools import partial
from html import escape as esc
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

BROWSER_NATIVE = frozenset({'.mp4', '.webm', '.ogv', '.m4v'})

_config = {
    'has_ffmpeg': False,
    'has_vlc': False,
    'allow_delete': True,
    'allow_transcode': True,
}

MIME_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.m4v': 'video/mp4',
    '.ogv': 'video/ogg',
}


# --- Filesystem ---

def is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def resolve_path(root, relative):
    """Resolve a path relative to root, following symlinks."""
    if not relative:
        return Path(root).resolve()
    return (Path(root) / relative).resolve()


def list_directory(root, rel_path):
    """List directory contents. Returns dict with dirs, videos, or error/locked."""
    resolved = resolve_path(root, rel_path)

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

    dirs, videos = [], []
    for name in entries:
        if name.startswith('.'):
            continue
        full = resolved / name
        child_path = os.path.join(rel_path, name) if rel_path else name
        if full.is_dir():
            dir_status = get_fscrypt_status(full)
            dirs.append({
                'name': name, 'path': child_path,
                'encrypted': dir_status['encrypted'],
                'unlocked': dir_status['unlocked'],
            })
        elif full.is_file() and is_video(name):
            try:
                size = full.stat().st_size
            except OSError:
                size = 0
            videos.append({'name': name, 'path': child_path, 'size': size})

    return {
        'path': rel_path, 'dirs': dirs, 'videos': videos,
        'encryptedDir': encrypted_root,
    }


# --- fscrypt ---

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
        proc.kill()
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


# --- URL + format helpers ---

def parse_query(url):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: v[0] for k, v in params.items()}


def url_for_browse(path=''):
    return '/' if not path else '/browse?' + urllib.parse.urlencode({'path': path})


def url_for_play(dir_path, idx, shuffle=False, seed=None, pos=None):
    params = {'path': dir_path, 'idx': str(idx)}
    if shuffle:
        params['shuffle'] = '1'
        if seed is not None:
            params['seed'] = str(seed)
        if pos is not None:
            params['pos'] = str(pos)
    return '/play?' + urllib.parse.urlencode(params)


def url_for_video(path):
    return '/video/' + '/'.join(urllib.parse.quote(p, safe='') for p in path.split('/'))


def fmt_size(b):
    if b < 1024:
        return f'{b} B'
    if b < 1048576:
        return f'{b / 1024:.1f} KB'
    if b < 1073741824:
        return f'{b / 1048576:.1f} MB'
    return f'{b / 1073741824:.1f} GB'


def shuffle_indices(n, seed):
    rng = random.Random(seed)
    indices = list(range(n))
    rng.shuffle(indices)
    return indices


def safe_int(s, default=0):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


# --- HTML rendering ---

CSS = """\
*,*::before,*::after{margin:0;padding:0;box-sizing:border-box}
html{height:100%}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  background:#0f0f1a;color:#e2e8f0;min-height:100%;overflow-x:hidden;max-width:100vw;
}
a{color:inherit;text-decoration:none}
nav{
  position:sticky;top:0;background:#1a1a2e;padding:12px 16px;
  display:flex;align-items:center;gap:4px;
  border-bottom:1px solid #2d2d44;z-index:10;flex-wrap:wrap;min-height:48px;
  overflow:hidden;max-width:100vw;
}
.crumb{
  color:#94a3b8;padding:4px 6px;border-radius:4px;
  font-size:15px;white-space:nowrap;
}
.crumb:hover{color:#c4b5fd;background:rgba(167,139,250,0.1)}
.crumb-sep{color:#4a4a6a;padding:0 2px;user-select:none}
.nav-spacer{flex:1}
.btn{
  background:#16213e;color:#e2e8f0;border:1px solid #2d2d44;
  padding:8px 14px;border-radius:6px;cursor:pointer;font-size:14px;
  min-height:40px;white-space:nowrap;transition:all .15s;
  display:inline-flex;align-items:center;
}
.btn:hover{background:#1e3054;border-color:#a78bfa}
.btn.active{background:#7c3aed;color:#fff;border-color:#7c3aed}
.btn-lock{border-color:#991b1b}
.btn-lock:hover{background:#7f1d1d;border-color:#dc2626}
#player-area{background:#000}
video{width:100%;max-height:70vh;display:block;background:#000}
#controls{
  display:flex;align-items:center;padding:8px 16px;gap:8px;
  background:#1a1a2e;border-bottom:1px solid #2d2d44;flex-wrap:wrap;
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
  background:#16213e;border-radius:8px;min-height:48px;
  transition:background .15s;border:2px solid transparent;min-width:0;overflow:hidden;
}
.item:hover{background:#1e3054}
.item.playing{border-color:#7c3aed}
.item-link{display:flex;align-items:center;gap:10px;flex:1;min-width:0}
.item-icon{font-size:18px;flex-shrink:0;line-height:1}
.item-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;flex:1}
.item-size{color:#64748b;font-size:12px;flex-shrink:0}
.btn-del{
  background:none;border:none;color:#64748b;cursor:pointer;font-size:16px;
  padding:4px;border-radius:4px;flex-shrink:0;line-height:1;
}
.btn-del:hover{color:#f87171;background:rgba(248,113,113,0.1)}
.empty{grid-column:1/-1;color:#64748b;text-align:center;padding:40px 20px;font-size:15px}
.action-bar{grid-column:1/-1;display:flex;gap:8px;padding-bottom:4px}
.unlock-box{
  max-width:380px;margin:40px auto;background:#1a1a2e;border:1px solid #2d2d44;
  border-radius:12px;padding:24px;
}
.unlock-box h3{margin-bottom:16px;font-size:18px}
.unlock-box input[type="password"]{
  width:100%;padding:12px;background:#0f0f1a;border:1px solid #2d2d44;
  border-radius:6px;color:#e2e8f0;font-size:16px;outline:none;
}
.unlock-box input:focus{border-color:#7c3aed}
.unlock-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.unlock-error{color:#f87171;font-size:13px;margin-top:10px;min-height:1.2em}
.error-page{color:#f87171;text-align:center;padding:60px 20px;font-size:16px}
@media(max-width:640px){
  #file-list{grid-template-columns:1fr;padding:8px;gap:6px}
  nav{padding:8px 12px}
  #controls{padding:6px 12px;justify-content:center}
}"""


def render_page(title, body):
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f'<title>{esc(title)}</title>\n'
        f'<style>{CSS}</style>\n'
        '<script src="https://unpkg.com/htmx.org@2.0.4"></script>\n'
        '</head>\n<body>\n'
        f'{body}\n'
        '</body>\n</html>'
    )


def render_nav(path, encrypted_dir=None):
    parts = path.split('/') if path else []
    pieces = ['<a class="crumb" href="/">SimpleParty</a>']
    acc = ''
    for part in parts:
        acc += ('/' if acc else '') + part
        pieces.append(f'<span class="crumb-sep">/</span>')
        pieces.append(f'<a class="crumb" href="{esc(url_for_browse(acc))}">{esc(part)}</a>')
    pieces.append('<span class="nav-spacer"></span>')
    if encrypted_dir is not None:
        parent = str(Path(encrypted_dir).parent)
        if parent == '.':
            parent = ''
        pieces.append(
            f'<form hx-post="/lock" hx-confirm="Lock this directory?" style="display:inline">'
            f'<input type="hidden" name="path" value="{esc(encrypted_dir)}">'
            f'<input type="hidden" name="redirect" value="{esc(url_for_browse(parent))}">'
            f'<button type="submit" class="btn btn-lock">Lock</button>'
            f'</form>'
        )
    return '<nav>' + ''.join(pieces) + '</nav>'


def render_file_list(data, current_idx=-1):
    pieces = ['<div id="file-list">']

    if data['videos']:
        shuffle_url = '/play?' + urllib.parse.urlencode({'path': data['path'], 'shuffle': '1'})
        pieces.append(
            f'<div class="action-bar">'
            f'<a class="btn" href="{esc(shuffle_url)}">\u21C5 Shuffle Play</a>'
            f'</div>'
        )

    for d in data['dirs']:
        if d['encrypted'] and not d['unlocked']:
            icon = '\U0001F512'
        elif d['encrypted']:
            icon = '\U0001F513'
        else:
            icon = '\U0001F4C1'
        pieces.append(
            f'<a class="item" href="{esc(url_for_browse(d["path"]))}">'
            f'<span class="item-icon">{icon}</span>'
            f'<span class="item-name">{esc(d["name"])}</span>'
            f'</a>'
        )

    for i, v in enumerate(data['videos']):
        cls = ' playing' if i == current_idx else ''
        play_url = url_for_play(data['path'], i)
        pieces.append(f'<div class="item{cls}">')
        pieces.append(
            f'<a class="item-link" href="{esc(play_url)}">'
            f'<span class="item-icon">\U0001F3AC</span>'
            f'<span class="item-name">{esc(v["name"])}</span>'
            f'<span class="item-size">{fmt_size(v["size"])}</span>'
            f'</a>'
        )
        if _config['allow_delete']:
            pieces.append(
                f'<form hx-post="/delete" hx-target="closest .item" hx-swap="delete" '
                f'hx-confirm="Delete {esc(v["name"])}?">'
                f'<input type="hidden" name="path" value="{esc(v["path"])}">'
                f'<button type="submit" class="btn-del" title="Delete">\U0001F5D1</button>'
                f'</form>'
            )
        pieces.append('</div>')

    if not data['dirs'] and not data['videos']:
        pieces.append('<div class="empty">Empty directory</div>')

    pieces.append('</div>')
    return ''.join(pieces)


def render_browse_page(data):
    title = f'SimpleParty \u2014 {data["path"].split("/")[-1]}' if data['path'] else 'SimpleParty'
    body = render_nav(data['path'], data.get('encryptedDir'))
    body += render_file_list(data)
    return render_page(title, body)


def render_locked_page(path, encrypted_dir, redirect_path=None, error=None):
    body = render_nav(path)
    dir_name = encrypted_dir.split('/')[-1] if encrypted_dir else 'directory'
    redir = redirect_path or path
    parent = str(Path(path).parent) if '/' in path else ''
    if parent == '.':
        parent = ''
    body += (
        f'<div class="unlock-box">'
        f'<h3>Unlock {esc(dir_name)}</h3>'
        f'<form hx-post="/unlock" hx-target="#unlock-error" hx-swap="innerHTML">'
        f'<input type="hidden" name="path" value="{esc(encrypted_dir)}">'
        f'<input type="hidden" name="redirect" value="{esc(url_for_browse(redir))}">'
        f'<input type="password" name="passphrase" placeholder="Passphrase" autofocus>'
        f'<div id="unlock-error" class="unlock-error">{esc(error) if error else ""}</div>'
        f'<div class="unlock-actions">'
        f'<a class="btn" href="{esc(url_for_browse(parent))}">Cancel</a>'
        f'<button class="btn active" type="submit">Unlock</button>'
        f'</div></form></div>'
    )
    return render_page('SimpleParty \u2014 Unlock', body)


def render_error_page(path, error):
    body = render_nav(path)
    body += f'<div class="error-page">{esc(error)}</div>'
    return render_page('SimpleParty \u2014 Error', body)


def render_play_page(data, idx, next_url, prev_url, shuffle_url, is_shuffled, pos_info):
    v = data['videos'][idx]
    video_src = url_for_video(v['path'])
    browse_url = url_for_browse(data['path'])

    body = render_nav(data['path'], data.get('encryptedDir'))
    body += (
        f'<div id="player-area">'
        f'<video id="video" src="{esc(video_src)}" controls playsinline autoplay></video>'
        f'<div id="controls">'
        f'<a class="btn" href="{esc(prev_url)}" title="Previous (p)">\u25C0 Prev</a>'
        f'<span id="now-playing">{esc(v["name"])} ({pos_info})</span>'
        f'<a class="btn" href="{esc(next_url)}" title="Next (n)">Next \u25B6</a>'
        f'<a class="btn{" active" if is_shuffled else ""}" '
        f'href="{esc(shuffle_url)}" title="Shuffle (s)">\u21C5 Shuffle</a>'
    )
    if _config['allow_delete']:
        body += (
            f'<form id="delete-form" hx-post="/delete" hx-confirm="Delete {esc(v["name"])}?">'
            f'<input type="hidden" name="path" value="{esc(v["path"])}">'
            f'<input type="hidden" name="redirect" value="{esc(browse_url)}">'
            f'<button type="submit" class="btn btn-lock" title="Delete (d)">'
            f'\U0001F5D1</button></form>'
        )
    body += '</div></div>'

    body += render_file_list(data, current_idx=idx)

    body += (
        '<script>\n'
        f'const video=document.getElementById("video");\n'
        f'const nextUrl={json.dumps(next_url)};\n'
        f'const prevUrl={json.dumps(prev_url)};\n'
        f'const browseUrl={json.dumps(browse_url)};\n'
        'video.addEventListener("ended",()=>{window.location.href=nextUrl});\n'
        'document.addEventListener("keydown",e=>{\n'
        '  if(e.target.tagName==="INPUT")return;\n'
        '  switch(e.key){\n'
        '    case"n":case"ArrowRight":window.location.href=nextUrl;break;\n'
        '    case"p":case"ArrowLeft":window.location.href=prevUrl;break;\n'
        '    case" ":e.preventDefault();video.paused?video.play():video.pause();break;\n'
        '    case"f":e.preventDefault();document.fullscreenElement?document.exitFullscreen():video.requestFullscreen();break;\n'
        '    case"m":video.muted=!video.muted;break;\n'
        '    case"Escape":window.location.href=browseUrl;break;\n'
        '    case"d":document.querySelector("#delete-form button")?.click();break;\n'
        '  }\n'
        '});\n'
        '</script>'
    )

    return render_page(f'SimpleParty \u2014 {v["name"]}', body)


# --- Video serving ---

def _needs_transcode(path):
    return path.suffix.lower() not in BROWSER_NATIVE


def _serve_transcoded(handler, path):
    if _config['has_ffmpeg']:
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


# --- HTTP helpers ---

def send_html(handler, content, status=200):
    body = content.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_redirect(handler, url):
    handler.send_response(302)
    handler.send_header('Location', url)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def send_hx_redirect(handler, url):
    handler.send_response(200)
    handler.send_header('HX-Redirect', url)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def read_form_body(handler):
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    params = urllib.parse.parse_qs(body)
    return {k: v[0] for k, v in params.items()}


# --- Route handlers ---

def handle_browse(handler, root):
    params = parse_query(handler.path)
    rel_path = params.get('path', '')
    data = list_directory(root, rel_path)
    if data.get('locked'):
        send_html(handler, render_locked_page(rel_path, data['encryptedDir']))
    elif 'error' in data:
        status = 404 if data['error'] == 'Not found' else 400
        send_html(handler, render_error_page(rel_path, data['error']), status)
    else:
        send_html(handler, render_browse_page(data))


def handle_play(handler, root):
    params = parse_query(handler.path)
    dir_path = params.get('path', '')
    data = list_directory(root, dir_path)

    if data.get('locked'):
        send_html(handler, render_locked_page(dir_path, data['encryptedDir']))
        return
    if 'error' in data or not data.get('videos'):
        send_redirect(handler, url_for_browse(dir_path))
        return

    n = len(data['videos'])
    shuffled = params.get('shuffle') == '1'

    if shuffled:
        seed = safe_int(params.get('seed'), random.randint(0, 2**31))
        pos = safe_int(params.get('pos')) % n
        order = shuffle_indices(n, seed)
        idx = order[pos]
        next_pos = (pos + 1) % n
        prev_pos = (pos - 1) % n
        next_url = url_for_play(dir_path, order[next_pos], shuffle=True, seed=seed, pos=next_pos)
        prev_url = url_for_play(dir_path, order[prev_pos], shuffle=True, seed=seed, pos=prev_pos)
        pos_info = f'{pos + 1}/{n}'
        shuffle_url = url_for_play(dir_path, idx)
    else:
        idx = max(0, min(safe_int(params.get('idx')), n - 1))
        next_url = url_for_play(dir_path, (idx + 1) % n)
        prev_url = url_for_play(dir_path, (idx - 1) % n)
        pos_info = f'{idx + 1}/{n}'
        shuffle_url = '/play?' + urllib.parse.urlencode({'path': dir_path, 'shuffle': '1'})

    send_html(handler, render_play_page(data, idx, next_url, prev_url, shuffle_url, shuffled, pos_info))


def handle_video(handler, root):
    parsed = urllib.parse.urlparse(handler.path)
    rel_path = urllib.parse.unquote(parsed.path[len('/video/'):])
    resolved = resolve_path(root, rel_path)

    if not resolved.is_file():
        handler.send_error(404)
        return

    if _config['allow_transcode'] and _needs_transcode(resolved) and (_config['has_ffmpeg'] or _config['has_vlc']):
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


def handle_delete(handler, root):
    if not _config['allow_delete']:
        handler.send_error(403, 'Delete disabled')
        return
    form = read_form_body(handler)
    rel_path = form.get('path', '')
    redirect_url = form.get('redirect')
    resolved = resolve_path(root, rel_path)
    if not resolved.is_file() or not is_video(resolved.name):
        handler.send_error(400, 'Invalid video path')
        return
    try:
        os.remove(resolved)
    except OSError as e:
        handler.send_error(500, str(e))
        return
    if redirect_url:
        send_hx_redirect(handler, redirect_url)
    else:
        handler.send_response(200)
        handler.send_header('Content-Length', '0')
        handler.end_headers()


def handle_unlock(handler, root):
    form = read_form_body(handler)
    encrypted_path = form.get('path', '')
    passphrase = form.get('passphrase', '')
    redirect_url = form.get('redirect', url_for_browse(encrypted_path))
    resolved = resolve_path(root, encrypted_path)
    ok, msg = fscrypt_unlock(resolved, passphrase)
    del passphrase
    if ok:
        send_hx_redirect(handler, redirect_url)
    else:
        send_html(handler, esc(msg or 'Unlock failed'))


def handle_lock(handler, root):
    form = read_form_body(handler)
    path = form.get('path', '')
    redirect_url = form.get('redirect', url_for_browse(''))
    resolved = resolve_path(root, path)
    fscrypt_lock(resolved)
    send_hx_redirect(handler, redirect_url)


# --- Server ---

class RequestHandler(BaseHTTPRequestHandler):
    def __init__(self, root, *args, **kwargs):
        self.root = root
        super().__init__(*args, **kwargs)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/' or path == '/browse':
            handle_browse(self, self.root)
        elif path == '/play':
            handle_play(self, self.root)
        elif path.startswith('/video/'):
            handle_video(self, self.root)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/delete':
            handle_delete(self, self.root)
        elif path == '/unlock':
            handle_unlock(self, self.root)
        elif path == '/lock':
            handle_lock(self, self.root)
        else:
            self.send_error(404)

    def do_HEAD(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith('/video/'):
            handle_video(self, self.root)
        else:
            self.do_GET()


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(
        description='SimpleParty - Easily enjoy your private video collection',
    )
    parser.add_argument('root', nargs='?', default='.', help='Root directory to serve (default: current directory)')
    parser.add_argument('-p', '--port', type=int, default=1312, help='Port (default: 1312)')
    parser.add_argument('-b', '--bind', default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    parser.add_argument('--no-delete', action='store_true', help='Disable video deletion')
    parser.add_argument('--no-transcode', action='store_true', help='Disable ffmpeg/VLC transcoding')
    args = parser.parse_args()

    root = str(Path(args.root).resolve())
    if not Path(root).is_dir():
        print(f'Error: {root} is not a directory', file=sys.stderr)
        raise SystemExit(1)

    _config['has_ffmpeg'] = shutil.which('ffmpeg') is not None
    _config['has_vlc'] = shutil.which('cvlc') is not None
    _config['allow_delete'] = not args.no_delete
    _config['allow_transcode'] = not args.no_transcode

    handler = partial(RequestHandler, root)
    server = ThreadedServer((args.bind, args.port), handler)

    features = []
    if _config['allow_transcode']:
        if _config['has_ffmpeg']:
            features.append('transcode: ffmpeg')
        elif _config['has_vlc']:
            features.append('transcode: vlc')
    if _config['allow_delete']:
        features.append('delete: on')
    if shutil.which('fscrypt'):
        features.append('fscrypt: on')

    url = f'http://{args.bind}:{args.port}'
    print(f'SimpleParty serving {root}')
    print(f'  {url}')
    if features:
        print(f'  [{", ".join(features)}]')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
