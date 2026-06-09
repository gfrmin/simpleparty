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


def find_video_idx(videos, name):
    """Find a video's index by name. Returns None if not found."""
    for i, v in enumerate(videos):
        if v['name'] == name:
            return i
    return None
