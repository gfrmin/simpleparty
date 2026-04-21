"""Download videos from URLs into a directory using yt-dlp."""

import logging
import re
from pathlib import Path

logger = logging.getLogger('simpleparty.downloader')


_URL_RE = re.compile(r'^https?://[^\s<>"\']+$')
_MAX_URL = 2048

_probed = False
_available = False


def is_available():
    """Is yt-dlp importable? Probed once, cached."""
    global _probed, _available
    if not _probed:
        try:
            import yt_dlp  # noqa: F401
            _available = True
        except ImportError:
            _available = False
        _probed = True
    return _available


def _require_ytdlp():
    try:
        import yt_dlp
        return yt_dlp
    except ImportError:
        from simpleparty import __version__
        raise RuntimeError(
            'yt-dlp is required for the download feature. '
            f'Install with: uvx simpleparty[download]=={__version__}'
        )


def validate_url(url):
    """Return cleaned URL or raise ValueError."""
    url = (url or '').strip()
    if not url:
        raise ValueError('URL is required')
    if len(url) > _MAX_URL:
        raise ValueError('URL too long')
    if not _URL_RE.match(url):
        raise ValueError('URL must start with http:// or https://')
    return url


def is_path_within(root, candidate):
    """True iff candidate resolves inside root."""
    try:
        root_p = Path(root).resolve()
        cand_p = Path(candidate).resolve()
    except OSError:
        return False
    return cand_p == root_p or root_p in cand_p.parents


def _apply_progress_hook(d, progress, state):
    """Map a yt-dlp progress_hook dict onto the shared progress dict.

    Extracted as a pure-ish function for unit testing. `state` is a scratchpad
    dict shared across hook calls (used to carry the final filename through
    postprocessing steps).
    """
    status = d.get('status')
    progress['status'] = status or ''
    if status == 'downloading':
        progress['phase'] = 'downloading'
        progress['filename'] = d.get('filename') or ''
        done = d.get('downloaded_bytes') or 0
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        progress['downloaded_bytes'] = done
        progress['total_bytes'] = total
        progress['speed'] = d.get('speed')
        progress['eta'] = d.get('eta')
        progress['percent'] = int(done * 100 / total) if total else 0
    elif status == 'finished':
        progress['phase'] = 'post-processing'
        progress['filename'] = d.get('filename') or progress.get('filename', '')
        progress['percent'] = 100
        fn = d.get('filename')
        if fn:
            state['final_path'] = fn
    elif status == 'error':
        progress['phase'] = 'error'


def _apply_pp_hook(d, progress, state):
    if d.get('status') == 'finished':
        info = d.get('info_dict') or {}
        fp = info.get('filepath') or state.get('final_path')
        if fp:
            state['final_path'] = fp


def download_video(url, target_dir, progress, *, format_str=None):
    """Blocking download. Mutates `progress` in place.

    On success sets progress['final_path'], progress['final_name'],
    progress['title']. On failure sets progress['error']. Always sets
    progress['running'] = False at the end.
    """
    state = {'final_path': None}
    try:
        yt_dlp = _require_ytdlp()
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        progress['status'] = 'starting'
        progress['phase'] = 'resolving'
        progress['running'] = True

        def _hook(d):
            _apply_progress_hook(d, progress, state)

        def _pp_hook(d):
            _apply_pp_hook(d, progress, state)

        ydl_opts = {
            'outtmpl': str(target / '%(title).180B [%(id)s].%(ext)s'),
            'restrictfilenames': True,
            'noplaylist': True,
            'quiet': True,
            'no_warnings': True,
            'format': format_str or 'bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b',
            'merge_output_format': 'mp4',
            'progress_hooks': [_hook],
            'postprocessor_hooks': [_pp_hook],
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            progress['title'] = (info or {}).get('title') or ''
            if not state['final_path']:
                try:
                    state['final_path'] = ydl.prepare_filename(info)
                except Exception:
                    pass

        final = state.get('final_path')
        if final:
            p = Path(final)
            progress['final_path'] = str(p)
            progress['final_name'] = p.name
        progress['phase'] = 'done'
        progress['status'] = 'finished'
    except Exception as e:
        logger.exception('download failed: %s', url)
        progress['error'] = str(e) or e.__class__.__name__
        progress['phase'] = 'error'
        progress['status'] = 'error'
    finally:
        progress['running'] = False
