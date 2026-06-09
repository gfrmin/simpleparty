#!/usr/bin/env python3
"""SimpleParty - Easily enjoy your private video collection."""

import argparse
import json
import logging
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from functools import partial
from html import escape as esc
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from simpleparty.state import (
    CONFIG as _config,
    BROWSER_NATIVE,
    DOWNLOAD_HISTORY_LIMIT,
    MIME_TYPES,
    VIDEO_EXTENSIONS,
)

logger = logging.getLogger('simpleparty.server')


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
                st = full.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            videos.append({'name': name, 'path': child_path, 'size': size, 'mtime': mtime})

    return {
        'path': rel_path, 'dirs': dirs, 'videos': videos,
        'encryptedDir': encrypted_root,
    }


# --- fscrypt ---

_FSCRYPT_TTL_SEC = 60.0
_fscrypt_cache = {}
_fscrypt_cache_lock = threading.Lock()
_fscrypt_missing = False


def _fscrypt_cache_key(dir_path):
    try:
        return str(Path(dir_path).resolve())
    except OSError:
        return str(dir_path)


def _invalidate_fscrypt_cache(dir_path):
    key = _fscrypt_cache_key(dir_path)
    with _fscrypt_cache_lock:
        _fscrypt_cache.pop(key, None)


def get_fscrypt_status(dir_path):
    if _fscrypt_missing:
        return {'encrypted': False, 'unlocked': True}
    key = _fscrypt_cache_key(dir_path)
    now = time.monotonic()
    with _fscrypt_cache_lock:
        cached = _fscrypt_cache.get(key)
        if cached and now - cached[1] < _FSCRYPT_TTL_SEC:
            return cached[0]
    status = _probe_fscrypt_status(dir_path)
    with _fscrypt_cache_lock:
        _fscrypt_cache[key] = (status, now)
    return status


def _probe_fscrypt_status(dir_path):
    global _fscrypt_missing
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
    except FileNotFoundError:
        _fscrypt_missing = True
        return {'encrypted': False, 'unlocked': True}
    except subprocess.TimeoutExpired:
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
        success = proc.returncode == 0
        if success:
            _invalidate_fscrypt_cache(dir_path)
        return success, (stdout.decode() + stderr.decode()).strip()
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
        success = result.returncode == 0
        if success:
            _invalidate_fscrypt_cache(dir_path)
        return success, (result.stdout + result.stderr).strip()
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


def url_for_browse(path='', tags=None, sort=None, direction=None, starred=False):
    params = {}
    if path:
        params['path'] = path
    if tags:
        params['tags'] = ','.join(tags)
    if sort and sort != 'date':
        params['sort'] = sort
    if direction and direction != 'desc':
        params['dir'] = direction
    if starred:
        params['starred'] = '1'
    return '/' if not params else '/browse?' + urllib.parse.urlencode(params)


def url_for_play(dir_path, idx, shuffle=False, seed=None, pos=None, tags=None, video=None, sort=None, direction=None, starred=False):
    params = {'path': dir_path, 'idx': str(idx)}
    if video:
        params['video'] = video
    if shuffle:
        params['shuffle'] = '1'
        if seed is not None:
            params['seed'] = str(seed)
        if pos is not None:
            params['pos'] = str(pos)
    if tags:
        params['tags'] = ','.join(tags)
    if sort and sort != 'date':
        params['sort'] = sort
    if direction and direction != 'desc':
        params['dir'] = direction
    if starred:
        params['starred'] = '1'
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


def parse_tags_param(params):
    """Parse comma-separated tags from URL params into a list."""
    raw = params.get('tags', '')
    return [t.strip() for t in raw.split(',') if t.strip()] if raw else []


def parse_starred_param(params):
    """Return True if the request asks for starred-only filtering."""
    return params.get('starred', '') == '1'


_SORT_FIELDS = {'name', 'size', 'date', 'length'}
_SORT_DIRS = {'asc', 'desc'}


def parse_sort_params(params):
    """Return (sort_field, direction) with defaults date/desc."""
    sort = params.get('sort', 'date')
    if sort not in _SORT_FIELDS:
        sort = 'date'
    direction = params.get('dir', 'desc')
    if direction not in _SORT_DIRS:
        direction = 'desc'
    return sort, direction


def sort_videos(videos, sort, direction):
    reverse = direction == 'desc'
    if sort == 'size':
        key = lambda v: (v.get('size', 0), v['name'].lower())
    elif sort == 'date':
        key = lambda v: (v.get('mtime', 0.0), v['name'].lower())
    elif sort == 'length':
        key = lambda v: (v.get('duration', 0.0), v['name'].lower())
    else:
        key = lambda v: v['name'].lower()
    return sorted(videos, key=key, reverse=reverse)


def _populate_durations(root, videos, tags_map, resolved):
    """Fill v['duration'] for each video. If tags_map is provided, cache durations
    in the tags file (saved only when something new was probed)."""
    if tags_map is None:
        from simpleparty.tagger import _get_duration
        for v in videos:
            v['duration'] = _get_duration(Path(root) / v['path'])
        return
    from simpleparty.tagger import get_video_duration, save_tags
    changed = False
    for v in videos:
        dur, ch = get_video_duration(
            v['name'], Path(root) / v['path'], tags_map, v.get('mtime', 0.0),
        )
        v['duration'] = dur
        changed = changed or ch
    if changed:
        save_tags(resolved, tags_map)


def filter_videos_by_tags(videos, tags_map, selected_tags):
    """Filter video list to those having ALL selected tags (AND logic)."""
    if not selected_tags or not tags_map:
        return videos
    selected_lower = {t.lower() for t in selected_tags}
    return [
        v for v in videos
        if v['name'] in tags_map
        and selected_lower <= {t.lower() for t in tags_map[v['name']].get('tags', [])}
    ]


def filter_videos_by_starred(videos, tags_map, starred_only):
    """Filter video list to only those marked starred."""
    if not starred_only:
        return videos
    if not tags_map:
        return []
    return [v for v in videos if tags_map.get(v['name'], {}).get('starred')]


def _compute_related_videos(data, idx, tags_map, max_results=8):
    """Return list of (video_index, shared_tag_count) for videos sharing tags with current."""
    if not tags_map:
        return []
    current = data['videos'][idx]
    current_tags = {t.lower() for t in tags_map.get(current['name'], {}).get('tags', [])}
    if not current_tags:
        return []
    scored = []
    for i, v in enumerate(data['videos']):
        if i == idx:
            continue
        vtags = {t.lower() for t in tags_map.get(v['name'], {}).get('tags', [])}
        overlap = len(current_tags & vtags)
        if overlap > 0:
            scored.append((i, overlap))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:max_results]


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


def find_video_idx(videos, name):
    """Find a video's index by name. Returns None if not found."""
    for i, v in enumerate(videos):
        if v['name'] == name:
            return i
    return None


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
.btn-star{padding:6px 10px;line-height:1}
.btn-star .star-icon{font-size:18px}
.btn-star.active{background:#facc15;color:#1a1a2e;border-color:#facc15}
.btn-star.active:hover{background:#eab308;border-color:#eab308}
.btn-star[disabled]{opacity:0.6;cursor:wait}
.tag-pill.star-pill{color:#facc15}
.tag-pill.star-pill.active{background:#facc15;color:#1a1a2e}
#player-area{position:sticky;top:0;z-index:5;background:#000}
#transcode-notice{
  background:#3a2e0a;color:#fde68a;border-bottom:1px solid #78350f;
  padding:8px 16px;font-size:13px;line-height:1.4;
}
video{width:100%;max-height:70vh;display:block;background:#000}
#controls{
  display:flex;align-items:center;padding:8px 16px;gap:8px;
  background:#1a1a2e;border-bottom:1px solid #2d2d44;flex-wrap:wrap;
}
.skip-group{display:flex;gap:4px}
.btn-skip{padding:6px 10px;font-size:12px;min-height:32px}
.speed-select{
  background:#16213e;color:#e2e8f0;border:1px solid #2d2d44;
  padding:6px 8px;border-radius:6px;font-size:13px;cursor:pointer;
  min-height:32px;
}
.speed-select:hover{border-color:#a78bfa}
#video-overlay{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  background:rgba(0,0,0,0.7);color:#fff;padding:10px 20px;border-radius:8px;
  font-size:20px;font-weight:bold;pointer-events:none;opacity:0;transition:opacity .15s;
  z-index:6;
}
#video-title{
  padding:8px 16px 0;background:#1a1a2e;
  font-size:16px;font-weight:600;color:#e2e8f0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
#now-playing{
  flex:1;text-align:center;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;color:#94a3b8;font-size:12px;padding:0 8px;
}
#file-list{
  padding:16px;
  display:flex;flex-wrap:wrap;gap:8px;
  overflow:hidden;max-width:100%;
}
.item{
  display:flex;align-items:center;gap:10px;padding:12px 14px;
  background:#16213e;border-radius:8px;min-height:48px;
  transition:background .15s;border:2px solid transparent;min-width:0;overflow:hidden;
  flex-wrap:wrap;width:100%;
}
.item-video{
  flex:0 1 calc(20% - 7px);min-width:90px;
  flex-direction:column;align-items:stretch;padding:0;gap:0;width:auto;
}
.item-video .item-link{flex-direction:column;align-items:stretch;gap:0}
.item-video .item-info{display:flex;align-items:center;gap:8px;padding:8px 10px;min-width:0}
.item-thumb{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:6px 6px 0 0;display:block;background:#0f0f1a}
.item-thumb-placeholder{display:flex;align-items:center;justify-content:center;font-size:32px;color:#4a4a6a}
.item:hover{background:#1e3054}
.item-video:hover{background:#1e3054}
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
.item-video .btn-del{position:absolute;top:4px;right:4px;z-index:2;background:rgba(0,0,0,0.5);border-radius:50%;padding:4px 6px}
.item-video{position:relative}
.item-video .item-tags{padding:0 10px 8px;width:100%}
.empty{width:100%;color:#64748b;text-align:center;padding:40px 20px;font-size:15px}
.action-bar{width:100%;display:flex;gap:8px;padding-bottom:4px;flex-wrap:wrap;overflow:hidden}
.unlock-box{
  max-width:380px;margin:40px auto;background:#1a1a2e;border:1px solid #2d2d44;
  border-radius:12px;padding:24px;
}
.unlock-box h1{margin-bottom:16px;font-size:18px;font-weight:600}
.unlock-box input[type="password"]{
  width:100%;padding:12px;background:#0f0f1a;border:1px solid #2d2d44;
  border-radius:6px;color:#e2e8f0;font-size:16px;outline:none;
}
.unlock-box input:focus{border-color:#7c3aed}
.unlock-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.unlock-error{color:#f87171;font-size:13px;margin-top:10px;min-height:1.2em}
.error-page{color:#f87171;text-align:center;padding:60px 20px;font-size:16px}
.item-tags{color:#64748b;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;width:100%;padding-top:2px}
.item-tags.suggested{color:#a78bfa;opacity:0.5;font-style:italic}
.btn-train.htmx-request{opacity:0.6;pointer-events:none}
.btn-train.htmx-request .btn-label{display:none}
.btn-train.htmx-request .btn-spinner{display:inline-block}
.btn-train:disabled,.btn-train.busy{opacity:0.5;cursor:not-allowed;pointer-events:none;border-color:#4a4a6a}
.btn-spinner{display:none;width:14px;height:14px;border:2px solid #64748b;border-top-color:#a78bfa;border-radius:50%;animation:spin .6s linear infinite;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.tag-progress-panel{display:flex;align-items:center;gap:10px;min-height:0;transition:all .3s ease}
.tag-progress-panel:empty{display:none}
.tag-progress-bar-wrap{flex:0 1 200px;min-width:80px;height:6px;background:#2d2d44;border-radius:3px;overflow:hidden}
.tag-progress-bar{height:100%;background:#7c3aed;border-radius:3px;transition:width .4s ease}
.tag-progress-text{color:#94a3b8;font-size:13px;white-space:nowrap}
.tag-progress-phase{color:#a78bfa;font-size:13px;font-weight:500}
.tag-progress-panel.active .tag-progress-phase{animation:pulse 1.5s ease infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.6}}
.tag-done{color:#4ade80;font-size:13px;font-weight:500;display:flex;align-items:center;gap:8px;animation:fadeIn .3s ease}
.tag-error{color:#f87171;font-size:13px;font-weight:500;animation:fadeIn .3s ease}
.download-details{display:inline-block}
.download-details[open] summary{color:#a78bfa}
.download-form{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px;width:100%}
.download-form input[type="url"],.download-form input[type="text"]{background:#0f0f1a;border:1px solid #2d2d44;border-radius:6px;color:#e2e8f0;padding:8px 10px;font-size:14px;flex:1;min-width:120px;outline:none}
.download-form input:focus{border-color:#7c3aed}
.download-progress-panel{display:flex;align-items:center;gap:10px;min-height:0;flex-wrap:wrap;transition:all .3s ease}
.download-progress-panel:empty{display:none}
.download-card{background:#1a1a2e;border:1px solid #2d2d44;border-radius:8px;padding:12px 14px;margin:8px 16px;display:flex;flex-direction:column;gap:8px}
.download-card.err{border-color:#7f1d1d}
.download-card .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.download-card .title{font-size:14px;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.download-card .url{font-size:12px;color:#64748b;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
.download-card .meta{color:#94a3b8;font-size:12px}
.download-board{padding:0 16px 24px}
.download-section-title{color:#94a3b8;font-size:13px;font-weight:500;margin:16px 16px 6px;text-transform:uppercase;letter-spacing:.5px}
@keyframes fadeIn{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:translateY(0)}}
.tag-filter{padding:8px 16px;border-bottom:1px solid #2d2d44;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.tag-selected-pills{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tag-pill{display:inline-flex;align-items:center;background:#2d2d44;color:#a78bfa;padding:4px 10px;border-radius:12px;font-size:12px;white-space:nowrap;cursor:pointer;text-decoration:none;transition:all .15s;gap:4px}
.tag-pill:hover{background:#3d3d5c}
.tag-pill.active{background:#7c3aed;color:#fff}
.tag-pill-count{color:#64748b;font-size:11px}
.tag-pill.active .tag-pill-count{color:rgba(255,255,255,0.7)}
.tag-pill-x{font-size:10px;opacity:0.7;margin-left:2px}
.tag-pill-x:hover{opacity:1}
.sort-pills{display:inline-flex;gap:6px;align-items:center}
.sort-pill{display:inline-flex;align-items:center;background:#2d2d44;color:#a78bfa;padding:4px 10px;border-radius:12px;font-size:12px;white-space:nowrap;text-decoration:none;transition:background .15s}
.sort-pill:hover{background:#3d3d5c}
.sort-pill.active{background:#7c3aed;color:#fff}
.sort-pill.active:hover{background:#6d28d9}
.tag-clear{color:#94a3b8;font-size:12px;cursor:pointer;text-decoration:underline;padding:4px 8px}
.tag-dropdown-wrap{position:relative;display:inline-block}
.tag-search{background:#0f0f1a;border:1px solid #2d2d44;border-radius:6px;color:#e2e8f0;padding:6px 10px;font-size:13px;width:220px}
.tag-search:focus{border-color:#7c3aed;outline:none}
.tag-dropdown{display:none;position:absolute;top:100%;left:0;z-index:50;background:#1a1a2e;border:1px solid #2d2d44;border-radius:6px;margin-top:4px;max-height:240px;overflow-y:auto;min-width:220px;box-shadow:0 4px 12px rgba(0,0,0,0.4)}
.tag-dropdown.open{display:block}
.tag-dropdown a{display:flex;justify-content:space-between;padding:6px 12px;color:#a78bfa;text-decoration:none;font-size:12px;transition:background .1s}
.tag-dropdown a:hover{background:#2d2d44}
.tag-dropdown a .cnt{color:#64748b;font-size:11px}
.video-meta{padding:8px 16px;background:#1a1a2e;border-bottom:1px solid #2d2d44;display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.video-meta .item-tags{width:100%}
.video-meta input[type="text"]{background:#0f0f1a;border:1px solid #2d2d44;border-radius:6px;color:#e2e8f0;padding:6px 10px;font-size:13px;flex:1;min-width:200px}
.video-meta input:focus{border-color:#7c3aed;outline:none}
.video-tag-pills{display:flex;flex-wrap:wrap;gap:6px;align-items:center;width:100%}
.video-tag-pill{display:inline-flex;align-items:center;background:#2d2d44;color:#a78bfa;padding:4px 10px;border-radius:12px;font-size:12px;white-space:nowrap;gap:4px}
.video-tag-pill.suggested{background:transparent;border:1px dashed #a78bfa;opacity:0.7}
.btn-confirm{color:#4ade80;border-color:#4ade80;font-size:12px;padding:2px 8px}
.btn-confirm:hover{background:#166534}
.btn-reject{color:#f87171;border-color:#f87171;font-size:12px;padding:2px 8px}
.btn-reject:hover{background:#7f1d1d}
.video-tag-remove{background:none;border:none;color:#a78bfa;cursor:pointer;font-size:10px;padding:0 0 0 2px;opacity:0.6;line-height:1}
.video-tag-remove:hover{opacity:1;color:#f87171}
.video-tag-add{background:#0f0f1a;border:1px solid #2d2d44;border-radius:12px;color:#e2e8f0;padding:4px 10px;font-size:12px;width:120px;outline:none}
.video-tag-add:focus{border-color:#7c3aed}
#related-videos{padding:16px;border-bottom:1px solid #2d2d44}
.related-heading{color:#94a3b8;font-size:14px;font-weight:500;margin-bottom:10px}
.related-list{display:flex;flex-wrap:wrap;gap:8px}
.related-list .item-video{flex:0 1 calc(12.5% - 7px);min-width:90px;max-width:180px}
.playlist{padding:16px}
.playlist-heading{color:#94a3b8;font-size:14px;font-weight:500;margin-bottom:10px}
.playlist-items{display:flex;flex-direction:column;gap:4px}
.playlist-item{display:flex;align-items:center;gap:10px;padding:6px 8px;border-radius:6px;text-decoration:none;color:#e2e8f0;transition:background .15s}
.playlist-item:hover{background:#1e3054}
.playlist-item.playing{background:#1a1a2e;border-left:3px solid #7c3aed}
.playlist-thumb{width:80px;aspect-ratio:16/9;object-fit:cover;border-radius:4px;flex-shrink:0;background:#0f0f1a}
.playlist-thumb-placeholder{width:80px;aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;font-size:20px;color:#4a4a6a;background:#0f0f1a;border-radius:4px;flex-shrink:0}
.playlist-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;flex:1;min-width:0}
.playlist-pos{color:#64748b;font-size:12px;flex-shrink:0;min-width:20px;text-align:right}
@media(max-width:1024px){
  .item-video{flex:0 1 calc(33.33% - 6px);max-width:none}
  .related-list .item-video{flex:0 1 calc(33.33% - 6px);max-width:none}
}
@media(max-width:640px){
  #file-list{padding:8px;gap:6px}
  .item-video{flex:0 1 calc(50% - 4px);min-width:0;max-width:none}
  .related-list .item-video{flex:0 1 calc(50% - 4px);min-width:0;max-width:none}
  #video-title{font-size:14px;padding:6px 12px 0}
  .playlist-thumb,.playlist-thumb-placeholder{width:60px}
  nav{padding:8px 12px}
  #controls{padding:6px 12px;justify-content:center}
}
/* --- Mobile & accessibility audit fixes (2026-06-05) --- */
.visually-hidden{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
a:focus-visible,button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:2px solid #a78bfa;outline-offset:2px;border-radius:4px}
.tag-search:focus,.video-meta input:focus{box-shadow:0 0 0 2px rgba(124,58,237,.55)}
/* L1: stop long filenames/tags from blowing out video cards. The card inherits
   flex-wrap:wrap from .item; as a column flex box that makes align-items:stretch
   size children to content width (a long name) instead of the card width. */
.item-video{flex-wrap:nowrap}
.item-name,.item-tags{min-width:0}
/* Contrast: lift muted greys to meet WCAG AA */
.item-size,.item-tags{color:#94a3b8}
.item-tags.suggested{opacity:1;color:#b3a4f7;font-style:italic}
.tag-pill-count{color:#aab2c2}
.tag-dropdown a .cnt{color:#94a3b8}
.download-card .url{color:#94a3b8}
/* A13: resting (non-hover) affordance for the card delete control */
.item-video .btn-del{color:#cbd5e1}
/* V4: distinguish sort pills from tag-filter chips */
.sort-pill{border-radius:6px}
/* L5: action-bar items keep natural height (no vertical stretch) */
.action-bar{align-items:flex-start}
/* L6: keep the tag dropdown inside the viewport */
.tag-dropdown{max-width:calc(100vw - 24px)}
/* P5: don't let the position counter grab the controls row */
#now-playing{flex:0 1 auto}
/* T2/T3: separate and visually flag the destructive bulk delete */
.tag-delete-all{flex-basis:100%;margin-top:6px}
.tag-delete-all .btn-del{display:inline-flex;align-items:center;gap:6px;color:#fca5a5;background:rgba(153,27,27,.18);border:1px solid #b91c1c;border-radius:6px;padding:8px 12px;min-height:44px}
.tag-delete-all .btn-del:hover{background:#7f1d1d;color:#fff}
#delete-form{margin-left:12px}
/* T1: real hit area for the suggested-tag remove control */
.video-tag-remove{display:inline-flex;align-items:center;justify-content:center;min-width:24px;min-height:24px;padding:4px;margin:-2px -4px -2px 0;font-size:14px;opacity:.85}
/* U3/U4/U5: state-page polish */
#transcode-notice{display:flex;align-items:flex-start;gap:8px}
#transcode-notice .tn-close{margin-left:auto;background:none;border:none;color:#fde68a;font-size:18px;cursor:pointer;line-height:1;padding:0 6px}
.unlock-error{font-size:14px}
.notice{background:#16213e;color:#cbd5e1;border-bottom:1px solid #2d2d44;padding:10px 16px;font-size:13px;display:flex;align-items:center;gap:8px}
.error-back{margin-top:20px;display:flex;justify-content:center}
/* A10: respect reduced-motion */
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:.001ms!important;animation-iteration-count:1!important;transition-duration:.001ms!important;scroll-behavior:auto!important}}
/* T1: >=44px touch targets + consistent gutters on phones */
@media(max-width:640px){
  .btn,.btn-skip,.speed-select,.sort-pill,.tag-pill,.tag-clear,.btn-star{min-height:44px}
  .sort-pill,.tag-pill{padding-top:10px;padding-bottom:10px}
  .tag-clear{padding:11px 8px}
  .btn-skip{padding:8px 12px}
  .btn-star{min-width:44px}
  .tag-search,.video-tag-add,.download-form input{min-height:44px}
  .tag-dropdown a{padding:10px 12px}
  .item-video .btn-del{min-width:40px;min-height:40px;display:flex;align-items:center;justify-content:center}
  nav,.tag-filter,#file-list,.download-board{padding-left:12px;padding-right:12px}
}
/* P3: in landscape, don't trap the controls under a viewport-filling video */
@media(orientation:landscape) and (max-height:520px){
  nav,#player-area{position:static}
  video{max-height:68vh}
  #controls{padding-top:4px;padding-bottom:4px;gap:6px}
  .btn,.btn-skip,.speed-select,.btn-star{min-height:36px}
}
/* P9: legible 2-line titles for the touch-navigation lists; clearer current item */
.related-list .item-name,.playlist-name{white-space:normal;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;line-height:1.25}
.playlist-item.playing{background:#241b4d}
.playlist-item.playing .playlist-name{color:#c4b5fd;font-weight:600}
/* T1: bring the last sub-floor controls up to a 44px touch target on phones */
@media(max-width:640px){
  .crumb{min-height:44px;display:inline-flex;align-items:center}
  .tag-dropdown a{min-height:44px;align-items:center}
}
/* V1: tighten the type scale — no sub-12px text, drop the stray 15px step */
.crumb,.empty{font-size:16px}
.item-tags,.tag-pill-count,.tag-dropdown a .cnt,.tag-pill-x{font-size:12px}
"""


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
    # Build (label, href) for each segment, then collapse the middle on deep
    # paths so the sticky breadcrumb stays compact (and doesn't wrap on phones).
    segs = []
    acc = ''
    for part in parts:
        acc += ('/' if acc else '') + part
        segs.append((part, url_for_browse(acc)))
    if len(segs) > 4:
        ellipsis_href = segs[-3][1]
        segs = [('…', ellipsis_href)] + segs[-2:]
    last = len(segs) - 1
    for i, (label, href) in enumerate(segs):
        pieces.append('<span class="crumb-sep" aria-hidden="true">/</span>')
        cur = ' aria-current="page"' if i == last else ''
        pieces.append(f'<a class="crumb" href="{esc(href)}"{cur}>{esc(label)}</a>')
    pieces.append('<span class="nav-spacer"></span>')
    if _config.get('allow_download'):
        pieces.append('<a class="btn" href="/download">\u2B07 Downloads</a>')
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
    return '<nav aria-label="Breadcrumb">' + ''.join(pieces) + '</nav>'


def _render_train_btn(path_param, is_busy):
    cls = 'btn btn-train busy' if is_busy else 'btn btn-train'
    disabled = ' disabled' if is_busy else ''
    label = '\U0001F9E0 Training\u2026' if is_busy else '\U0001F9E0 Train'
    return (
        f'<form hx-post="/train" style="display:inline" id="train-form">'
        f'<input type="hidden" name="path" value="{path_param}">'
        f'<button class="{cls}"{disabled} hx-disabled-elt="this">'
        f'<span class="btn-spinner"></span>'
        f'<span class="btn-label">{label}</span>'
        f'</button>'
        f'</form>'
    )


def render_file_list(data, current_idx=-1, show_shuffle=True, tags_map=None, selected_tags=None, sort='name', direction='asc', starred_only=False):
    pieces = ['<div id="file-list">']

    shuffle_btn = ''
    if show_shuffle and data['videos']:
        shuffle_params = {'path': data['path'], 'shuffle': '1'}
        if selected_tags:
            shuffle_params['tags'] = ','.join(selected_tags)
        if sort and sort != 'name':
            shuffle_params['sort'] = sort
        if direction and direction != 'asc':
            shuffle_params['dir'] = direction
        if starred_only:
            shuffle_params['starred'] = '1'
        shuffle_url = '/play?' + urllib.parse.urlencode(shuffle_params)
        shuffle_btn = f'<a class="btn" href="{esc(shuffle_url)}">\u21C5 Shuffle Play</a>'
    want_action_bar = bool(shuffle_btn) or (
        _config.get('allow_download') and (data['videos'] or data['dirs'])
    )
    if want_action_bar:
        tag_html = ''
        if data['videos'] and _config['allow_tag'] and _config['has_ffmpeg']:
            path_param = esc(data['path'])
            # Check if model exists for this directory
            from simpleparty.tagger import model_path as _model_path
            resolved_dir = resolve_path(_config.get('root', '.'), data['path'])
            has_model = _model_path(resolved_dir).exists() if resolved_dir.is_dir() else False
            resolved_str = str(resolved_dir)
            job = _config['tag_jobs'].get(resolved_str)
            is_busy = bool(job and job.get('running'))
            tag_html = _render_train_btn(path_param, is_busy)
            if has_model:
                tag_html += (
                    f'<form hx-post="/suggest" style="display:inline">'
                    f'<input type="hidden" name="path" value="{path_param}">'
                    f'<button class="btn">\U0001F3F7 Suggest tags</button>'
                    f'</form>'
                )
            # Show "Confirm all" if there are suggested tags
            if tags_map:
                has_suggested = any(
                    e.get('status') == 'suggested' for e in tags_map.values()
                )
                if has_suggested:
                    tag_html += (
                        f'<form hx-post="/confirm-all" style="display:inline">'
                        f'<input type="hidden" name="path" value="{path_param}">'
                        f'<button class="btn btn-confirm">\u2714 Confirm all</button>'
                        f'</form>'
                    )
            status_url = f'/tag-status?{urllib.parse.urlencode({"path": data["path"]})}'
            poll = 'every 2s' if is_busy else 'every 10s'
            tag_html += (
                f'<div hx-get="{status_url}" '
                f'hx-trigger="load,{poll}" hx-swap="outerHTML" '
                f'class="tag-progress-panel{" active" if is_busy else ""}" '
                f'role="status" aria-live="polite" id="tag-progress"></div>'
            )
        download_html = ''
        if _config['allow_download']:
            path_q = urllib.parse.urlencode({'path': data['path']})
            download_html = (
                f'<details class="download-details">'
                f'<summary class="btn">\u2B07 Download URL</summary>'
                f'<div style="flex-basis:100%">{render_download_form(data["path"], autofocus=False)}</div>'
                f'</details>'
                f'<a class="btn" href="/download">Manage</a>'
                f'<div hx-get="/download-status?{path_q}&inline=1" hx-trigger="load" '
                f'hx-swap="outerHTML" class="download-progress-panel" '
                f'role="status" aria-live="polite" '
                f'id="download-progress"></div>'
            )
        sort_html = render_sort_pills(data['path'], selected_tags, sort, direction, starred_only=starred_only) if data['videos'] else ''
        pieces.append(
            f'<div class="action-bar">'
            f'{shuffle_btn}'
            f'{sort_html}'
            f'{tag_html}'
            f'{download_html}'
            f'</div>'
        )

    for d in data['dirs']:
        if d['encrypted'] and not d['unlocked']:
            icon = '\U0001F512'
            state = ' <span class="visually-hidden">(encrypted, locked)</span>'
        elif d['encrypted']:
            icon = '\U0001F513'
            state = ' <span class="visually-hidden">(encrypted, unlocked)</span>'
        else:
            icon = '\U0001F4C1'
            state = ''
        pieces.append(
            f'<a class="item" href="{esc(url_for_browse(d["path"]))}">'
            f'<span class="item-icon" aria-hidden="true">{icon}</span>'
            f'<span class="item-name">{esc(d["name"])}{state}</span>'
            f'</a>'
        )

    from simpleparty.tagger import thumb_path
    root_dir = _config.get('root', '.')
    for i, v in enumerate(data['videos']):
        cls = ' playing' if i == current_idx else ''
        play_url = url_for_play(data['path'], i, tags=selected_tags, video=v['name'], sort=sort, direction=direction, starred=starred_only)
        resolved_dir = resolve_path(root_dir, data['path'])
        has_thumb = thumb_path(str(resolved_dir), v['name']).exists()
        pieces.append(f'<div class="item item-video{cls}">')
        thumb_url = f'/thumb/{urllib.parse.quote(v["path"])}'
        if has_thumb:
            thumb_html = f'<img src="{thumb_url}" loading="lazy" class="item-thumb" alt="">'
        else:
            thumb_html = '<div class="item-thumb item-thumb-placeholder" aria-hidden="true">\U0001F3AC</div>'
        pieces.append(
            f'<a class="item-link" href="{esc(play_url)}">'
            f'{thumb_html}'
            f'<span class="item-info">'
            f'<span class="item-name">{esc(v["name"])}</span>'
            f'<span class="item-size">{fmt_size(v["size"])}</span>'
            f'</span>'
            f'</a>'
        )
        if _config['allow_delete']:
            pieces.append(
                f'<form hx-post="/delete" hx-target="closest .item" hx-swap="delete" '
                f'hx-confirm="Delete {esc(v["name"])}?">'
                f'<input type="hidden" name="path" value="{esc(v["path"])}">'
                f'<button type="submit" class="btn-del" title="Delete" '
                f'aria-label="Delete {esc(v["name"])}">'
                f'<span aria-hidden="true">\U0001F5D1</span></button>'
                f'</form>'
            )
        if tags_map and v['name'] in tags_map:
            entry = tags_map[v['name']]
            video_tags = entry.get('tags', [])
            if video_tags:
                is_suggested = entry.get('status') == 'suggested'
                tags_text = esc(' \u00B7 '.join(video_tags[:8]))
                cls = ' suggested' if is_suggested else ''
                prefix = (
                    '<span class="visually-hidden">Suggested tags: </span>'
                    '<span aria-hidden="true">\u2753\u2009</span>'
                ) if is_suggested else ''
                pieces.append(f'<div class="item-tags{cls}">{prefix}{tags_text}</div>')
        pieces.append('</div>')

    if not data['dirs'] and not data['videos']:
        pieces.append('<div class="empty">Empty directory</div>')

    pieces.append('</div>')
    return ''.join(pieces)


def render_related_videos(data, idx, tags_map, selected_tags=None, sort='name', direction='asc', starred_only=False):
    """Render a 'Related Videos' section based on tag overlap."""
    related = _compute_related_videos(data, idx, tags_map)
    if not related:
        return ''
    from simpleparty.tagger import thumb_path
    root_dir = _config.get('root', '.')
    pieces = [
        '<div id="related-videos">'
        '<h2 class="related-heading">Related Videos</h2>'
        '<div class="related-list">'
    ]
    for video_idx, _overlap in related:
        v = data['videos'][video_idx]
        play_url = url_for_play(data['path'], video_idx, tags=selected_tags, video=v['name'], sort=sort, direction=direction, starred=starred_only)
        resolved_dir = resolve_path(root_dir, data['path'])
        has_thumb = thumb_path(str(resolved_dir), v['name']).exists()
        thumb_url = f'/thumb/{urllib.parse.quote(v["path"])}'
        if has_thumb:
            thumb_html = f'<img src="{thumb_url}" loading="lazy" class="item-thumb" alt="">'
        else:
            thumb_html = '<div class="item-thumb item-thumb-placeholder" aria-hidden="true">\U0001F3AC</div>'
        pieces.append(
            f'<div class="item item-video">'
            f'<a class="item-link" href="{esc(play_url)}">'
            f'{thumb_html}'
            f'<span class="item-info">'
            f'<span class="item-name">{esc(v["name"])}</span>'
            f'</span>'
            f'</a>'
            f'</div>'
        )
    pieces.append('</div></div>')
    return ''.join(pieces)


def render_playlist(data, current_idx, play_order, shuffle_seed, selected_tags=None, sort='name', direction='asc', starred_only=False):
    """Render playlist showing videos in playback order with current highlighted."""
    from simpleparty.tagger import thumb_path
    root_dir = _config.get('root', '.')
    n = len(data['videos'])
    is_shuffled = play_order is not None

    # Build ordered list: (video_index, playlist_position)
    # Starting from current position, wrapping around
    if is_shuffled:
        # Find current position in shuffle order
        current_pos = play_order.index(current_idx)
        ordered = [(play_order[(current_pos + i) % n], i) for i in range(n)]
    else:
        ordered = [((current_idx + i) % n, i) for i in range(n)]

    pieces = [
        '<div class="playlist">'
        '<h2 class="playlist-heading">Playlist</h2>'
        '<div class="playlist-items">'
    ]

    for video_idx, offset in ordered:
        v = data['videos'][video_idx]
        is_current = (offset == 0)

        if is_shuffled:
            pos_in_order = (current_pos + offset) % n
            play_url = url_for_play(data['path'], video_idx, shuffle=True, seed=shuffle_seed, pos=pos_in_order, tags=selected_tags, video=v['name'], sort=sort, direction=direction, starred=starred_only)
        else:
            play_url = url_for_play(data['path'], video_idx, tags=selected_tags, video=v['name'], sort=sort, direction=direction, starred=starred_only)

        resolved_dir = resolve_path(root_dir, data['path'])
        has_thumb = thumb_path(str(resolved_dir), v['name']).exists()
        thumb_url = f'/thumb/{urllib.parse.quote(v["path"])}'

        cls = ' playing' if is_current else ''
        if has_thumb:
            thumb_html = f'<img src="{thumb_url}" loading="lazy" class="playlist-thumb" alt="">'
        else:
            thumb_html = '<div class="playlist-thumb-placeholder" aria-hidden="true">\U0001F3AC</div>'

        label = '\u25B6 Now' if is_current else str(offset)
        pieces.append(
            f'<a class="playlist-item{cls}" href="{esc(play_url)}">'
            f'{thumb_html}'
            f'<span class="playlist-name">{esc(v["name"])}</span>'
            f'<span class="playlist-pos">{label}</span>'
            f'</a>'
        )

    pieces.append('</div></div>')
    return ''.join(pieces)


def _compute_viable_tags(tags_map, selected_tags):
    """Return set of lowercased tags that can be added without producing zero results."""
    selected_lower = {t.lower() for t in selected_tags} if selected_tags else set()
    viable = set()
    for video_data in tags_map.values():
        vtags = {t.lower().strip() for t in video_data.get('tags', [])}
        if selected_lower <= vtags:
            viable |= vtags
    # Remove already-selected tags from viable set
    viable -= selected_lower
    return viable


def _hx_browse(href):
    """htmx attrs that swap only the browse content (sort/filter without a full
    page reload, preserving scroll), with the href kept as a no-JS fallback."""
    return (
        f'hx-get="{esc(href)}" hx-target="#browse-content" '
        f'hx-select="#browse-content" hx-swap="outerHTML" hx-push-url="true"'
    )


def render_sort_pills(path, selected_tags, sort, direction, starred_only=False):
    """Render three sort pills (Name/Size/Date). Active pill shows direction arrow;
    clicking the active pill flips direction, clicking an inactive one switches
    to that field at a sensible default direction."""
    fields = [
        ('name', 'Name', 'asc'),
        ('size', 'Size', 'desc'),
        ('date', 'Date', 'desc'),
        ('length', 'Length', 'desc'),
    ]
    pieces = ['<div class="sort-pills">']
    for field, label, default_dir in fields:
        active = field == sort
        if active:
            new_dir = 'desc' if direction == 'asc' else 'asc'
            arrow = ' ▲' if direction == 'asc' else ' ▼'
        else:
            new_dir = default_dir
            arrow = ''
        href = url_for_browse(path, tags=selected_tags, sort=field, direction=new_dir, starred=starred_only)
        cls = 'sort-pill' + (' active' if active else '')
        aria = ' aria-current="true"' if active else ''
        pieces.append(f'<a class="{cls}" href="{esc(href)}" {_hx_browse(href)}{aria}>{label}{arrow}</a>')
    pieces.append('</div>')
    return ''.join(pieces)


def render_tag_filter(tags_map, selected_tags, path, filtered_count=None, starred_only=False):
    """Render tag filter: selected pills + searchable dropdown of viable tags."""
    if not tags_map:
        return ''
    # Count all tags
    counts = {}
    for video_data in tags_map.values():
        for tag in video_data.get('tags', []):
            key = tag.lower().strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    has_starred = any(e.get('starred') for e in tags_map.values())
    if not counts and not has_starred:
        return ''

    selected_lower = {t.lower() for t in selected_tags} if selected_tags else set()
    viable = _compute_viable_tags(tags_map, selected_tags)

    pieces = ['<div class="tag-filter">']

    # Star filter pill (if any starred videos exist in this directory)
    if has_starred:
        if starred_only:
            href = url_for_browse(path, tags=selected_tags, starred=False)
            pieces.append(
                f'<a class="tag-pill star-pill active" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Showing starred only — show all videos" '
                f'title="Show all videos">'
                f'★ Starred only <span class="tag-pill-x" aria-hidden="true">×</span></a>'
            )
        else:
            href = url_for_browse(path, tags=selected_tags, starred=True)
            pieces.append(
                f'<a class="tag-pill star-pill" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Show only starred videos" title="Show only starred videos">★ Starred only</a>'
            )

    # Selected tag pills
    if selected_tags:
        pieces.append('<div class="tag-selected-pills">')
        for tag in selected_tags:
            remove_tags = [t for t in selected_tags if t.lower() != tag.lower()]
            href = url_for_browse(path, tags=remove_tags if remove_tags else None, starred=starred_only)
            pieces.append(
                f'<a class="tag-pill active" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Remove filter {esc(tag)}">'
                f'{esc(tag)} <span class="tag-pill-x" aria-hidden="true">\u00d7</span></a>'
            )
        clear_href = url_for_browse(path, starred=starred_only)
        pieces.append(
            f'<a class="tag-clear" href="{esc(clear_href)}" {_hx_browse(clear_href)}>Clear all</a>'
        )
        if _config.get('allow_delete') and filtered_count:
            tags_csv = ','.join(selected_tags)
            tags_label = ', '.join(selected_tags)
            confirm = (
                f'Delete all {filtered_count} video'
                f'{"" if filtered_count == 1 else "s"} tagged "{tags_label}"? '
                'This cannot be undone.'
            )
            pieces.append(
                f'<form class="tag-delete-all" hx-post="/delete-by-tag" '
                f'hx-confirm="{esc(confirm)}" style="display:inline">'
                f'<input type="hidden" name="path" value="{esc(path)}">'
                f'<input type="hidden" name="tags" value="{esc(tags_csv)}">'
                f'<button type="submit" class="btn-del" '
                f'title="Delete all videos with these tags">'
                f'<span aria-hidden="true">\U0001F5D1</span> Delete all ({filtered_count})</button>'
                f'</form>'
            )
        pieces.append('</div>')

    # Search input + dropdown
    viable_sorted = sorted(
        [(tag, counts[tag]) for tag in viable if tag in counts],
        key=lambda x: (-x[1], x[0]),
    )

    if viable_sorted:
        pieces.append('<div class="tag-dropdown-wrap">')
        pieces.append(
            '<input type="text" id="tag-search" class="tag-search" '
            'placeholder="Filter by tag\u2026" autocomplete="off" '
            'role="combobox" aria-expanded="false" aria-controls="tag-dropdown" '
            'aria-autocomplete="list" aria-label="Filter videos by tag">'
        )
        pieces.append('<div class="tag-dropdown" id="tag-dropdown" role="listbox">')
        for oi, (tag, count) in enumerate(viable_sorted):
            new_tags = list(selected_tags or []) + [tag]
            href = url_for_browse(path, tags=new_tags, starred=starred_only)
            cnt = f' <span class="cnt">({count})</span>' if count > 1 else ''
            pieces.append(f'<a href="{esc(href)}" {_hx_browse(href)} role="option" id="tagopt-{oi}">{esc(tag)}{cnt}</a>')
        pieces.append('</div></div>')

        # JS: keyboard-operable combobox (filter, arrow/enter/escape, outside-click)
        pieces.append(
            '<script>'
            '(function(){'
            'var s=document.getElementById("tag-search"),'
            'd=document.getElementById("tag-dropdown");'
            'if(!s||s.dataset.bound)return;s.dataset.bound="1";'
            'var items=Array.prototype.slice.call(d.querySelectorAll("a"));'
            'var empty=document.createElement("div");empty.textContent="No matching tags";'
            'empty.setAttribute("role","presentation");'
            'empty.style.cssText="padding:10px 12px;color:#94a3b8;font-size:12px";empty.hidden=true;'
            'd.appendChild(empty);var hi=-1;'
            'function vis(){return items.filter(function(a){return a.style.display!=="none"})}'
            'function paint(){items.forEach(function(a){a.style.background=""});'
            'var v=vis();if(hi>=0&&hi<v.length){v[hi].style.background="#2d2d44";'
            'v[hi].scrollIntoView({block:"nearest"});s.setAttribute("aria-activedescendant",v[hi].id)}'
            'else{s.removeAttribute("aria-activedescendant")}}'
            'function open(){d.classList.add("open");s.setAttribute("aria-expanded","true")}'
            'function close(){d.classList.remove("open");s.setAttribute("aria-expanded","false");hi=-1;paint()}'
            's.addEventListener("focus",open);'
            's.addEventListener("input",function(){'
            'var q=this.value.toLowerCase();'
            'items.forEach(function(a){a.style.display=a.textContent.toLowerCase().includes(q)?"":"none"});'
            'hi=-1;empty.hidden=vis().length>0;open();paint()});'
            's.addEventListener("keydown",function(e){var v=vis();'
            'if(e.key==="ArrowDown"){e.preventDefault();open();hi=Math.min(v.length-1,hi+1);paint()}'
            'else if(e.key==="ArrowUp"){e.preventDefault();hi=Math.max(0,hi-1);paint()}'
            'else if(e.key==="Enter"&&hi>=0&&v[hi]){e.preventDefault();v[hi].click()}'
            'else if(e.key==="Escape"){close();s.blur()}});'
            '})();'
            # Register the outside-click closer ONCE (survives #browse-content swaps)
            'if(!window.__spTagOutside){window.__spTagOutside=1;'
            'document.addEventListener("click",function(e){'
            'if(!e.target.closest(".tag-dropdown-wrap")){'
            'var dd=document.getElementById("tag-dropdown"),ts=document.getElementById("tag-search");'
            'if(dd)dd.classList.remove("open");if(ts)ts.setAttribute("aria-expanded","false")}});}'
            '</script>'
        )

    pieces.append('</div>')
    return ''.join(pieces)


def render_browse_page(data, tags_map=None, selected_tags=None, sort='name', direction='asc', starred_only=False):
    title = f'SimpleParty \u2014 {data["path"].split("/")[-1]}' if data['path'] else 'SimpleParty'
    heading = data['path'].split('/')[-1] if data['path'] else 'Library'
    body = render_nav(data['path'], data.get('encryptedDir'))
    body += '<main id="main">'
    body += f'<h1 class="visually-hidden">{esc(heading)}</h1>'
    if not _config.get('has_ffmpeg'):
        body += (
            '<div class="notice" role="status">'
            'ffmpeg not found: video thumbnails and AI tagging are disabled. '
            'Install ffmpeg to enable them.</div>'
        )
    body += '<div id="browse-content">'
    body += render_tag_filter(
        tags_map, selected_tags, data['path'],
        filtered_count=len(data['videos']),
        starred_only=starred_only,
    )
    body += render_file_list(
        data, tags_map=tags_map, selected_tags=selected_tags,
        sort=sort, direction=direction, starred_only=starred_only,
    )
    body += '</div></main>'
    return render_page(title, body)


def render_locked_page(path, encrypted_dir, redirect_path=None, error=None):
    body = render_nav(path)
    dir_name = encrypted_dir.split('/')[-1] if encrypted_dir else 'directory'
    redir = redirect_path or path
    parent = str(Path(path).parent) if '/' in path else ''
    if parent == '.':
        parent = ''
    body += (
        f'<main id="main"><div class="unlock-box">'
        f'<h1>Unlock {esc(dir_name)}</h1>'
        f'<form hx-post="/unlock" hx-target="#unlock-error" hx-swap="innerHTML">'
        f'<input type="hidden" name="path" value="{esc(encrypted_dir)}">'
        f'<input type="hidden" name="redirect" value="{esc(url_for_browse(redir))}">'
        f'<input type="password" name="passphrase" placeholder="Passphrase" '
        f'aria-label="Passphrase" autofocus>'
        f'<div id="unlock-error" class="unlock-error" role="alert">{esc(error) if error else ""}</div>'
        f'<div class="unlock-actions">'
        f'<a class="btn" href="{esc(url_for_browse(parent))}">Cancel</a>'
        f'<button class="btn active" type="submit">Unlock</button>'
        f'</div></form></div></main>'
    )
    return render_page('SimpleParty \u2014 Unlock', body)


def render_error_page(path, error):
    body = render_nav(path)
    body += (
        f'<main id="main"><div class="unlock-box" style="text-align:center" role="alert">'
        f'<h1>Something went wrong</h1>'
        f'<p style="color:#f87171;margin-top:8px">{esc(error)}</p>'
        f'<div class="error-back"><a class="btn" href="/">\u2190 Back to library</a></div>'
        f'</div></main>'
    )
    return render_page('SimpleParty \u2014 Error', body)


def render_video_tags_inline(rel_path, video_name, tags_list, status='confirmed'):
    """Render tag pills with inline add/remove for the video play page."""
    is_suggested = status == 'suggested'
    pieces = ['<div class="video-tag-pills">']

    if is_suggested:
        pieces.append(
            '<span class="visually-hidden">Suggested (unconfirmed) tags — accept or reject:</span> '
            f'<form hx-post="/confirm-tags" hx-target="#video-meta" hx-swap="innerHTML" '
            f'style="display:inline;margin:0;padding:0">'
            f'<input type="hidden" name="path" value="{esc(rel_path)}">'
            f'<input type="hidden" name="video" value="{esc(video_name)}">'
            f'<button type="submit" class="btn btn-confirm" title="Accept suggested tags">'
            f'\u2714 Accept</button></form> '
            f'<form hx-post="/reject-tags" hx-target="#video-meta" hx-swap="innerHTML" '
            f'style="display:inline;margin:0;padding:0">'
            f'<input type="hidden" name="path" value="{esc(rel_path)}">'
            f'<input type="hidden" name="video" value="{esc(video_name)}">'
            f'<button type="submit" class="btn btn-reject" title="Reject suggested tags">'
            f'\u2718 Reject</button></form> '
        )

    pill_class = 'video-tag-pill suggested' if is_suggested else 'video-tag-pill'
    for i, tag in enumerate(tags_list):
        if is_suggested:
            pieces.append(
                f'<span class="{pill_class}">{esc(tag)}'
                f'<form hx-post="/reject-tag" hx-target="#video-meta" hx-swap="innerHTML" '
                f'style="display:inline;margin:0;padding:0">'
                f'<input type="hidden" name="path" value="{esc(rel_path)}">'
                f'<input type="hidden" name="video" value="{esc(video_name)}">'
                f'<input type="hidden" name="tag" value="{esc(tag)}">'
                f'<button type="submit" class="video-tag-remove" title="Reject tag" '
                f'aria-label="Reject suggested tag {esc(tag)}">'
                f'<span aria-hidden="true">\u00d7</span></button>'
                f'</form></span>'
            )
        else:
            remaining = ', '.join(t for j, t in enumerate(tags_list) if j != i)
            pieces.append(
                f'<span class="{pill_class}">{esc(tag)}'
                f'<form hx-post="/save-tags" hx-target="#video-meta" hx-swap="innerHTML" '
                f'style="display:inline;margin:0;padding:0">'
                f'<input type="hidden" name="path" value="{esc(rel_path)}">'
                f'<input type="hidden" name="video" value="{esc(video_name)}">'
                f'<input type="hidden" name="tags" value="{esc(remaining)}">'
                f'<button type="submit" class="video-tag-remove" title="Remove tag" '
                f'aria-label="Remove tag {esc(tag)}">'
                f'<span aria-hidden="true">\u00d7</span></button>'
                f'</form></span>'
            )
    # Inline add input
    all_csv = ', '.join(tags_list)
    prefix = (all_csv + ', ') if all_csv else ''
    pieces.append(
        f'<form hx-post="/save-tags" hx-target="#video-meta" hx-swap="innerHTML" '
        f'style="display:inline;margin:0;padding:0" data-prefix="{esc(prefix)}" '
        f'onsubmit="var f=this,i=f.querySelector(&quot;.video-tag-add&quot;);'
        f'f.querySelector(&quot;[name=tags]&quot;).value=f.dataset.prefix+i.value;return true">'
        f'<input type="hidden" name="path" value="{esc(rel_path)}">'
        f'<input type="hidden" name="video" value="{esc(video_name)}">'
        f'<input type="hidden" name="tags" value="">'
        f'<input type="text" class="video-tag-add" placeholder="add tag\u2026" aria-label="Add a tag">'
        f'</form>'
    )
    pieces.append('</div>')
    return ''.join(pieces)


def render_play_page(data, idx, next_url, prev_url, shuffle_url, is_shuffled, pos_info, tags_map=None, selected_tags=None, play_order=None, shuffle_seed=None, transcode_plan=None, sort='name', direction='asc', starred_only=False):
    v = data['videos'][idx]
    video_src = url_for_video(v['path'])
    browse_url = url_for_browse(data['path'], tags=selected_tags, sort=sort, direction=direction, starred=starred_only)

    body = render_nav(data['path'], data.get('encryptedDir'))
    body += '<main id="main">'
    if transcode_plan == 'reencode':
        body += (
            '<div id="transcode-notice" role="status">'
            '<span>\u2699 Re-encoding this video in real time (source codec not '
            'supported by your browser); start-up and seeking may be slower.</span>'
            '<button type="button" class="tn-close" aria-label="Dismiss notice" '
            'onclick="this.parentNode.remove()">\u00d7</button>'
            '</div>'
        )
    body += (
        f'<div id="player-area">'
        f'<video id="video" src="{esc(video_src)}" controls playsinline autoplay></video>'
        f'<div id="video-overlay" role="status" aria-live="polite"></div>'
        f'<h1 id="video-title">{esc(v["name"])}</h1>'
        f'</div>'
        f'<div id="controls">'
        f'<a class="btn" href="{esc(prev_url)}" title="Previous (p)">\u25C0 Prev</a>'
        f'<div class="skip-group">'
        f'<button class="btn btn-skip" onclick="skip(-30)" title="Back 30s (J)">-30s</button>'
        f'<button class="btn btn-skip" onclick="skip(-10)" title="Back 10s (j)">-10s</button>'
        f'<button class="btn btn-skip" onclick="skip(10)" title="Forward 10s (l)">+10s</button>'
        f'<button class="btn btn-skip" onclick="skip(30)" title="Forward 30s (L)">+30s</button>'
        f'</div>'
        f'<span id="now-playing">{pos_info}</span>'
        f'<a class="btn" href="{esc(next_url)}" title="Next (n)">Next \u25B6</a>'
        f'<select id="speed-select" class="speed-select" onchange="setSpeed(this.value)" aria-label="Playback speed" title="Speed (&lt; &gt;)">'
        f'<option value="0.5">0.5x</option>'
        f'<option value="0.75">0.75x</option>'
        f'<option value="1" selected>1x</option>'
        f'<option value="1.25">1.25x</option>'
        f'<option value="1.5">1.5x</option>'
        f'<option value="2">2x</option>'
        f'<option value="3">3x</option>'
        f'</select>'
        f'<a class="btn{" active" if is_shuffled else ""}" '
        f'href="{esc(shuffle_url)}" title="Shuffle (s)">\u21C5 Shuffle</a>'
        f'<button id="btn-autoplay" class="btn" title="Autoplay (a)" aria-pressed="false">Autoplay</button>'
        f'<button id="btn-repeat" class="btn" title="Repeat (r)" aria-pressed="false">Repeat</button>'
    )
    if _config['allow_tag']:
        is_video_starred = bool(tags_map and tags_map.get(v['name'], {}).get('starred'))
        body += (
            f'<button id="btn-star" type="button" class="btn btn-star{" active" if is_video_starred else ""}" '
            f'data-starred="{"1" if is_video_starred else "0"}" '
            f'aria-pressed="{"true" if is_video_starred else "false"}" '
            f'aria-label="Star this video" '
            f'data-dir="{esc(data["path"])}" data-video="{esc(v["name"])}" '
            f'title="Star this video">'
            f'<span class="star-icon" aria-hidden="true">{"★" if is_video_starred else "☆"}</span></button>'
        )
    if _config['allow_delete']:
        body += (
            f'<form id="delete-form" hx-post="/delete" hx-confirm="Delete {esc(v["name"])}?">'
            f'<input type="hidden" name="path" value="{esc(v["path"])}">'
            f'<input type="hidden" name="redirect" value="{esc(browse_url)}">'
            f'<button type="submit" class="btn btn-lock" title="Delete (d)" '
            f'aria-label="Delete {esc(v["name"])}">'
            f'<span aria-hidden="true">\U0001F5D1</span></button></form>'
        )
    body += '</div>'

    if _config['allow_tag']:
        video_entry = tags_map.get(v['name'], {}) if tags_map else {}
        video_tags = video_entry.get('tags', [])
        video_status = video_entry.get('status', 'confirmed')
        meta_html = render_video_tags_inline(data['path'], v['name'], video_tags, status=video_status)
        if not video_tags or video_status in ('suggested', 'rejected'):
            from simpleparty.tagger import model_path as _model_path
            resolved_dir = resolve_path(_config.get('root', '.'), data['path'])
            if _model_path(resolved_dir).exists():
                meta_html += (
                    f'<form hx-post="/suggest-one" hx-target="#video-meta" '
                    f'hx-swap="innerHTML" style="display:inline">'
                    f'<input type="hidden" name="path" value="{esc(data["path"])}">'
                    f'<input type="hidden" name="video" value="{esc(v["name"])}">'
                    f'<button class="btn">\U0001F3F7 Suggest tags</button>'
                    f'</form>'
                )
        body += f'<div class="video-meta" id="video-meta">{meta_html}</div>'

    if tags_map:
        body += render_related_videos(data, idx, tags_map, selected_tags=selected_tags, sort=sort, direction=direction, starred_only=starred_only)

    body += render_playlist(data, idx, play_order, shuffle_seed, selected_tags=selected_tags, sort=sort, direction=direction, starred_only=starred_only)
    body += '</main>'

    body += (
        '<script>\n'
        f'const video=document.getElementById("video");\n'
        f'const nextUrl={json.dumps(next_url)};\n'
        f'const prevUrl={json.dumps(prev_url)};\n'
        f'const browseUrl={json.dumps(browse_url)};\n'
        'const overlay=document.getElementById("video-overlay");\n'
        'const speedSel=document.getElementById("speed-select");\n'
        'const speeds=[0.5,0.75,1,1.25,1.5,2,3];\n'
        'let overlayTimer;\n'
        'function flash(txt){overlay.textContent=txt;overlay.style.opacity="1";clearTimeout(overlayTimer);overlayTimer=setTimeout(()=>{overlay.style.opacity="0"},600)}\n'
        'function skip(s){video.currentTime=Math.max(0,Math.min(video.duration||0,video.currentTime+s));flash((s>0?"+":"")+s+"s")}\n'
        'function setSpeed(v){v=parseFloat(v);video.playbackRate=v;speedSel.value=v;flash(v+"x")}\n'
        'function cycleSpeed(dir){const i=speeds.indexOf(video.playbackRate);const ni=Math.max(0,Math.min(speeds.length-1,i+dir));setSpeed(speeds[ni])}\n'
        'let autoplay=localStorage.getItem("sp-autoplay")!=="false";\n'
        'let repeat=localStorage.getItem("sp-repeat")||"off";\n'
        'const btnAuto=document.getElementById("btn-autoplay");\n'
        'const btnRepeat=document.getElementById("btn-repeat");\n'
        'function updateAutoBtn(){btnAuto.classList.toggle("active",autoplay);btnAuto.textContent=autoplay?"Autoplay: On":"Autoplay: Off";btnAuto.setAttribute("aria-pressed",autoplay?"true":"false")}\n'
        'function updateRepeatBtn(){var on=repeat!=="off";btnRepeat.classList.toggle("active",on);btnRepeat.textContent=repeat==="one"?"Repeat: One":repeat==="all"?"Repeat: All":"Repeat: Off";btnRepeat.setAttribute("aria-pressed",on?"true":"false")}\n'
        'btnAuto.addEventListener("click",()=>{autoplay=!autoplay;localStorage.setItem("sp-autoplay",autoplay);updateAutoBtn();flash(autoplay?"Autoplay on":"Autoplay off")});\n'
        'btnRepeat.addEventListener("click",()=>{repeat=repeat==="off"?"all":repeat==="all"?"one":"off";localStorage.setItem("sp-repeat",repeat);updateRepeatBtn();flash(repeat==="off"?"Repeat off":repeat==="one"?"Repeat one":"Repeat all")});\n'
        'updateAutoBtn();updateRepeatBtn();\n'
        'const btnStar=document.getElementById("btn-star");\n'
        'if(btnStar){btnStar.addEventListener("click",async()=>{\n'
        '  const cur=btnStar.dataset.starred==="1";const next=!cur;\n'
        '  const fd=new FormData();fd.set("dir",btnStar.dataset.dir);fd.set("name",btnStar.dataset.video);fd.set("starred",next?"1":"0");\n'
        '  btnStar.disabled=true;\n'
        '  try{const r=await fetch("/star-update",{method:"POST",body:new URLSearchParams(fd)});\n'
        '    if(!r.ok)throw new Error("HTTP "+r.status);\n'
        '    btnStar.dataset.starred=next?"1":"0";\n'
        '    btnStar.classList.toggle("active",next);\n'
        '    btnStar.setAttribute("aria-pressed",next?"true":"false");\n'
        '    btnStar.querySelector(".star-icon").textContent=next?"★":"☆";\n'
        '    flash(next?"Starred":"Unstarred");\n'
        '  }catch(e){flash("Star failed")}\n'
        '  finally{btnStar.disabled=false}\n'
        '})}\n'
        'video.addEventListener("ended",()=>{if(repeat==="one"){video.currentTime=0;video.play()}else if(repeat==="all"||autoplay){window.location.href=nextUrl}});\n'
        'video.play().catch(()=>{});\n'
        # Touch: swipe -> next/prev, double-tap left/right half -> skip -/+10s (parity with keys)
        '(function(){var x0=0,y0=0,t0=0,lastTap=0,lastSide=0;\n'
        'video.addEventListener("touchstart",function(e){if(e.touches.length!==1){t0=0;return}var t=e.touches[0];var r=video.getBoundingClientRect();if(t.clientY>r.bottom-48){t0=0;return}x0=t.clientX;y0=t.clientY;t0=Date.now()},{passive:true});\n'
        'video.addEventListener("touchend",function(e){if(!t0)return;var t=e.changedTouches[0];var dx=t.clientX-x0,dy=t.clientY-y0,dt=Date.now()-t0;t0=0;\n'
        '  if(dt<700&&Math.abs(dx)>60&&Math.abs(dx)>Math.abs(dy)*1.7){window.location.href=dx<0?nextUrl:prevUrl;return}\n'
        '  if(dt<350&&Math.abs(dx)<24&&Math.abs(dy)<24){var r2=video.getBoundingClientRect();var side=t.clientX>r2.left+r2.width/2?1:-1;var now=Date.now();\n'
        '    if(now-lastTap<320&&side===lastSide){skip(side*10);lastTap=0}else{lastTap=now;lastSide=side}}},{passive:true});\n'
        '})();\n'
        'document.addEventListener("keydown",e=>{\n'
        '  var tn=e.target.tagName;\n'
        '  if(tn==="INPUT"||tn==="SELECT"||tn==="TEXTAREA"||e.target.isContentEditable)return;\n'
        '  if(e.target.closest&&e.target.closest("a,button,summary,[tabindex]"))return;\n'
        '  if(e.ctrlKey||e.metaKey||e.altKey)return;\n'
        '  switch(e.key){\n'
        '    case"n":case"ArrowRight":window.location.href=nextUrl;break;\n'
        '    case"p":case"ArrowLeft":window.location.href=prevUrl;break;\n'
        '    case" ":e.preventDefault();video.paused?video.play():video.pause();break;\n'
        '    case"f":e.preventDefault();document.fullscreenElement?document.exitFullscreen():video.requestFullscreen();break;\n'
        '    case"m":video.muted=!video.muted;break;\n'
        '    case"j":e.preventDefault();skip(-10);break;\n'
        '    case"l":e.preventDefault();skip(10);break;\n'
        '    case"J":e.preventDefault();skip(-30);break;\n'
        '    case"L":e.preventDefault();skip(30);break;\n'
        '    case"<":e.preventDefault();cycleSpeed(-1);break;\n'
        '    case">":e.preventDefault();cycleSpeed(1);break;\n'
        '    case"Escape":window.location.href=browseUrl;break;\n'
        '    case"d":document.querySelector("#delete-form button")?.click();break;\n'
        '    case"a":btnAuto.click();break;\n'
        '    case"r":btnRepeat.click();break;\n'
        '  }\n'
        '});\n'
        '</script>'
    )

    return render_page(f'SimpleParty \u2014 {v["name"]}', body)


# --- Video serving ---

def _is_mpegts(path):
    try:
        with open(path, 'rb') as f:
            header = f.read(377)
        return len(header) >= 377 and header[0] == 0x47 and header[188] == 0x47 and header[376] == 0x47
    except OSError:
        return False


BROWSER_VIDEO_CODECS = frozenset({'h264', 'vp8', 'vp9', 'av1'})
BROWSER_AUDIO_CODECS = frozenset({'aac', 'mp3', 'opus', 'vorbis'})

_probe_cache = {}


def _probe_streams(path):
    """Return (video_codec, audio_codec) for a file, or (None, None) on failure. Cached by (path, mtime, size)."""
    try:
        st = path.stat()
    except OSError:
        return (None, None)
    key = (str(path), st.st_mtime, st.st_size)
    cached = _probe_cache.get(key)
    if cached is not None:
        return cached

    def _probe(stream_selector):
        try:
            result = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', stream_selector,
                 '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(path)],
                capture_output=True, timeout=10, text=True,
            )
            codec = result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else None
            return codec or None
        except (subprocess.TimeoutExpired, OSError):
            return None

    vcodec = _probe('v:0')
    acodec = _probe('a:0')
    _probe_cache[key] = (vcodec, acodec)
    return (vcodec, acodec)


def _transcode_plan(path):
    """Return None | 'remux' | 'reencode' for a video file.

    None       -> serve as-is (container and codecs are browser-compatible).
    'remux'    -> repackage into fMP4 without re-encoding streams.
    'reencode' -> full libx264/aac re-encode (video codec not browser-compatible).
    """
    suffix = path.suffix.lower()
    # MPEG-TS masquerading as .mp4 is handled separately by _remux_mpegts.
    if suffix == '.mp4' and _is_mpegts(path):
        return 'remux'

    if not _config.get('has_ffmpeg'):
        # Without ffprobe/ffmpeg we can't inspect codecs; fall back to container-only.
        return None if suffix in BROWSER_NATIVE else 'reencode'

    vcodec, acodec = _probe_streams(path)
    video_ok = vcodec in BROWSER_VIDEO_CODECS
    audio_ok = acodec is None or acodec in BROWSER_AUDIO_CODECS  # missing audio is fine

    if suffix in BROWSER_NATIVE and video_ok and audio_ok:
        return None
    if video_ok and audio_ok:
        return 'remux'
    return 'reencode'


def _needs_transcode(path):
    return _transcode_plan(path) is not None


def _remux_mpegts(path):
    tmp = path.with_suffix('.tmp.mp4')
    try:
        result = subprocess.run(
            ['ffmpeg', '-fflags', '+genpts', '-i', str(path),
             '-c', 'copy', '-bsf:a', 'aac_adtstoasc',
             '-f', 'mp4', '-loglevel', 'error', '-y', str(tmp)],
            capture_output=True, timeout=300,
        )
        if result.returncode == 0:
            os.replace(str(tmp), str(path))
            return True
        return False
    except (subprocess.TimeoutExpired, OSError):
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _serve_transcoded(handler, path, plan='reencode'):
    if _config['has_ffmpeg']:
        if plan == 'remux':
            cmd = [
                'ffmpeg', '-i', str(path),
                '-c:v', 'copy', '-c:a', 'copy',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
                '-f', 'mp4', '-loglevel', 'error', 'pipe:1',
            ]
        else:  # 'reencode'
            cmd = [
                'ffmpeg', '-i', str(path),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
                '-pix_fmt', 'yuv420p', '-crf', '23',
                '-c:a', 'aac', '-b:a', '160k',
                '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
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
    except (BrokenPipeError, ConnectionResetError):
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
    except (BrokenPipeError, ConnectionResetError):
        pass


def _stream_file(handler, path):
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
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


# --- Thumbnail generation ---

def _generate_thumbnails(directory, videos):
    """Background worker: extract thumbnails and backing full-res frames.

    For each video, ensures both a full-res frame in frames/ and a
    thumbnail in thumbs/ exist. Migrates legacy thumbnails that lack
    a backing frame by extracting the full-res frame.
    """
    from simpleparty.tagger import (
        FRAMES_DIR, thumb_path, extract_thumbnail,
        extract_frame, _downscale_frame, _get_duration,
    )
    try:
        t0 = time.monotonic()
        frames_dir = Path(directory) / FRAMES_DIR
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Pre-scan: classify each video
        skip = []
        need_frame = []       # no full-res frame (extract frame + maybe thumb)
        need_thumb_only = []   # frame exists but no thumbnail
        for v in videos:
            name = v['name']
            tp = thumb_path(directory, name)
            has_frame = bool(sorted(frames_dir.glob(f'{name}.f*.jpg')))
            video_file = Path(directory) / name
            if not video_file.exists():
                continue
            if has_frame and tp.exists():
                skip.append(name)
            elif not has_frame:
                need_frame.append(name)
            else:
                need_thumb_only.append(name)

        logger.debug('thumbnails for %s: %d skip, %d need frame, %d need thumb only',
                     directory, len(skip), len(need_frame), len(need_thumb_only))
        for name in skip:
            logger.debug('  skip (frame+thumb exist): %s', name)
        for name in need_frame:
            logger.debug('  need frame extraction: %s', name)
        for name in need_thumb_only:
            logger.debug('  need thumb only: %s', name)

        for name in need_frame:
            video_file = Path(directory) / name
            tp = thumb_path(directory, name)
            extract_thumbnail(str(video_file), str(tp))

        for name in need_thumb_only:
            tp = thumb_path(directory, name)
            existing = sorted(frames_dir.glob(f'{name}.f*.jpg'))
            if existing:
                _downscale_frame(str(existing[0]), str(tp))

        logger.debug('thumbnail generation done for %s (%.1fs)', directory, time.monotonic() - t0)
    finally:
        _config['thumb_jobs'].discard(str(directory))


def _maybe_start_thumbs(directory, videos):
    """Spawn background thumbnail generation if needed and not already running."""
    if not _config['has_ffmpeg'] or not videos:
        return
    dir_str = str(directory)
    if dir_str in _config['thumb_jobs']:
        return
    from simpleparty.tagger import FRAMES_DIR, thumb_path
    frames_dir = Path(directory) / FRAMES_DIR
    def _needs_work(name):
        has_thumb = thumb_path(directory, name).exists()
        has_frame = bool(sorted(frames_dir.glob(f'{name}.f*.jpg'))) if frames_dir.exists() else False
        return not has_thumb or not has_frame
    missing = any(_needs_work(v['name']) for v in videos)
    if not missing:
        return
    _config['thumb_jobs'].add(dir_str)
    logger.debug('starting background thumbnail thread for %s (%d videos to check)',
                 directory, len(videos))
    t = threading.Thread(
        target=_generate_thumbnails,
        args=(directory, videos),
        daemon=True,
    )
    t.start()


# --- Download queue ---

def _new_download_job(job_id, url, target_dir, target_rel):
    return {
        'id': job_id,
        'url': url,
        'target_dir': target_dir,
        'target_rel': target_rel,
        'state': 'queued',
        'running': False,
        'status': '',
        'phase': 'queued',
        'enqueued_at': time.time(),
        'started_at': None,
        'finished_at': None,
        'title': '',
        'filename': '',
        'downloaded_bytes': 0,
        'total_bytes': 0,
        'percent': 0,
        'speed': None,
        'eta': None,
        'final_path': None,
        'final_name': None,
        'play_dir': None,
        'play_name': None,
        'error': None,
    }


def _evict_download_history():
    """Trim non-running jobs beyond the history limit (oldest first)."""
    order = _config['download_order']
    jobs = _config['download_jobs']
    non_running = [jid for jid in order if not jobs.get(jid, {}).get('running')]
    excess = len(non_running) - DOWNLOAD_HISTORY_LIMIT
    if excess <= 0:
        return
    to_drop = set(non_running[:excess])
    _config['download_order'] = [jid for jid in order if jid not in to_drop]
    for jid in to_drop:
        jobs.pop(jid, None)


def _finalize_download_job(job, root):
    """After a download ends, compute play/browse links if final file is in root."""
    final = job.get('final_path')
    if not final:
        return
    try:
        rel = Path(final).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return
    if not Path(final).exists() or not is_video(Path(final).name):
        return
    rel_str = str(rel)
    if '/' in rel_str:
        parent, name = rel_str.rsplit('/', 1)
    else:
        parent, name = '', rel_str
    job['play_dir'] = parent
    job['play_name'] = name


def _download_worker_loop(root):
    from simpleparty.downloader import download_video
    q = _config['download_queue']
    while True:
        job_id = q.get()
        with _config['download_lock']:
            job = _config['download_jobs'].get(job_id)
        if not job:
            continue
        if job.get('state') == 'cancelled':
            continue
        job['state'] = 'running'
        job['started_at'] = time.time()
        try:
            download_video(
                job['url'], job['target_dir'], job,
                format_str=_config.get('yt_dlp_format'),
            )
            if job.get('cancel_requested'):
                job['state'] = 'cancelled'
                job.pop('error', None)
            elif job.get('error'):
                job['state'] = 'error'
            else:
                job['state'] = 'done'
        except Exception as e:
            logger.exception('download worker: unexpected failure')
            job['error'] = str(e) or e.__class__.__name__
            job['state'] = 'error'
        finally:
            job['running'] = False
            job['finished_at'] = time.time()
            if job.get('state') != 'cancelled':
                _finalize_download_job(job, root)


def _ensure_download_worker(root):
    """Create queue + daemon worker on first use."""
    with _config['download_lock']:
        if _config['download_queue'] is None:
            _config['download_queue'] = queue.Queue()
        if _config['download_worker'] is None or not _config['download_worker'].is_alive():
            t = threading.Thread(
                target=_download_worker_loop,
                args=(root,),
                daemon=True,
            )
            _config['download_worker'] = t
            t.start()


def _snapshot_download_jobs():
    with _config['download_lock']:
        order = list(_config['download_order'])
        jobs = {jid: dict(_config['download_jobs'][jid])
                for jid in order if jid in _config['download_jobs']}
    return order, jobs


def _any_download_running(jobs):
    return any(j.get('running') for j in jobs.values())


def render_download_form(target_rel='', *, autofocus=False):
    rel = esc(target_rel)
    af = ' autofocus' if autofocus else ''
    return (
        f'<form hx-post="/download" class="download-form">'
        f'<input type="hidden" name="path" value="{rel}">'
        f'<input type="url" name="url" placeholder="https://… (paste a URL)" aria-label="Download URL" required{af}>'
        f'<button type="submit" class="btn active">\u2B07 Queue</button>'
        f'</form>'
    )


def _render_bytes(n):
    if not n:
        return ''
    return fmt_size(n)


def _render_speed(s):
    if not s:
        return ''
    return fmt_size(int(s)) + '/s'


def _render_eta(eta):
    if eta is None or eta < 0:
        return ''
    if eta < 60:
        return f'{int(eta)}s'
    m, s = divmod(int(eta), 60)
    if m < 60:
        return f'{m}m {s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h {m:02d}m'


def _render_download_job_card(job, *, full=True):
    state = job.get('state', 'queued')
    title = job.get('title') or Path(job.get('filename') or '').name or job['url']
    err = ''
    if job.get('error'):
        err = f'<div class="tag-error">\u274C {esc(job["error"])}</div>'
    card_cls = 'download-card err' if state == 'error' else 'download-card'

    bar = ''
    meta = ''
    if state == 'running':
        pct = job.get('percent', 0)
        bar = (
            f'<div class="tag-progress-bar-wrap">'
            f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
            f'</div>'
        )
        line1 = [job.get('phase', 'downloading')]
        total = job.get('total_bytes', 0)
        done = job.get('downloaded_bytes', 0)
        if total:
            line1.append(f'{_render_bytes(done)} / {_render_bytes(total)} ({pct}%)')
        elif done:
            line1.append(_render_bytes(done))
        line2 = []
        sp = _render_speed(job.get('speed'))
        if sp:
            line2.append(sp)
        eta = _render_eta(job.get('eta'))
        if eta:
            line2.append('ETA ' + eta)
        # Two short rows so the progress doesn't wrap unpredictably on a 360px card
        meta = f'<div class="meta">{esc(" · ".join(line1))}</div>'
        if line2:
            meta += f'<div class="meta">{esc(" · ".join(line2))}</div>'
    elif state == 'done':
        meta_bits = ['Done']
        final_name = job.get('final_name')
        if final_name:
            meta_bits.append(final_name)
        links = ''
        if job.get('play_dir') is not None and job.get('play_name'):
            play_url = url_for_play(job['play_dir'], 0, video=job['play_name'])
            links += f' <a class="btn" href="{esc(play_url)}">\u25B6 Play</a>'
            browse_url = url_for_browse(job['play_dir'])
            links += f' <a class="btn" href="{esc(browse_url)}">\U0001F4C1 Folder</a>'
        meta = (
            f'<div class="meta"><span class="tag-done">\u2705 '
            f'{esc(" · ".join(meta_bits))}</span>{links}</div>'
        )
    elif state == 'cancelled':
        meta = '<div class="meta">Cancelled</div>'
    elif state == 'queued':
        meta = '<div class="meta">Queued</div>'

    cancel = ''
    if full and state in ('queued', 'running'):
        cancel = (
            f'<form hx-post="/download-cancel" style="display:inline">'
            f'<input type="hidden" name="id" value="{esc(job["id"])}">'
            f'<button class="btn">Cancel</button>'
            f'</form>'
        )

    target_link = ''
    if full:
        browse_url = url_for_browse(job.get('target_rel', ''))
        tlabel = job.get('target_rel') or '/'
        target_link = (
            f'<span class="meta">→ '
            f'<a class="crumb" href="{esc(browse_url)}">{esc(tlabel)}</a></span>'
        )

    return (
        f'<div class="{card_cls}">'
        f'<div class="row">'
        f'<span class="title">{esc(title)}</span>'
        f'{target_link}'
        f'{cancel}'
        f'</div>'
        f'<div class="row">'
        f'<span class="url">{esc(job["url"])}</span>'
        f'</div>'
        f'{bar}'
        f'{meta}'
        f'{err}'
        f'</div>'
    )


def render_download_status(path_filter=None):
    """Returns the status fragment — self-polling element.

    `path_filter` (rel path) scopes to jobs targeting that directory, for the
    inline panel on the browse page. Without it, returns the full board for
    the dedicated page.
    """
    order, jobs = _snapshot_download_jobs()
    running = _any_download_running(jobs)
    poll = 'every 1s' if running else 'every 10s'

    if path_filter is not None:
        scoped = [jobs[jid] for jid in order
                  if jobs[jid].get('target_rel') == path_filter]
        active = [j for j in scoped if j.get('state') in ('queued', 'running')]
        inner = ''
        if active:
            parts = []
            for j in active:
                if j.get('state') == 'running':
                    pct = j.get('percent', 0)
                    title = j.get('title') or Path(j.get('filename') or '').name or j['url']
                    parts.append(
                        f'<span class="tag-progress-phase">\u2B07 {esc(title[:60])}</span>'
                        f'<div class="tag-progress-bar-wrap">'
                        f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
                        f'</div>'
                        f'<span class="tag-progress-text">{pct}%</span>'
                    )
                else:
                    parts.append(f'<span class="tag-progress-text">\u2B07 queued</span>')
            inner = ''.join(parts) + ' <a class="btn" href="/download">Manage</a>'
        return (
            f'<div hx-get="/download-status?{urllib.parse.urlencode({"path": path_filter, "inline": "1"})}" '
            f'hx-trigger="{poll}" hx-swap="outerHTML" '
            f'role="status" aria-live="polite" '
            f'class="download-progress-panel" id="download-progress">{inner}</div>'
        )

    # Full board
    active = [jobs[jid] for jid in order if jobs[jid].get('state') == 'running']
    queued = [jobs[jid] for jid in order if jobs[jid].get('state') == 'queued']
    finished = [jobs[jid] for jid in order
                if jobs[jid].get('state') in ('done', 'error', 'cancelled')]
    finished.reverse()  # most recent first

    pieces = []
    if active:
        pieces.append('<div class="download-section-title">Now downloading</div>')
        for j in active:
            pieces.append(_render_download_job_card(j))
    if queued:
        pieces.append('<div class="download-section-title">Queued</div>')
        for j in queued:
            pieces.append(_render_download_job_card(j))
    if finished:
        pieces.append(
            '<div class="download-section-title">Recent '
            '<form hx-post="/download-clear" style="display:inline;margin-left:8px">'
            '<button class="btn">Clear completed</button></form></div>'
        )
        for j in finished:
            pieces.append(_render_download_job_card(j))
    if not pieces:
        pieces.append('<div class="empty">No downloads yet.</div>')

    return (
        f'<div hx-get="/download-status" hx-trigger="{poll}" '
        f'hx-swap="outerHTML" class="download-board" id="download-board" role="status" aria-live="polite">'
        f'{"".join(pieces)}</div>'
    )


def render_download_page(target_rel=''):
    nav = render_nav('')
    hint = (
        '<div style="padding:16px 16px 0;color:#94a3b8;font-size:13px">'
        'Paste a URL. Downloads land in the chosen directory '
        '(default: server root). One at a time.'
        '</div>'
    )
    full_form = (
        f'<form hx-post="/download" class="download-form" style="margin:8px 16px 0">'
        f'<input type="url" name="url" placeholder="https://…" aria-label="Download URL" required autofocus style="flex:2">'
        f'<input type="text" name="path" placeholder="subdir/ (blank = root)" aria-label="Target subdirectory" value="{esc(target_rel)}" style="flex:1">'
        f'<button type="submit" class="btn active">\u2B07 Queue</button>'
        f'</form>'
    )
    board = render_download_status(path_filter=None)
    body = (
        nav + '<main id="main"><h1 class="visually-hidden">Downloads</h1>'
        + hint + full_form + board + '</main>'
    )
    return render_page('Downloads — SimpleParty', body)


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
        tags_map = None
        selected_tags = parse_tags_param(params)
        starred_only = parse_starred_param(params)
        sort, direction = parse_sort_params(params)
        resolved = resolve_path(root, rel_path)
        if _config['allow_tag']:
            from simpleparty.tagger import load_tags
            tags_map = load_tags(resolved)
            data['videos'] = filter_videos_by_tags(data['videos'], tags_map, selected_tags)
            data['videos'] = filter_videos_by_starred(data['videos'], tags_map, starred_only)
        if sort == 'length':
            _populate_durations(root, data['videos'], tags_map, resolved)
        data['videos'] = sort_videos(data['videos'], sort, direction)
        _maybe_start_thumbs(resolved, data['videos'])
        send_html(handler, render_browse_page(
            data, tags_map=tags_map, selected_tags=selected_tags,
            sort=sort, direction=direction, starred_only=starred_only,
        ))


def handle_play(handler, root):
    params = parse_query(handler.path)
    dir_path = params.get('path', '')
    data = list_directory(root, dir_path)

    if data.get('locked'):
        send_html(handler, render_locked_page(dir_path, data['encryptedDir']))
        return

    selected_tags = parse_tags_param(params)
    starred_only = parse_starred_param(params)
    sort, direction = parse_sort_params(params)
    tags_map = None
    resolved = resolve_path(root, dir_path)
    if _config['allow_tag']:
        from simpleparty.tagger import load_tags
        tags_map = load_tags(resolved)
        data['videos'] = filter_videos_by_tags(data['videos'], tags_map, selected_tags)
        data['videos'] = filter_videos_by_starred(data['videos'], tags_map, starred_only)
    if sort == 'length':
        _populate_durations(root, data['videos'], tags_map, resolved)
    data['videos'] = sort_videos(data['videos'], sort, direction)

    if 'error' in data or not data.get('videos'):
        send_redirect(handler, url_for_browse(dir_path, tags=selected_tags, sort=sort, direction=direction, starred=starred_only))
        return

    n = len(data['videos'])
    shuffled = params.get('shuffle') == '1'
    video_name = params.get('video')

    play_order = None
    shuffle_seed = None
    if shuffled:
        seed = safe_int(params.get('seed'), random.randint(0, 2**31))
        pos = safe_int(params.get('pos')) % n
        order = shuffle_indices(n, seed)
        idx = order[pos]
        # If a video name was provided and doesn't match, find it by name
        # (handles stale URLs after deletion)
        if video_name and data['videos'][idx]['name'] != video_name:
            found = find_video_idx(data['videos'], video_name)
            if found is not None:
                idx = found
                pos = order.index(idx) if idx in order else pos
        next_pos = (pos + 1) % n
        prev_pos = (pos - 1) % n
        next_url = url_for_play(dir_path, order[next_pos], shuffle=True, seed=seed, pos=next_pos, tags=selected_tags, video=data['videos'][order[next_pos]]['name'], sort=sort, direction=direction, starred=starred_only)
        prev_url = url_for_play(dir_path, order[prev_pos], shuffle=True, seed=seed, pos=prev_pos, tags=selected_tags, video=data['videos'][order[prev_pos]]['name'], sort=sort, direction=direction, starred=starred_only)
        pos_info = f'{pos + 1}/{n}'
        shuffle_url = url_for_play(dir_path, idx, tags=selected_tags, video=data['videos'][idx]['name'], sort=sort, direction=direction, starred=starred_only)
        play_order = order
        shuffle_seed = seed
    else:
        idx_param = safe_int(params.get('idx'))
        # Prefer video name lookup (stable across deletions)
        found = find_video_idx(data['videos'], video_name) if video_name else None
        idx = found if found is not None else max(0, min(idx_param, n - 1))
        next_idx = (idx + 1) % n
        prev_idx = (idx - 1) % n
        next_url = url_for_play(dir_path, next_idx, tags=selected_tags, video=data['videos'][next_idx]['name'], sort=sort, direction=direction, starred=starred_only)
        prev_url = url_for_play(dir_path, prev_idx, tags=selected_tags, video=data['videos'][prev_idx]['name'], sort=sort, direction=direction, starred=starred_only)
        pos_info = f'{idx + 1}/{n}'
        shuffle_params = {'path': dir_path, 'shuffle': '1'}
        if selected_tags:
            shuffle_params['tags'] = ','.join(selected_tags)
        if sort and sort != 'name':
            shuffle_params['sort'] = sort
        if direction and direction != 'asc':
            shuffle_params['dir'] = direction
        if starred_only:
            shuffle_params['starred'] = '1'
        shuffle_url = '/play?' + urllib.parse.urlencode(shuffle_params)

    transcode_plan = None
    if _config['allow_transcode'] and (_config['has_ffmpeg'] or _config['has_vlc']):
        try:
            video_fs_path = resolve_path(root, data['videos'][idx]['path'])
            if video_fs_path.is_file():
                transcode_plan = _transcode_plan(video_fs_path) if _config['has_ffmpeg'] else (
                    None if video_fs_path.suffix.lower() in BROWSER_NATIVE else 'reencode'
                )
        except OSError:
            pass

    send_html(handler, render_play_page(data, idx, next_url, prev_url, shuffle_url, shuffled, pos_info, tags_map=tags_map, selected_tags=selected_tags, play_order=play_order, shuffle_seed=shuffle_seed, transcode_plan=transcode_plan, sort=sort, direction=direction, starred_only=starred_only))


def handle_video(handler, root):
    parsed = urllib.parse.urlparse(handler.path)
    rel_path = urllib.parse.unquote(parsed.path[len('/video/'):])
    resolved = resolve_path(root, rel_path)

    if not resolved.is_file():
        handler.send_error(404)
        return

    if _config['allow_transcode'] and (_config['has_ffmpeg'] or _config['has_vlc']):
        plan = _transcode_plan(resolved) if _config['has_ffmpeg'] else (
            None if resolved.suffix.lower() in BROWSER_NATIVE else 'reencode'
        )
        if plan is not None:
            if _is_mpegts(resolved) and _config['has_ffmpeg'] and _remux_mpegts(resolved):
                pass  # file is now a proper MP4, fall through to normal serving
            else:
                vcodec, acodec = _probe_streams(resolved) if _config['has_ffmpeg'] else (None, None)
                logger.info('transcode plan=%s v=%s a=%s file=%s', plan, vcodec, acodec, resolved)
                _serve_transcoded(handler, resolved, plan)
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
    # Clean up tags entry for deleted video
    if _config['allow_tag']:
        try:
            from simpleparty.tagger import load_tags, save_tags
            dir_path = resolved.parent
            all_tags = load_tags(dir_path)
            if resolved.name in all_tags:
                del all_tags[resolved.name]
                save_tags(dir_path, all_tags)
        except Exception:
            pass  # best-effort cleanup
    if redirect_url:
        send_hx_redirect(handler, redirect_url)
    else:
        handler.send_response(200)
        handler.send_header('Content-Length', '0')
        handler.end_headers()


def handle_delete_by_tag(handler, root):
    if not _config['allow_delete']:
        handler.send_error(403, 'Delete disabled')
        return
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    form = read_form_body(handler)
    rel_path = form.get('path', '')
    raw_tags = form.get('tags', '')
    selected_tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    if not selected_tags:
        handler.send_error(400, 'No tags specified')
        return
    resolved_dir = resolve_path(root, rel_path)
    if not resolved_dir.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    data = list_directory(root, rel_path)
    if 'error' in data or data.get('locked'):
        handler.send_error(400, 'Cannot list directory')
        return

    from simpleparty.tagger import load_tags, save_tags
    tags_map = load_tags(resolved_dir)
    targets = filter_videos_by_tags(data['videos'], tags_map, selected_tags)

    for video in targets:
        video_path = resolved_dir / video['name']
        try:
            os.remove(video_path)
        except OSError as e:
            logger.warning('delete-by-tag: failed to remove %s: %s', video_path, e)
            continue
        tags_map.pop(video['name'], None)

    try:
        save_tags(resolved_dir, tags_map)
    except OSError as e:
        logger.warning('delete-by-tag: failed to save tags for %s: %s', resolved_dir, e)

    send_hx_redirect(handler, url_for_browse(rel_path))


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


def handle_train(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import train

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    max_frames = int(form.get('frames', '1'))
    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    resolved_str = str(resolved)
    existing = _config['tag_jobs'].get(resolved_str)
    if existing and existing.get('running'):
        send_hx_redirect(handler, url_for_browse(rel_path))
        return

    progress = {'running': True, 'done': 0, 'total': 0, 'current': '', 'phase': 'preparing'}
    _config['tag_jobs'][resolved_str] = progress

    t = threading.Thread(
        target=train,
        args=(resolved_str,),
        kwargs={'max_frames': max_frames, 'progress': progress},
        daemon=True,
    )
    t.start()
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_suggest(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import suggest_for_directory
    from simpleparty.tagger import model_path as _model_path

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    mp = _model_path(resolved)
    model_path = str(mp)
    if not mp.exists():
        handler.send_error(400, 'No trained model found. Train first.')
        return

    resolved_str = str(resolved)
    existing = _config['tag_jobs'].get(resolved_str)
    if existing and existing.get('running'):
        send_hx_redirect(handler, url_for_browse(rel_path))
        return

    progress = {'running': True, 'done': 0, 'total': 0, 'current': '', 'phase': 'suggesting'}
    _config['tag_jobs'][resolved_str] = progress

    t = threading.Thread(
        target=suggest_for_directory,
        args=(resolved_str, model_path),
        kwargs={'progress': progress, 'max_tags': _config['max_tags']},
        daemon=True,
    )
    t.start()
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_suggest_one(handler, root):
    """Suggest tags for a single video and return updated tag HTML."""
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import suggest_for_video
    from simpleparty.tagger import load_tags, save_tags, model_path as _model_path

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    mp = _model_path(resolved)
    if not mp.exists():
        handler.send_error(400, 'No trained model found. Train first.')
        return

    video_path = resolved / video_name
    if not video_path.exists():
        handler.send_error(404, 'Video not found')
        return

    results = suggest_for_video(str(video_path), str(mp), max_tags=_config['max_tags'])
    if results:
        all_tags = load_tags(resolved)
        avg_conf = sum(c for _, c in results) / len(results)
        entry = all_tags.get(video_name, {})
        entry['tags'] = [tag for tag, _ in results]
        entry['status'] = 'suggested'
        entry['confidence'] = round(avg_conf, 3)
        all_tags[video_name] = entry
        save_tags(resolved, all_tags)
        send_html(handler, render_video_tags_inline(
            rel_path, video_name,
            [tag for tag, _ in results],
            status='suggested',
        ))
    else:
        send_html(handler, render_video_tags_inline(rel_path, video_name, []))


def handle_confirm_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    all_tags = load_tags(resolved)
    entry = all_tags.get(video_name, {})
    entry['status'] = 'confirmed'
    from datetime import datetime, timezone
    entry['confirmed_at'] = datetime.now(timezone.utc).isoformat()
    all_tags[video_name] = entry
    save_tags(resolved, all_tags)

    tags_list = entry.get('tags', [])
    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list, status='confirmed'))


def handle_confirm_all(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir():
        handler.send_error(400, 'Invalid request')
        return

    all_tags = load_tags(resolved)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for entry in all_tags.values():
        if entry.get('status') == 'suggested':
            entry['status'] = 'confirmed'
            entry['confirmed_at'] = now
            count += 1
    save_tags(resolved, all_tags)
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_reject_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    all_tags = load_tags(resolved)
    if video_name in all_tags:
        entry = all_tags[video_name]
        entry['rejected_tags'] = entry.get('rejected_tags', []) + entry.get('tags', [])
        entry['tags'] = []
        entry['status'] = 'rejected'
        save_tags(resolved, all_tags)

    send_html(handler, render_video_tags_inline(rel_path, video_name, []))


def handle_reject_tag(handler, root):
    """Reject a single suggested tag, keeping remaining suggestions."""
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    tag = form.get('tag', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name or not tag:
        handler.send_error(400, 'Invalid request')
        return

    all_tags = load_tags(resolved)
    entry = all_tags.get(video_name, {})
    tags_list = entry.get('tags', [])

    if tag in tags_list:
        tags_list.remove(tag)
        entry['tags'] = tags_list
        rejected = entry.get('rejected_tags', [])
        rejected.append(tag)
        entry['rejected_tags'] = rejected

    if not tags_list:
        entry['status'] = 'rejected'
        status = 'rejected'
    else:
        status = entry.get('status', 'confirmed')

    all_tags[video_name] = entry
    save_tags(resolved, all_tags)

    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list, status=status))


def handle_tag_status(handler, root):
    if not _config['allow_tag']:
        send_html(handler, '')
        return
    params = parse_query(handler.path)
    rel_path = params.get('path', '')
    resolved = str(resolve_path(root, rel_path))
    status_url = f'/tag-status?{urllib.parse.urlencode({"path": rel_path})}'
    path_param = esc(rel_path)

    progress = _config['tag_jobs'].get(resolved)

    # OOB swap to keep train button in sync
    def train_btn_oob(busy):
        btn = _render_train_btn(path_param, busy)
        return btn.replace('id="train-form"', 'id="train-form" hx-swap-oob="true"', 1)

    def wrap(inner, poll=None, active=False):
        if poll:
            return (
                f'<div hx-get="{status_url}" hx-trigger="{poll}" '
                f'hx-swap="outerHTML" class="tag-progress-panel'
                f'{" active" if active else ""}" role="status" aria-live="polite" id="tag-progress">'
                f'{inner}</div>'
            )
        return f'<div class="tag-progress-panel" id="tag-progress" role="status" aria-live="polite">{inner}</div>'

    if not progress:
        send_html(handler, wrap('', poll='every 10s'))
        return

    if progress.get('error'):
        send_html(handler,
            wrap(f'<span class="tag-error">\u274C {esc(progress["error"])}</span>')
            + train_btn_oob(False))
        return

    if not progress.get('running'):
        if progress.get('phase') == 'done':
            msg = esc(progress.get('current', 'Done'))
            suggest = (
                f'<form hx-post="/suggest" style="display:inline">'
                f'<input type="hidden" name="path" value="{path_param}">'
                f'<button class="btn">\U0001F3F7 Suggest tags</button>'
                f'</form>'
            )
            send_html(handler,
                wrap(f'<span class="tag-done">\u2705 {msg} {suggest}</span>')
                + train_btn_oob(False))
        else:
            send_html(handler, wrap('', poll='every 10s'))
        return

    phase = progress.get('phase', '')
    done = progress.get('done', 0)
    total = progress.get('total', 0)
    current = progress.get('current', '')

    pct = int(done * 100 / total) if total > 0 else 0
    bar = (
        f'<div class="tag-progress-bar-wrap">'
        f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
        f'</div>'
    ) if total > 0 else ''

    phase_label = phase.replace('_', ' ').replace('(', '(').title()
    text = f'{done}/{total}' if total else ''
    if current:
        text += f' \u2014 {esc(current)}' if text else esc(current)
    text += '\u2026'

    inner = (
        f'<span class="tag-progress-phase">{esc(phase_label)}</span>'
        f'{bar}'
        f'<span class="tag-progress-text">{text}</span>'
    )
    send_html(handler,
        wrap(inner, poll='every 2s', active=True)
        + train_btn_oob(True))


def handle_save_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    raw_tags = form.get('tags', '')

    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    tags_list = [t.strip() for t in raw_tags.split(',') if t.strip()]

    all_tags = load_tags(resolved)
    entry = all_tags.get(video_name, {})
    entry['tags'] = tags_list
    entry['status'] = 'confirmed'
    from datetime import datetime, timezone
    entry['tagged_at'] = datetime.now(timezone.utc).isoformat()
    all_tags[video_name] = entry
    save_tags(resolved, all_tags)

    # Return updated pill HTML for HTMX swap
    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list))


def handle_star_update(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, save_tags, set_starred

    form = read_form_body(handler)
    rel_dir = form.get('dir', '')
    video_name = form.get('name', '')
    starred_flag = form.get('starred', '') == '1'

    if not video_name:
        handler.send_error(400, 'Missing video name')
        return

    resolved = resolve_path(root, rel_dir)
    if not resolved.is_dir():
        handler.send_error(404, 'Directory not found')
        return
    if not (resolved / video_name).exists():
        handler.send_error(404, 'Video not found')
        return

    all_tags = load_tags(resolved)
    set_starred(all_tags, video_name, starred_flag)
    save_tags(resolved, all_tags)

    handler.send_response(204)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def handle_thumb(handler, root):
    """Serve a thumbnail JPEG from .simpleparty/thumbs/."""
    raw = urllib.parse.urlparse(handler.path).path
    rel = raw[len('/thumb/'):]  # strip prefix
    rel = urllib.parse.unquote(rel)
    if not rel:
        handler.send_error(404)
        return
    # rel is "dir/subdir/video.mp4" — thumb is at dir/subdir/.simpleparty/thumbs/video.mp4.jpg
    from simpleparty.tagger import thumb_path
    video_dir = Path(root) / Path(rel).parent
    video_name = Path(rel).name
    tp = thumb_path(str(video_dir), video_name)
    if not tp.exists():
        handler.send_error(404)
        return
    data = tp.read_bytes()
    handler.send_response(200)
    handler.send_header('Content-Type', 'image/jpeg')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.end_headers()
    handler.wfile.write(data)


def handle_download_page(handler, root):
    if not _config['allow_download']:
        handler.send_error(404)
        return
    params = parse_query(handler.path)
    rel = params.get('path', '')
    send_html(handler, render_download_page(rel))


def handle_download_submit(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    from simpleparty.downloader import validate_url, is_path_within

    form = read_form_body(handler)
    rel_path = form.get('path', '') or ''
    redirect_url = form.get('redirect') or ''
    try:
        url = validate_url(form.get('url', ''))
    except ValueError as e:
        handler.send_error(400, str(e))
        return

    resolved = resolve_path(root, rel_path)
    if not is_path_within(root, resolved) or not resolved.is_dir():
        handler.send_error(400, 'Invalid target directory')
        return

    job_id = uuid.uuid4().hex
    job = _new_download_job(job_id, url, str(resolved), rel_path)
    with _config['download_lock']:
        _config['download_jobs'][job_id] = job
        _config['download_order'].append(job_id)
        _evict_download_history()
    _ensure_download_worker(root)
    _config['download_queue'].put(job_id)

    send_hx_redirect(handler, redirect_url or '/download')


def handle_download_status(handler, root):
    if not _config['allow_download']:
        send_html(handler, '')
        return
    params = parse_query(handler.path)
    # The browse-page inline panel carries inline=1 so it stays scoped (and
    # hidden when empty) even for the root dir, where the blank path is dropped.
    if 'inline' in params:
        path_filter = params.get('path', '')
    else:
        path_filter = params.get('path') if 'path' in params else None
    send_html(handler, render_download_status(path_filter=path_filter))


def handle_download_cancel(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    form = read_form_body(handler)
    job_id = form.get('id', '')
    with _config['download_lock']:
        job = _config['download_jobs'].get(job_id)
        if job and job.get('state') == 'queued':
            job['state'] = 'cancelled'
            job['finished_at'] = time.time()
        elif job and job.get('state') == 'running':
            # Signal the worker's progress hook to abort the in-flight download.
            job['cancel_requested'] = True
    send_hx_redirect(handler, '/download')


def handle_download_clear(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    with _config['download_lock']:
        jobs = _config['download_jobs']
        order = _config['download_order']
        keep = [jid for jid in order
                if jobs.get(jid, {}).get('state') in ('queued', 'running')]
        dropped = [jid for jid in order if jid not in keep]
        for jid in dropped:
            jobs.pop(jid, None)
        _config['download_order'] = keep
    send_hx_redirect(handler, '/download')


# --- Server ---

class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

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
        elif path == '/tag-status':
            handle_tag_status(self, self.root)
        elif path.startswith('/thumb/'):
            handle_thumb(self, self.root)
        elif path == '/download':
            handle_download_page(self, self.root)
        elif path == '/download-status':
            handle_download_status(self, self.root)
        else:
            self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == '/delete':
            handle_delete(self, self.root)
        elif path == '/delete-by-tag':
            handle_delete_by_tag(self, self.root)
        elif path == '/unlock':
            handle_unlock(self, self.root)
        elif path == '/lock':
            handle_lock(self, self.root)
        elif path == '/train':
            handle_train(self, self.root)
        elif path == '/suggest':
            handle_suggest(self, self.root)
        elif path == '/suggest-one':
            handle_suggest_one(self, self.root)
        elif path == '/confirm-tags':
            handle_confirm_tags(self, self.root)
        elif path == '/confirm-all':
            handle_confirm_all(self, self.root)
        elif path == '/reject-tags':
            handle_reject_tags(self, self.root)
        elif path == '/reject-tag':
            handle_reject_tag(self, self.root)
        elif path == '/save-tags':
            handle_save_tags(self, self.root)
        elif path == '/star-update':
            handle_star_update(self, self.root)
        elif path == '/download':
            handle_download_submit(self, self.root)
        elif path == '/download-cancel':
            handle_download_cancel(self, self.root)
        elif path == '/download-clear':
            handle_download_clear(self, self.root)
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
    parser.add_argument('--no-tag', action='store_true', help='Disable all tagging features')
    parser.add_argument('--max-tags', type=int, default=10, help='Max tags per video when suggesting (default: 10)')
    parser.add_argument('--no-download', action='store_true', help='Disable URL download feature')
    parser.add_argument('--yt-dlp-format', default=None,
                        help='yt-dlp format selector (default: bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b)')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format='%(asctime)s %(name)s %(message)s',
        datefmt='%H:%M:%S',
    )

    root = str(Path(args.root).resolve())
    if not Path(root).is_dir():
        print(f'Error: {root} is not a directory', file=sys.stderr)
        raise SystemExit(1)

    _config['has_ffmpeg'] = shutil.which('ffmpeg') is not None
    _config['has_vlc'] = shutil.which('cvlc') is not None
    _config['allow_delete'] = not args.no_delete
    _config['allow_transcode'] = not args.no_transcode

    _config['root'] = root
    _config['max_tags'] = args.max_tags
    if args.no_tag:
        _config['allow_tag'] = False

    from simpleparty.downloader import is_available as _ytdlp_available
    _config['has_ytdlp'] = _ytdlp_available()
    _config['allow_download'] = (not args.no_download) and _config['has_ytdlp']
    _config['yt_dlp_format'] = args.yt_dlp_format

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
    has_torch = False
    if _config['allow_tag']:
        try:
            import torch
            has_torch = True
            features.append('tag: on')
        except ImportError:
            features.append('tag: on (tagger unavailable)')
    if _config['allow_download']:
        features.append('download: on')
    elif not args.no_download and not _config['has_ytdlp']:
        features.append('download: on (yt-dlp unavailable)')

    from simpleparty import __version__
    url = f'http://{args.bind}:{args.port}'
    print(f'SimpleParty {__version__} serving {root}')
    print(f'  {url}')
    if features:
        print(f'  [{", ".join(features)}]')
    if _config['allow_tag'] and not has_torch:
        print(f'  To train a tagger: uvx simpleparty[classifier]=={__version__}')
    if (not args.no_download) and (not _config['has_ytdlp']):
        print(f'  To enable downloads: uvx simpleparty[download]=={__version__}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
