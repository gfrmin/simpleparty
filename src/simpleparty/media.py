"""Video serving (transcode/remux/stream) and thumbnail generation."""

import logging
import os
import subprocess
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
    if not _config['has_ffmpeg'] or not videos:
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
    if dir_str in jobs.duration_jobs:
        return True
    jobs.duration_jobs.add(dir_str)
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
        extract_frame, _downscale_frame,
        list_thumbs, videos_with_frames,
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


def _maybe_start_thumbs(directory, videos, thumbs=frozenset(), frames=frozenset()):
    """Spawn background thumbnail generation if needed and not already running.

    `thumbs`/`frames` are the name sets from tagger.list_thumbs() /
    tagger.videos_with_frames(), computed once by the caller.
    """
    if not _config['has_ffmpeg'] or not videos:
        return
    dir_str = str(directory)
    if dir_str in jobs.thumb_jobs:
        return
    missing = any(
        v['name'] not in thumbs or v['name'] not in frames for v in videos
    )
    if not missing:
        return
    jobs.thumb_jobs.add(dir_str)
    logger.debug('starting background thumbnail thread for %s (%d videos to check)',
                 directory, len(videos))
    t = threading.Thread(
        target=_generate_thumbnails,
        args=(directory, videos),
        daemon=True,
    )
    t.start()
