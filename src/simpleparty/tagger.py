"""Video tagging: keyframe extraction and tag file I/O."""

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

logger = logging.getLogger('simpleparty.tagger')

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

SIMPLEPARTY_DIR = '.simpleparty'
TAGS_FILENAME = 'tags.json'
MODEL_FILENAME = 'model.pt'
THUMB_DIR = 'thumbs'
FRAMES_DIR = SIMPLEPARTY_DIR + '/frames'

# Legacy paths for backward compat
_LEGACY_TAGS = '.simpleparty-tags.json'
_LEGACY_MODEL = '.simpleparty-model.pt'


# --- Tag file I/O ---

# Per-directory cache of parsed tags files, keyed on the file's
# (mtime_ns, size) so out-of-band writes are still picked up.
_tags_cache = {}        # str(dir) -> (stat_key, tags_dict, lower_index)
_tags_cache_lock = threading.Lock()
_dir_write_locks = {}   # str(dir) -> threading.Lock, created under _tags_cache_lock
_TAGS_CACHE_MAX = 64    # directories; parsed maps can be large


def _tags_cache_put(key, value):
    """Insert under _tags_cache_lock with oldest-first eviction."""
    _tags_cache[key] = value
    while len(_tags_cache) > _TAGS_CACHE_MAX:
        _tags_cache.pop(next(iter(_tags_cache)))


def _sp_dir(directory_path):
    """Return the .simpleparty directory path, creating it if needed."""
    d = Path(directory_path) / SIMPLEPARTY_DIR
    d.mkdir(exist_ok=True)
    return d


def _tags_file(directory_path):
    """The tags file to read: current location, or the legacy flat file."""
    tags_file = Path(directory_path) / SIMPLEPARTY_DIR / TAGS_FILENAME
    if tags_file.exists():
        return tags_file
    legacy = Path(directory_path) / _LEGACY_TAGS
    return legacy if legacy.exists() else tags_file


def _stat_key(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _build_lower_index(tags):
    return {
        name: frozenset(t.lower() for t in entry.get('tags', []))
        for name, entry in tags.items()
    }


def load_tags(directory_path):
    """Load tags JSON for a directory, or return empty dict.

    The returned dict is a shared cache object: treat it as READ-ONLY and
    go through update_tags() for modifications.
    """
    return load_tags_index(directory_path)[0]


def load_tags_index(directory_path):
    """Return (tags, lower_index); lower_index maps video name to a frozenset
    of lowercased tags. Both are shared cache objects: read-only."""
    key = str(Path(directory_path))
    tags_file = _tags_file(directory_path)
    stat_key = _stat_key(tags_file)
    with _tags_cache_lock:
        cached = _tags_cache.get(key)
        if cached and cached[0] == stat_key:
            return cached[1], cached[2]
    if stat_key is None:
        tags = {}
    else:
        try:
            with open(tags_file, 'r') as f:
                tags = json.load(f)
        except (json.JSONDecodeError, OSError):
            tags = {}
    index = _build_lower_index(tags)
    with _tags_cache_lock:
        _tags_cache_put(key, (stat_key, tags, index))
    return tags, index


def save_tags(directory_path, tags):
    """Atomically write tags JSON for a directory (write-through cache)."""
    sp = _sp_dir(directory_path)
    tags_file = sp / TAGS_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(sp), suffix='.tmp', prefix='tags-',
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(tags, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(tags_file))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    with _tags_cache_lock:
        _tags_cache_put(str(Path(directory_path)), (
            _stat_key(tags_file), tags, _build_lower_index(tags),
        ))


def _write_lock(directory_path):
    key = str(Path(directory_path))
    with _tags_cache_lock:
        lock = _dir_write_locks.get(key)
        if lock is None:
            lock = _dir_write_locks[key] = threading.Lock()
    return lock


def update_tags(directory_path, transform):
    """Atomically read-modify-write a directory's tags file.

    `transform` receives a one-level copy of the latest tags dict and
    returns the dict to persist (it may mutate the copy and its entry
    dicts, but must not mutate nested lists in place). Serialized per
    directory so concurrent writers cannot lose updates. Returns the
    persisted dict.
    """
    with _write_lock(directory_path):
        current = load_tags(directory_path)
        updated = transform({k: dict(v) for k, v in current.items()})
        save_tags(directory_path, updated)
        return updated


_thumbs_cache = {}  # str(thumbs dir) -> (mtime_ns, frozenset of names)
_thumbs_cache_lock = threading.Lock()
_THUMBS_CACHE_MAX = 64


def list_thumbs(directory_path):
    """Names of videos with a thumbnail. Cached on the thumbs directory's
    mtime (writing a thumbnail bumps it), so repeat requests — e.g. each
    pagination fragment — cost one stat instead of a scandir."""
    thumbs_dir = Path(directory_path) / SIMPLEPARTY_DIR / THUMB_DIR
    try:
        mtime_ns = os.stat(thumbs_dir).st_mtime_ns
    except OSError:
        return frozenset()
    key = str(thumbs_dir)
    with _thumbs_cache_lock:
        cached = _thumbs_cache.get(key)
        if cached and cached[0] == mtime_ns:
            return cached[1]
    try:
        with os.scandir(thumbs_dir) as it:
            names = frozenset(e.name[:-4] for e in it if e.name.endswith('.jpg'))
    except OSError:
        return frozenset()
    with _thumbs_cache_lock:
        _thumbs_cache[key] = (mtime_ns, names)
        while len(_thumbs_cache) > _THUMBS_CACHE_MAX:
            _thumbs_cache.pop(next(iter(_thumbs_cache)))
    return names


def videos_with_frames(directory_path):
    """Names of videos with at least one extracted full-res frame."""
    frames_dir = Path(directory_path) / FRAMES_DIR
    try:
        with os.scandir(frames_dir) as it:
            return frozenset(
                e.name[:-4].rsplit('.f', 1)[0]
                for e in it
                if e.name.endswith('.jpg') and '.f' in e.name
            )
    except OSError:
        return frozenset()


def untagged_videos(directory_path, existing_tags, names=None):
    """Return list of video filenames in directory not yet tagged.

    `names` lets a caller that already listed the directory skip the
    redundant listdir.
    """
    if names is None:
        try:
            names = sorted(os.listdir(directory_path))
        except OSError:
            return []
    result = []
    for name in names:
        if name.startswith('.'):
            continue
        if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
            entry = existing_tags.get(name)
            if not entry or entry.get('status') == 'rejected':
                result.append(name)
    return result


def is_starred(entry):
    """Return True if a tags-map entry is starred."""
    return bool(entry) and bool(entry.get('starred'))


def _entry_is_empty(entry):
    """An entry with no tags, no rejected_tags, no starred flag, and no cached duration is dead weight."""
    if not entry:
        return True
    if entry.get('tags'):
        return False
    if entry.get('rejected_tags'):
        return False
    if entry.get('starred'):
        return False
    if entry.get('duration') is not None:
        return False
    return True


def set_starred(tags, video_name, starred):
    """Mutate `tags` to set/clear starred for `video_name`. Returns the updated dict.

    Removes the entry entirely if it ends up empty after clearing.
    """
    entry = tags.get(video_name, {})
    if starred:
        entry['starred'] = True
        tags[video_name] = entry
    else:
        entry.pop('starred', None)
        if _entry_is_empty(entry):
            tags.pop(video_name, None)
        else:
            tags[video_name] = entry
    return tags


def confirmed_entries(tags):
    """Return dict of entries with status != 'suggested'."""
    return {
        name: entry for name, entry in tags.items()
        if entry.get('status', 'confirmed') != 'suggested'
        and entry.get('tags')
    }


def training_entries(tags):
    """Return dict of entries useful for training (confirmed or rejected).

    Includes entries with confirmed tags and/or rejected_tags.
    Excludes entries with status='suggested' (not yet reviewed).
    """
    return {
        name: entry for name, entry in tags.items()
        if entry.get('status', 'confirmed') not in ('suggested',)
        and (entry.get('tags') or entry.get('rejected_tags'))
    }


def rewrite_tags(tags_map, mapping):
    """Pure directory-level tag rewrite: rename/merge/remove across all entries.

    `mapping` maps a *lowercased* source tag to either a target string
    (rename — renaming onto an existing tag merges them) or None (remove). For
    each video entry a new entry is produced where:
      - each tag whose lowercased form is a mapping key is replaced by the
        target (or dropped if None); order is preserved and duplicates are
        removed case-insensitively (so a merge yields a single tag);
      - `suggest_scores` keys are rewritten the same way; on a collision the
        higher score wins, and dropped tags' scores are removed;
      - `status`, `starred`, and every other field are preserved untouched;
      - entries are kept even if their `tags` list becomes empty.

    The input is not mutated. Tags not named in `mapping` pass through, so an
    unknown key is a no-op.
    """
    def map_target(tag):
        """Return (keep, target): keep=False means drop this tag."""
        key = tag.lower()
        if key in mapping:
            target = mapping[key]
            return (target is not None, target)
        return (True, tag)

    result = {}
    for name, entry in tags_map.items():
        new_entry = dict(entry)
        if 'tags' in entry:
            new_tags = []
            seen = set()
            for tag in entry['tags']:
                keep, target = map_target(tag)
                if not keep:
                    continue
                dedup_key = target.lower()
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                new_tags.append(target)
            new_entry['tags'] = new_tags
        scores = entry.get('suggest_scores')
        if isinstance(scores, dict):
            new_scores = {}
            for tag, score in scores.items():
                keep, target = map_target(tag)
                if not keep:
                    continue
                if target in new_scores:
                    new_scores[target] = max(new_scores[target], score)
                else:
                    new_scores[target] = score
            new_entry['suggest_scores'] = new_scores
        result[name] = new_entry
    return result


# --- Keyframe extraction ---

def _is_dark_frame(jpeg_path, threshold=20):
    """Check if a JPEG is nearly black by sampling pixel bytes."""
    try:
        data = Path(jpeg_path).read_bytes()
        if len(data) < 1000:
            logger.debug('dark frame (too small %d bytes): %s', len(data), jpeg_path)
            return True
        start = len(data) // 3
        end = 2 * len(data) // 3
        sample = data[start:end:10]
        if not sample:
            return True
        avg = sum(sample) / len(sample)
        if avg < threshold:
            logger.debug('dark frame (avg %.1f < %d): %s', avg, threshold, jpeg_path)
            return True
        return False
    except OSError:
        logger.debug('dark frame (read error): %s', jpeg_path)
        return True


def _get_duration(video_path):
    """Get video duration in seconds via ffprobe."""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'csv=p=0',
        str(video_path),
    ]
    try:
        t0 = time.monotonic()
        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        dur = float(result.stdout.strip())
        logger.debug('duration %.1fs for %s (%.2fs)', dur, video_path, time.monotonic() - t0)
        return dur
    except (subprocess.TimeoutExpired, ValueError, OSError):
        logger.debug('duration failed for %s', video_path)
        return 0.0


def extract_keyframes(video_path, max_frames=3):
    """Extract frames at evenly spaced positions through the video.

    For max_frames=3, extracts at 25%, 50%, 75% of duration.
    For max_frames=1, extracts at 50%.
    Returns list of JPEG paths. Caller must clean up the temp directory.
    """
    t0 = time.monotonic()
    tmpdir = tempfile.mkdtemp(prefix='simpleparty-frames-')
    duration = _get_duration(video_path)
    if duration <= 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return []

    positions = [duration * (i + 1) / (max_frames + 1) for i in range(max_frames)]
    timeout = max(30, int(duration / 10))
    logger.debug('extracting %d keyframes from %s at positions %s',
                 max_frames, video_path, ['%.1fs' % p for p in positions])

    for idx, pos in enumerate(positions):
        out_path = os.path.join(tmpdir, f'frame_{idx:02d}.jpg')
        cmd = [
            'ffmpeg', '-ss', f'{pos:.2f}',
            '-t', '30',
            '-skip_frame', 'nokey',
            '-i', str(video_path),
            '-frames:v', '1',
            '-q:v', '4',
            out_path,
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass

    frames = sorted(Path(tmpdir).glob('frame_*.jpg'))
    usable = [f for f in frames if not _is_dark_frame(f)]
    logger.debug('keyframes done for %s: %d usable of %d extracted (%.2fs)',
                 video_path, len(usable), len(frames), time.monotonic() - t0)
    if not usable:
        # Nothing for the caller to consume or clean up — drop the temp dir here.
        shutil.rmtree(tmpdir, ignore_errors=True)
    return usable[:max_frames]


def model_path(directory_path):
    """Return path to the trained model, checking legacy location as fallback."""
    p = Path(directory_path) / SIMPLEPARTY_DIR / MODEL_FILENAME
    if p.exists():
        return p
    legacy = Path(directory_path) / _LEGACY_MODEL
    return legacy if legacy.exists() else p


def thumb_path(directory_path, video_name):
    """Return path where a video's thumbnail should live."""
    return Path(directory_path) / SIMPLEPARTY_DIR / THUMB_DIR / f'{video_name}.jpg'


def extract_frame(video_path, position, out_path, timeout=30):
    """Extract a single full-res frame at *position* seconds. Returns True on success."""
    cmd = [
        'ffmpeg', '-y', '-ss', f'{position:.2f}',
        '-t', '10',
        '-skip_frame', 'nokey',
        '-i', str(video_path),
        '-frames:v', '1',
        '-q:v', '4',
        str(out_path),
    ]
    try:
        t0 = time.monotonic()
        subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        ok = Path(out_path).exists() and not _is_dark_frame(out_path)
        logger.debug('extract_frame %s @%.1fs -> %s (%s, %.2fs)',
                     video_path, position, out_path,
                     'ok' if ok else 'failed', time.monotonic() - t0)
        return ok
    except subprocess.TimeoutExpired:
        logger.debug('extract_frame timeout for %s @%.1fs', video_path, position)
        return False


def _downscale_frame(frame_path, output_path):
    """Downscale a JPEG to 320px width for use as a thumbnail. Returns True on success."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg', '-i', str(frame_path),
        '-vf', 'scale=320:-1',
        '-q:v', '6',
        '-y', str(output_path),
    ]
    try:
        t0 = time.monotonic()
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        logger.debug('downscale timeout: %s', frame_path)
        return False
    out = Path(output_path)
    if not out.exists():
        logger.debug('downscale failed (no output): %s', frame_path)
        return False
    if _is_dark_frame(out):
        out.unlink()
        return False
    logger.debug('downscaled %s -> %s (%.2fs)', frame_path, output_path, time.monotonic() - t0)
    return True


def extract_thumbnail(video_path, output_path):
    """Extract a thumbnail by downscaling a full-res frame.

    Reuses an existing full-res frame from the frames dir if available,
    otherwise extracts one first. Returns True on success.
    """
    video_path = Path(video_path)
    video_name = video_path.name
    frames_dir = video_path.parent / FRAMES_DIR
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Look for an existing full-res frame
    existing = sorted(frames_dir.glob(f'{video_name}.f*.jpg'))
    if existing:
        logger.debug('thumbnail reusing existing frame %s', existing[0])
        return _downscale_frame(existing[0], output_path)

    # Extract a new full-res frame, trying progressively earlier positions
    # (earlier positions are faster to seek to on slow storage)
    logger.debug('thumbnail extracting new frame for %s', video_name)
    duration = _get_duration(video_path)
    if duration <= 0:
        return False

    positions = [duration / 2, duration / 4, min(10, duration / 4), 1]
    timeout_per = min(45, max(20, int(duration / 15)))
    frame_path = frames_dir / f'{video_name}.f0.jpg'
    for pos in positions:
        if extract_frame(str(video_path), pos, str(frame_path), timeout=timeout_per):
            break
    else:
        return False

    return _downscale_frame(str(frame_path), output_path)
