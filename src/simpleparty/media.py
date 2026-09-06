"""Video serving (transcode/remux/stream) and thumbnail generation."""

import collections
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from simpleparty import jobs
from simpleparty.state import CONFIG as _config, BROWSER_NATIVE

logger = logging.getLogger('simpleparty.media')


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
_probe_cache_lock = threading.Lock()
# Bounded because a recursive tree scan probes every uncached video in one
# pass; unbounded, this would retain an entry per video for the process's
# life. Entries are tiny tuples, so the cap costs ~1MB at worst. Same
# cap-and-evict-oldest shape as tagger._thumbs_cache.
_PROBE_CACHE_MAX = 4096


def _probe_streams(path):
    """Return (video_codec, audio_codec) for a file, or (None, None) on failure. Cached by (path, mtime, size)."""
    try:
        st = path.stat()
    except OSError:
        return (None, None)
    key = (str(path), st.st_mtime, st.st_size)
    with _probe_cache_lock:
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
    with _probe_cache_lock:
        _probe_cache[key] = (vcodec, acodec)
        while len(_probe_cache) > _PROBE_CACHE_MAX:
            _probe_cache.pop(next(iter(_probe_cache)))
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

    proc = None
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
        if proc is not None:
            proc.kill()
    except Exception:
        if proc is not None and proc.poll() is None:
            proc.kill()
        # Headers are already out; terminate the chunked body so a
        # keep-alive client isn't left waiting forever.
        try:
            handler.wfile.write(b'0\r\n\r\n')
        except OSError:
            pass


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


# --- Background pre-transcode cache ---
#
# Non-destructive: never touches the source. A background job (triggered on
# browse, prioritized on play) re-encodes 'reencode'-plan videos once into
# .simpleparty/transcoded/<name>.mp4, so later playbacks skip the live
# ffmpeg pipe in _serve_transcoded entirely. Freshness is just "does the
# cache file exist and is its mtime >= the source's" - no tags.json bookkeeping.

TRANSCODED_DIR = 'transcoded'  # sibling of tagger.THUMB_DIR under .simpleparty/

_REENCODE_MIN_TIMEOUT = 60
_REENCODE_TIMEOUT_PER_SEC = 4.0  # generous: -preset medium on modest hardware
_REENCODE_TIMEOUT_UNKNOWN_DURATION = 1800

# ffmpeg emits a -progress block roughly every 0.5s; there's no value in
# updating shared state faster than any client polls it.
_REENCODE_PROGRESS_MIN_INTERVAL = 1.0
_REENCODE_STDERR_TAIL = 40  # lines kept for diagnostics

_OUT_TIME_RE = re.compile(r'(-?\d+):(\d{2}):(\d{2})(?:\.(\d+))?')
_SPEED_RE = re.compile(r'([\d.]+)x')


def _parse_progress_line(line, scratch):
    """Fold one line of `ffmpeg -progress` output into `scratch`, which the
    caller keeps across calls. Returns the completed block (and clears
    scratch) once the block-terminating `progress=` key arrives, else None.

    Pure: no I/O, so the parsing is unit-testable without running ffmpeg.
    """
    line = line.strip()
    if not line or '=' not in line:
        return None
    key, _, value = line.partition('=')
    scratch[key] = value
    if key == 'progress':
        block = dict(scratch)
        scratch.clear()
        return block
    return None


def _progress_block_to_metrics(block):
    """One completed -progress block -> (elapsed_seconds|None, speed|None).

    Prefers out_time_us, whose name matches its actual unit, and falls back
    to the out_time "HH:MM:SS.ffffff" string for older builds. out_time_ms is
    deliberately never read: ffmpeg populates it with MICROseconds despite the
    name, so trusting it would under-report elapsed time by 1000x.
    """
    elapsed = None
    out_us = block.get('out_time_us')
    if out_us not in (None, 'N/A'):
        try:
            elapsed = int(out_us) / 1_000_000
        except ValueError:
            pass
    if elapsed is None:
        m = _OUT_TIME_RE.match(block.get('out_time') or '')
        if m:
            hours, minutes, seconds, frac = m.groups()
            elapsed = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
            if frac:
                elapsed += int(frac[:6].ljust(6, '0')) / 1_000_000

    speed = None
    m = _SPEED_RE.match((block.get('speed') or '').strip())
    if m:
        try:
            speed = float(m.group(1))
        except ValueError:
            pass
    return elapsed, speed


def _transcoded_path(directory, name):
    """Where a video's cached re-encode should live. Appends (not replaces)
    the suffix, like tagger.thumb_path, so e.g. movie.mkv -> movie.mkv.mp4
    can't collide with a movie.mp4 that also needed caching."""
    from simpleparty.tagger import SIMPLEPARTY_DIR
    return Path(directory) / SIMPLEPARTY_DIR / TRANSCODED_DIR / f'{name}.mp4'


def _cached_transcode(path):
    """Return the cached re-encoded Path for `path` if it exists and is at
    least as new as the source; else None."""
    cached = _transcoded_path(path.parent, path.name)
    try:
        src_mtime = path.stat().st_mtime
        cached_mtime = cached.stat().st_mtime
    except OSError:
        return None
    if cached_mtime < src_mtime:
        return None
    return cached


def discard_cached_transcode(path):
    """Best-effort removal of a video's cached re-encode, for when the source
    is deleted. Unlinks unconditionally rather than via _cached_transcode: a
    *stale* cache entry is just as orphaned once the source is gone, and
    freshness is judged against a source that no longer exists.

    Unlike a thumbnail, this file is a second full copy of the video, so
    leaving it behind would quietly retain the bytes with nothing left in the
    UI to ever surface or invalidate them.
    """
    try:
        _transcoded_path(path.parent, path.name).unlink(missing_ok=True)
    except OSError as e:
        logger.warning('could not discard cached transcode for %s: %s', path, e)


def _reencode_video(path_str):
    """Background worker body: re-encode into .simpleparty/transcoded/. Uses
    a slower preset than the live path (media.py's _serve_transcoded) since
    this isn't latency-constrained, and +faststart since the output is a
    normal seekable file (served via _stream_range/_stream_file) rather than
    a live pipe. Never touches the source. Returns True on success."""
    from simpleparty.tagger import _get_duration

    path = Path(path_str)
    try:
        if not path.is_file():
            return False
    except OSError:
        return False

    if _cached_transcode(path) is not None:
        return True  # already fresh (e.g. a redundant enqueue)

    dest = _transcoded_path(path.parent, path.name)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Typically a locked fscrypt directory (ENOKEY), a read-only mount or
        # a full disk. Record why: a bare False left the UI reporting a
        # failure with no reason at all.
        reason = f'could not create the transcode cache directory: {e}'
        logger.warning('reencode failed for %s (%s)', path, reason)
        jobs.report_reencode_progress(path_str, error=reason)
        return False

    duration = _get_duration(path)
    timeout = (max(_REENCODE_MIN_TIMEOUT, duration * _REENCODE_TIMEOUT_PER_SEC)
               if duration > 0 else _REENCODE_TIMEOUT_UNKNOWN_DURATION)

    jobs.report_reencode_progress(path_str, percent=0 if duration > 0 else None)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(dest.parent), suffix='.tmp', prefix='reencode-',
    )
    os.close(tmp_fd)
    try:
        returncode, timed_out, stderr_tail = _run_reencode(
            path_str, path, tmp_path, duration, timeout)
        if returncode != 0:
            if timed_out:
                reason = f'timed out after {timeout:.0f}s'
            else:
                tail = ' '.join(line.strip() for line in stderr_tail)[-200:]
                reason = f'ffmpeg exited {returncode}' + (f': {tail}' if tail else '')
            logger.warning('reencode failed for %s (%s)', path, reason)
            jobs.report_reencode_progress(path_str, error=reason)
            return False
        if not path.exists():
            # Source vanished mid-encode; discard rather than leave an
            # orphaned cache file with no source left to invalidate it.
            return False
        os.replace(tmp_path, str(dest))
        jobs.report_reencode_progress(path_str, percent=100)
        return True
    except OSError as e:
        logger.warning('reencode OSError for %s: %s', path, e)
        jobs.report_reencode_progress(path_str, error=str(e) or e.__class__.__name__)
        return False
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass


def _run_reencode(path_str, path, tmp_path, duration, timeout):
    """Run the encode, streaming ffmpeg's -progress output into the job's
    live status. Returns (returncode, timed_out, stderr_tail).

    Both pipes are drained concurrently — stdout here, stderr on a helper
    thread — because leaving either unread risks filling its OS buffer and
    deadlocking ffmpeg against a reader blocked on the other one. (The live
    path in _serve_transcoded leaves stderr undrained; don't copy that.)
    """
    cmd = [
        'ffmpeg', '-i', str(path),
        '-c:v', 'libx264', '-preset', 'medium', '-pix_fmt', 'yuv420p', '-crf', '23',
        '-c:a', 'aac', '-b:a', '160k',
        '-movflags', '+faststart',
        # -nostats is required alongside -progress: ffmpeg's human-readable
        # stats line is governed by -stats/-nostats, not by -loglevel.
        '-f', 'mp4', '-loglevel', 'error', '-nostats',
        '-progress', 'pipe:1', '-y', tmp_path,
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace',
    )

    timed_out = threading.Event()

    def _kill():
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill)
    watchdog.daemon = True
    watchdog.start()

    stderr_tail = collections.deque(maxlen=_REENCODE_STDERR_TAIL)

    def _drain_stderr():
        try:
            for line in proc.stderr:
                stderr_tail.append(line)
        except (ValueError, OSError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    scratch = {}
    last_report = 0.0
    try:
        for line in proc.stdout:
            block = _parse_progress_line(line, scratch)
            if block is None:
                continue
            now = time.monotonic()
            if now - last_report < _REENCODE_PROGRESS_MIN_INTERVAL:
                continue
            last_report = now
            elapsed, speed = _progress_block_to_metrics(block)
            percent = None
            eta = None
            if elapsed is not None and duration > 0:
                percent = int(min(99, elapsed * 100 / duration))
                if speed and speed > 0:
                    eta = max(0.0, (duration - elapsed) / speed)
            jobs.report_reencode_progress(
                path_str, percent=percent, speed=speed, eta=eta)
    except (ValueError, OSError):
        pass
    finally:
        watchdog.cancel()
        returncode = proc.wait()
        stderr_thread.join(timeout=5)

    return returncode, timed_out.is_set(), list(stderr_tail)


def _scan_for_reencode(directory, videos, dir_mtime_ns):
    """Background worker: enqueue any video whose transcode plan is
    'reencode' and that doesn't already have a fresh cache."""
    try:
        for v in videos:
            video_path = Path(directory) / v['name']
            try:
                if not video_path.is_file():
                    continue
            except OSError:
                continue
            if _cached_transcode(video_path) is not None:
                continue
            if _transcode_plan(video_path) != 'reencode':
                continue
            jobs.enqueue_reencode(str(video_path))
        jobs.ensure_reencode_worker()
    finally:
        jobs.mark_dir_scanned(str(directory), dir_mtime_ns)
        jobs.reencode_scan_jobs.discard(str(directory))


MAX_TREE_SCAN_VIDEOS = 5000  # the real cost driver: up to 2 ffprobe spawns
                             # per uncached video


def _scan_tree_for_reencode(root, rel_path, job):
    """Background worker: recursively find videos needing a re-encode across
    rel_path's whole directory tree, filling `job` in place so the UI can poll
    live progress. Deliberately never enqueues — confirm-then-queue is a
    separate, explicit step so a misclick can't start hours of CPU work.

    Only this thread writes to `job` while it runs (guaranteed by
    claim_tree_scan_job), so no lock is needed — the same convention the
    tag/download progress dicts already use.
    """
    from simpleparty.library import walk_video_tree

    stats = {}
    try:
        for _dir_rel, resolved_dir, videos in walk_video_tree(root, rel_path, stats=stats):
            try:
                dir_mtime_ns = resolved_dir.stat().st_mtime_ns
            except OSError:
                dir_mtime_ns = None
            dir_complete = True
            for name, _size, _mtime in videos:
                if job['scanned_videos'] >= MAX_TREE_SCAN_VIDEOS:
                    job['truncated'] = True
                    dir_complete = False
                    break
                job['scanned_videos'] += 1
                video_path = resolved_dir / name
                if _cached_transcode(video_path) is not None:
                    continue
                if _transcode_plan(video_path) != 'reencode':
                    continue
                job['found'].append(str(video_path))
            job['scanned_dirs'] = stats['dirs_visited']
            # Only record the directory as scanned-at-this-mtime if its whole
            # video list was inspected, so a truncated scan can't permanently
            # suppress the automatic scanner from revisiting the tail.
            if dir_complete and dir_mtime_ns is not None:
                jobs.mark_dir_scanned(str(resolved_dir), dir_mtime_ns)
            if job['truncated']:
                break
        job['truncated'] = job['truncated'] or stats.get('truncated', False)
        job['skipped_locked'] = stats.get('dirs_locked_skipped', 0)
        job['skipped_unreadable'] = stats.get('dirs_unreadable_skipped', 0)
    except Exception as e:
        logger.exception('tree reencode scan failed for %s', rel_path)
        job['error'] = str(e) or e.__class__.__name__
    finally:
        job['running'] = False
        job['finished_at'] = time.time()


def _maybe_start_reencode_scan(directory, videos):
    """Spawn a background scan that enqueues 'reencode'-plan videos lacking a
    fresh cache. Gated on has_ffmpeg specifically (not vlc), matching how
    _maybe_start_thumbs gates solely on has_ffmpeg.

    Re-scans are throttled on the directory's own mtime (has anything been
    added/removed/renamed since the last scan?) rather than re-run on every
    call: _transcode_plan does a real file read (_is_mpegts) per .mp4 plus
    ffprobe for anything not yet in _probe_cache, and the browse page's
    duration-probe-style 4s poll would otherwise re-run that over every
    video in the directory for as long as any encode is in flight.
    """
    if not (_config['has_ffmpeg'] and _config['allow_transcode']
            and _config.get('allow_pretranscode', True) and videos):
        return
    dir_str = str(directory)
    try:
        dir_mtime_ns = Path(directory).stat().st_mtime_ns
    except OSError:
        return
    with jobs.job_sets_lock:
        last = jobs.reencode_scanned_at.get(dir_str)
    if last == dir_mtime_ns:
        return  # nothing added/removed since the last scan
    if not jobs.try_claim(jobs.reencode_scan_jobs, dir_str):
        return
    threading.Thread(
        target=_scan_for_reencode, args=(directory, videos, dir_mtime_ns), daemon=True,
    ).start()


# --- Duration probing ---

_DURATION_FLUSH_EVERY = 25


def _merge_durations(batch):
    """Transform that writes only duration/duration_mtime for its own
    entries, so concurrent tag/star edits survive."""
    def _apply(tags):
        for name, dur, mtime in batch:
            entry = tags.get(name, {})
            entry['duration'] = dur
            entry['duration_mtime'] = mtime
            tags[name] = entry
        return tags
    return _apply


def _probe_durations(resolved, items):
    """Background worker: ffprobe each (name, path, mtime), flushing the
    results into tags.json in batches."""
    from simpleparty.tagger import _get_duration, update_tags
    try:
        t0 = time.monotonic()
        batch = []
        for name, path, mtime in items:
            batch.append((name, _get_duration(path), mtime))
            if len(batch) >= _DURATION_FLUSH_EVERY:
                update_tags(resolved, _merge_durations(batch))
                batch = []
        if batch:
            update_tags(resolved, _merge_durations(batch))
        logger.debug('duration probe done for %s: %d videos (%.1fs)',
                     resolved, len(items), time.monotonic() - t0)
    finally:
        jobs.duration_jobs.discard(str(resolved))


def _maybe_start_durations(resolved, videos, tags_map):
    """Spawn a background duration probe for videos lacking a valid cached
    duration. Returns True while results are still pending."""
    if not _config['has_ffmpeg'] or not _config['allow_tag'] or not videos:
        # Probing persists results into .simpleparty/tags.json; with
        # tagging disabled we must not write sidecar files into the tree.
        return False
    tags_map = tags_map or {}
    missing = []
    for v in videos:
        entry = tags_map.get(v['name'], {})
        if entry.get('duration') is None or entry.get('duration_mtime') != v.get('mtime', 0.0):
            missing.append((v['name'], Path(resolved) / v['name'], v.get('mtime', 0.0)))
    if not missing:
        return False
    dir_str = str(resolved)
    if not jobs.try_claim(jobs.duration_jobs, dir_str):
        return True
    threading.Thread(
        target=_probe_durations, args=(resolved, missing), daemon=True,
    ).start()
    return True


# --- Thumbnail generation ---

def _generate_thumbnails(directory, videos):
    """Background worker: extract thumbnails and backing full-res frames.

    For each video, ensures both a full-res frame in frames/ and a
    thumbnail in thumbs/ exist. Migrates legacy thumbnails that lack
    a backing frame by extracting the full-res frame.
    """
    from simpleparty.tagger import (
        FRAMES_DIR, thumb_path, extract_thumbnail,
        _downscale_frame, list_thumbs, videos_with_frames,
    )
    try:
        t0 = time.monotonic()
        frames_dir = Path(directory) / FRAMES_DIR
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Pre-scan: classify each video (two scandirs instead of per-video
        # exists()/glob checks)
        existing_thumbs = list_thumbs(directory)
        existing_frames = videos_with_frames(directory)
        skip = []
        need_frame = []       # no full-res frame (extract frame + maybe thumb)
        need_thumb_only = []   # frame exists but no thumbnail
        for v in videos:
            name = v['name']
            has_frame = name in existing_frames
            video_file = Path(directory) / name
            if not video_file.exists():
                continue
            if has_frame and name in existing_thumbs:
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
        jobs.thumb_jobs.discard(str(directory))


def _maybe_start_thumbs(directory, videos, thumbs=frozenset()):
    """Spawn background thumbnail generation if needed and not already running.

    `thumbs` is the name set from tagger.list_thumbs(), computed once by
    the caller (it needs it for rendering anyway).
    """
    if not _config['has_ffmpeg'] or not videos:
        return
    dir_str = str(directory)
    if dir_str in jobs.thumb_jobs:  # cheap pre-check; try_claim is authoritative
        return
    from simpleparty.tagger import videos_with_frames
    frames = videos_with_frames(directory)
    missing = any(
        v['name'] not in thumbs or v['name'] not in frames for v in videos
    )
    if not missing:
        return
    if not jobs.try_claim(jobs.thumb_jobs, dir_str):
        return
    logger.debug('starting background thumbnail thread for %s (%d videos to check)',
                 directory, len(videos))
    t = threading.Thread(
        target=_generate_thumbnails,
        args=(directory, videos),
        daemon=True,
    )
    t.start()
