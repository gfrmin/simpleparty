"""Shared constants and write-once configuration.

CONFIG settings are written once in main() and read-only afterwards.
Mutable runtime job state lives in simpleparty.jobs.
"""

import threading

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

BROWSER_NATIVE = frozenset({'.mp4', '.webm', '.ogv', '.m4v'})

CONFIG = {
    'has_ffmpeg': False,
    'has_vlc': False,
    'has_ytdlp': False,
    'allow_delete': True,
    'allow_transcode': True,
    'allow_tag': True,
    'allow_download': False,
    'tag_jobs': {},  # path -> progress dict
    'thumb_jobs': set(),  # directories currently generating thumbs
    'download_queue': None,          # queue.Queue[str], lazy
    'download_jobs': {},             # job_id -> job dict (see _new_download_job)
    'download_order': [],            # job_ids in enqueue order, capped
    'download_lock': threading.Lock(),
    'download_worker': None,         # threading.Thread, lazy
    'yt_dlp_format': None,
}

DOWNLOAD_HISTORY_LIMIT = 20

MIME_TYPES = {
    '.mp4': 'video/mp4',
    '.webm': 'video/webm',
    '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.m4v': 'video/mp4',
    '.ogv': 'video/ogg',
}
