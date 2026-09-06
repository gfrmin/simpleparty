"""Filesystem listing, fscrypt status, and video sort/filter helpers."""

import errno
import fcntl
import logging
import os
import random
import re
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path

from simpleparty.state import VIDEO_EXTENSIONS

logger = logging.getLogger('simpleparty.library')


# --- Filesystem ---

def is_video(name):
    return Path(name).suffix.lower() in VIDEO_EXTENSIONS


def resolve_path(root, relative):
    """Resolve a path relative to root, following symlinks."""
    if not relative:
        return Path(root).resolve()
    return (Path(root) / relative).resolve()


def is_safe_rel_path(relative):
    """True if a request-supplied path stays inside the served root
    lexically: relative, with no '..' components. Symlinks inside the
    tree are still followed by resolve_path()."""
    if not relative:
        return True
    p = Path(relative)
    return not p.is_absolute() and '..' not in p.parts


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
    if not is_safe_rel_path(rel_path):
        return {'error': 'Not found'}
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

# Detection and unlocking pull apart cleanly: the kernel reports encryption
# state over ioctls that need no userspace tool, while unlocking genuinely
# needs the fscrypt binary (the passphrase is stretched with argon2id using
# costs stored in fscrypt's own protector metadata, which the stdlib cannot
# do). So detection always works; only unlock/lock depend on the CLI.

# Ioctls from <linux/fscrypt.h>, encoded as
#   _IOWR(type, nr, size) == (3 << 30) | (size << 16) | (ord(type) << 8) | nr
_FS_IOC_GET_POLICY_EX = (3 << 30) | (9 << 16) | (0x66 << 8) | 22
_FS_IOC_GET_KEY_STATUS = (3 << 30) | (128 << 16) | (0x66 << 8) | 26

_KEY_SPEC_IDENTIFIER = 2  # v2 policies key off a 16-byte master key identifier
_KEY_STATUS_PRESENT = 2   # FSCRYPT_KEY_STATUS_PRESENT

# struct fscrypt_get_key_status_arg is 128 bytes, with `status` at offset 64.
_KEY_STATUS_ARG_SIZE = 128
_KEY_STATUS_OFFSET = 64
# Room offered to the kernel for the returned policy; v2 policies need 24.
_POLICY_BUF_SIZE = 64

# Errnos that mean "nothing is encrypted here" rather than "the query failed":
# no policy on this directory, or a filesystem that has no encryption support
# and so doesn't implement the ioctl.
_NOT_ENCRYPTED_ERRNOS = frozenset({
    errno.ENODATA, errno.ENOTTY, errno.ENOTSUP, errno.EOPNOTSUPP,
    errno.EINVAL, errno.ENOENT, errno.EACCES, errno.EPERM,
})


def _unencrypted():
    """Fresh dict each call: callers treat these as their own to mutate."""
    return {'encrypted': False, 'unlocked': True}


def _probe_kernel_status(dir_path):
    """Ask the kernel whether dir_path is encrypted and whether its master key
    is present. Returns a status dict, or None when the question needs the
    fscrypt CLI to answer.

    FS_IOC_GET_ENCRYPTION_POLICY_EX reports the directory's policy, and
    FS_IOC_GET_ENCRYPTION_KEY_STATUS reports whether that policy's master key
    has been added to the filesystem. Both cost about as much as a stat().
    """
    try:
        fd = os.open(dir_path, os.O_RDONLY)
    except OSError:
        return _unencrypted()
    try:
        # struct fscrypt_get_policy_ex_arg: u64 policy_size, then the policy
        # union. policy_size goes in as the room available and comes back as
        # the room used. Kernel structs are native-endian.
        buf = bytearray(8 + _POLICY_BUF_SIZE)
        struct.pack_into('=Q', buf, 0, _POLICY_BUF_SIZE)
        try:
            fcntl.ioctl(fd, _FS_IOC_GET_POLICY_EX, buf, True)
        except OSError as e:
            return _unencrypted() if e.errno in _NOT_ENCRYPTED_ERRNOS else None

        if buf[8] != 2:
            # v1 policy. Its key may live in a process keyring that this ioctl
            # does not report on, so let the CLI answer instead.
            return None
        # struct fscrypt_policy_v2: 8 header bytes, then master_key_identifier.
        key = bytes(buf[16:32])

        # struct fscrypt_get_key_status_arg opens with a fscrypt_key_specifier
        # (u32 type, u32 reserved, then the key bytes).
        arg = bytearray(_KEY_STATUS_ARG_SIZE)
        struct.pack_into('=II', arg, 0, _KEY_SPEC_IDENTIFIER, 0)
        arg[8:8 + len(key)] = key
        fcntl.ioctl(fd, _FS_IOC_GET_KEY_STATUS, arg, True)
        status = struct.unpack_from('=I', arg, _KEY_STATUS_OFFSET)[0]
        return {'encrypted': True, 'unlocked': status == _KEY_STATUS_PRESENT}
    except OSError:
        return None
    finally:
        os.close(fd)


def get_fscrypt_status(dir_path):
    """Whether dir_path is fscrypt-encrypted, and if so whether it's unlocked.

    The kernel answers this for v2 policies with no fscrypt binary involved.
    v1 policies fall back to the CLI, and if that is unavailable the directory
    is reported as plain — the one remaining case that can mislead, which is
    what fscrypt_tool_error() exists to explain.
    """
    status = _probe_kernel_status(dir_path)
    return status if status is not None else _cli_fscrypt_status(dir_path)


MAX_WALK_DIRS = 20000  # pathological-tree guard; a directory costs one
                       # listdir + stat + ioctl, so normal libraries never
                       # come close.


def walk_video_tree(root, rel_path, stats=None, max_dirs=MAX_WALK_DIRS):
    """Walk rel_path and every descendant directory beneath it, yielding
    (dir_rel_path, resolved_dir, videos) per directory actually visited.
    `videos` is scan_directory()'s [(name, size, mtime), ...].

    The rest of the app lists one directory per request and lets the browser
    recurse by clicking, so this is the only recursive traversal in the
    codebase — and it has to hand-roll three things a bare os.walk gets
    wrong:

      * Dot-directories are skipped (via scan_directory), keeping
        .simpleparty/{transcoded,frames,thumbs} and .git out of the walk.
        Without this a re-encode scan would find its own output.
      * Locked fscrypt subtrees are skipped rather than descended into. A
        locked directory is still isdir()-true and listable, but its entries
        are ciphertext names whose contents can't be read.
      * Symlinked subdirectories are never descended into, so there are no
        loops and no escaping the tree sideways.

    `stats`, if given, is mutated in place as the walk proceeds so a caller
    can report live progress: dirs_visited, dirs_locked_skipped,
    dirs_unreadable_skipped, truncated.
    """
    if stats is None:
        stats = {}
    stats.setdefault('dirs_visited', 0)
    stats.setdefault('dirs_locked_skipped', 0)
    stats.setdefault('dirs_unreadable_skipped', 0)
    stats.setdefault('truncated', False)

    if not is_safe_rel_path(rel_path):
        return
    root_resolved = Path(root).resolve()
    start = resolve_path(root, rel_path)
    # is_safe_rel_path is only lexical and resolve_path follows symlinks, so
    # a symlinked rel_path could still land outside root. This walk can cause
    # writes into many .simpleparty/transcoded/ dirs, so confirm containment.
    if start != root_resolved and root_resolved not in start.parents:
        return

    stack = [(rel_path, start)]
    while stack:
        if stats['dirs_visited'] >= max_dirs:
            stats['truncated'] = True
            return
        cur_rel, cur_resolved = stack.pop()

        status = get_fscrypt_status(cur_resolved)
        if status['encrypted'] and not status['unlocked']:
            stats['dirs_locked_skipped'] += 1
            continue

        scanned = scan_directory(cur_resolved)
        if scanned is None:
            stats['dirs_unreadable_skipped'] += 1
            continue
        videos, dir_names = scanned
        stats['dirs_visited'] += 1
        yield cur_rel, cur_resolved, videos

        for name in reversed(dir_names):  # reversed so pop() visits in order
            child = cur_resolved / name
            try:
                if child.is_symlink():
                    continue
            except OSError:
                continue
            stack.append((os.path.join(cur_rel, name) if cur_rel else name, child))


def has_encrypted_dir(root):
    """True if root or one of its immediate children is encrypted. Lets the
    caller keep advice about installing fscrypt to the people it applies to."""
    if get_fscrypt_status(root)['encrypted']:
        return True
    try:
        with os.scandir(root) as entries:
            return any(
                get_fscrypt_status(e.path)['encrypted']
                for e in entries
                if not e.name.startswith('.') and e.is_dir(follow_symlinks=False)
            )
    except OSError:
        return False


# --- fscrypt CLI ---

_FSCRYPT_TTL_SEC = 60.0
_fscrypt_cache = {}
_fscrypt_cache_lock = threading.Lock()

_TOOL_ERROR_UNKNOWN = object()
_tool_error = _TOOL_ERROR_UNKNOWN
_tool_error_lock = threading.Lock()

# Reasons fscrypt can't be used, phrased to drop into "fscrypt is {reason}".
FSCRYPT_NOT_INSTALLED = 'not installed'
FSCRYPT_NOT_SET_UP = 'not set up'


def _fscrypt_cache_key(dir_path):
    try:
        return str(Path(dir_path).resolve())
    except OSError:
        return str(dir_path)


def _invalidate_fscrypt_cache(dir_path):
    key = _fscrypt_cache_key(dir_path)
    with _fscrypt_cache_lock:
        _fscrypt_cache.pop(key, None)


def fscrypt_tool_error():
    """None when the fscrypt CLI can be used, else a short reason why not.

    Probed once and remembered, since installing fscrypt mid-run is not a case
    worth re-checking on every request.
    """
    global _tool_error
    with _tool_error_lock:
        if _tool_error is _TOOL_ERROR_UNKNOWN:
            # Callers report this themselves (startup banner, unlock page), so
            # a plain "not installed" is not logged here — only anomalies are,
            # from _probe_fscrypt_tool.
            _tool_error = _probe_fscrypt_tool()
        return _tool_error


def _probe_fscrypt_tool():
    if shutil.which('fscrypt') is None:
        return FSCRYPT_NOT_INSTALLED
    try:
        # The path argument matters: bare `fscrypt status` exits 0 even with no
        # /etc/fscrypt.conf, while every command that needs the config -- unlock
        # included -- fails. Give it a path and the config check fires.
        # Verified against fscrypt 0.3.6.
        result = subprocess.run(
            ['fscrypt', 'status', '/'], capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return FSCRYPT_NOT_INSTALLED
    except (subprocess.TimeoutExpired, OSError):
        return 'not responding'
    if result.returncode == 0:
        return None
    output = (result.stdout + result.stderr).lower()
    if 'fscrypt.conf' in output or 'fscrypt setup' in output:
        return FSCRYPT_NOT_SET_UP
    # Some other failure -- possibly just that `/` is a filesystem fscrypt
    # cannot report on, which says nothing about unlocking elsewhere. Say so,
    # but leave the unlock form up: a real error beats a guessed one.
    logger.warning('fscrypt status failed: %s', (result.stderr or result.stdout).strip())
    return None


def fscrypt_remedy(tool_error):
    """The shortest true instruction for making unlocking work, as
    (prose, command). The command is None when there isn't one obvious step."""
    if tool_error == FSCRYPT_NOT_INSTALLED:
        return 'Install fscrypt, then run', 'sudo fscrypt setup'
    if tool_error == FSCRYPT_NOT_SET_UP:
        return 'Finish setting fscrypt up by running', 'sudo fscrypt setup'
    return 'Check that fscrypt works by running', 'fscrypt status'


def _cli_fscrypt_status(dir_path):
    """Fallback for policies the kernel probe won't answer for (v1)."""
    if fscrypt_tool_error() is not None:
        return _unencrypted()
    key = _fscrypt_cache_key(dir_path)
    now = time.monotonic()
    with _fscrypt_cache_lock:
        cached = _fscrypt_cache.get(key)
        if cached and now - cached[1] < _FSCRYPT_TTL_SEC:
            return dict(cached[0])
    status = _probe_fscrypt_status(dir_path)
    with _fscrypt_cache_lock:
        _fscrypt_cache[key] = (status, now)
    return dict(status)


def _probe_fscrypt_status(dir_path):
    try:
        result = subprocess.run(
            ['fscrypt', 'status', str(dir_path)],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return _unencrypted()
    except subprocess.TimeoutExpired:
        logger.warning('fscrypt status timed out for %s', dir_path)
        return _unencrypted()
    output = result.stdout + result.stderr
    # Only reached once the kernel has already said there is a policy here, so
    # a failed command is an error to report, not evidence of a plain
    # directory. Reporting it as plain is what hid this from the user before.
    if result.returncode != 0:
        logger.warning('fscrypt status failed for %s: %s', dir_path, output.strip())
        return _unencrypted()
    if 'is encrypted with fscrypt' not in output:
        return _unencrypted()
    return {'encrypted': True, 'unlocked': bool(re.search(r'Unlocked:\s*Yes', output))}


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
