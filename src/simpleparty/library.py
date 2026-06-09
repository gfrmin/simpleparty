"""Filesystem listing, fscrypt status, and video sort/filter helpers."""

import os
import random
import re
import subprocess
import threading
import time
from pathlib import Path

from simpleparty.state import VIDEO_EXTENSIONS


# --- Filesystem ---

def is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def resolve_path(root, relative):
    """Resolve a path relative to root, following symlinks."""
    if not relative:
        return Path(root).resolve()
    return (Path(root) / relative).resolve()


# Directory scans cached on the directory's mtime_ns: creates, deletes, and
# renames bump it, so cache hits skip the listdir and the per-file stats.
# In-place file modifications don't bump it; sizes/mtimes can briefly lag.
_dir_cache = {}  # str(resolved) -> (dir_mtime_ns, [(name, size, mtime)], [dir names])
_dir_cache_lock = threading.Lock()
_DIR_CACHE_MAX = 64


def scan_directory(resolved):
    """One pass over a directory. Returns ([(name, size, mtime)], [dir names])
    for videos and child directories, or None if unreadable."""
    try:
        entries = sorted(os.listdir(resolved))
    except (PermissionError, OSError):
        return None
    videos, dir_names = [], []
    for name in entries:
        if name.startswith('.'):
            continue
        full = resolved / name
        if full.is_dir():
            dir_names.append(name)
        elif full.is_file() and is_video(name):
            try:
                st = full.stat()
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0
            videos.append((name, size, mtime))
    return videos, dir_names


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
        dir_mtime_ns = resolved.stat().st_mtime_ns
    except OSError:
        return {'error': 'Cannot read directory'}

    key = str(resolved)
    with _dir_cache_lock:
        cached = _dir_cache.get(key)
    if cached and cached[0] == dir_mtime_ns:
        video_entries, dir_names = cached[1], cached[2]
    else:
        scanned = scan_directory(resolved)
        if scanned is None:
            return {'error': 'Cannot read directory'}
        video_entries, dir_names = scanned
        with _dir_cache_lock:
            _dir_cache[key] = (dir_mtime_ns, video_entries, dir_names)
            while len(_dir_cache) > _DIR_CACHE_MAX:
                _dir_cache.pop(next(iter(_dir_cache)))

    encrypted_root = find_encrypted_ancestor(root, rel_path)

    def child_path(name):
        return os.path.join(rel_path, name) if rel_path else name

    # fscrypt status is recomputed per request (it has its own TTL cache,
    # invalidated on lock/unlock), and callers receive fresh dicts so the
    # cached entries are never mutated downstream.
    dirs = []
    for name in dir_names:
        dir_status = get_fscrypt_status(resolved / name)
        dirs.append({
            'name': name, 'path': child_path(name),
            'encrypted': dir_status['encrypted'],
            'unlocked': dir_status['unlocked'],
        })
    videos = [
        {'name': name, 'path': child_path(name), 'size': size, 'mtime': mtime}
        for name, size, mtime in video_entries
    ]

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


# --- Sorting and filtering ---

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


def durations_from_tags(videos, tags_map):
    """Return new video dicts with 'duration' filled from cached tag entries.

    A cached duration counts only when its duration_mtime matches the
    file's mtime; anything else sorts as 0.0 until the background probe
    (media._maybe_start_durations) refreshes the cache."""
    tags_map = tags_map or {}
    out = []
    for v in videos:
        entry = tags_map.get(v['name'], {})
        dur = entry.get('duration')
        cached_ok = dur is not None and entry.get('duration_mtime') == v.get('mtime', 0.0)
        out.append({**v, 'duration': dur if cached_ok else 0.0})
    return out


def filter_videos_by_tags(videos, lower_index, selected_tags):
    """Filter video list to those having ALL selected tags (AND logic).

    `lower_index` maps video name -> frozenset of lowercased tags
    (from tagger.load_tags_index), so nothing is re-lowercased per request.
    """
    if not selected_tags or not lower_index:
        return videos
    selected_lower = {t.lower() for t in selected_tags}
    return [
        v for v in videos
        if selected_lower <= lower_index.get(v['name'], frozenset())
    ]


def filter_videos_by_starred(videos, tags_map, starred_only):
    """Filter video list to only those marked starred."""
    if not starred_only:
        return videos
    if not tags_map:
        return []
    return [v for v in videos if tags_map.get(v['name'], {}).get('starred')]


def _compute_related_videos(data, idx, lower_index, max_results=8):
    """Return list of (video_index, shared_tag_count) for videos sharing tags with current."""
    if not lower_index:
        return []
    current_tags = lower_index.get(data['videos'][idx]['name'], frozenset())
    if not current_tags:
        return []
    scored = []
    for i, v in enumerate(data['videos']):
        if i == idx:
            continue
        overlap = len(current_tags & lower_index.get(v['name'], frozenset()))
        if overlap > 0:
            scored.append((i, overlap))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:max_results]


def shuffle_indices(n, seed):
    rng = random.Random(seed)
    indices = list(range(n))
    rng.shuffle(indices)
    return indices


def find_video_idx(videos, name):
    """Find a video's index by name. Returns None if not found."""
    for i, v in enumerate(videos):
        if v['name'] == name:
            return i
    return None
