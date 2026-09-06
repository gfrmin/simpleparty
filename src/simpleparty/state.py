"""Shared constants and write-once configuration.

CONFIG settings are written once in main() and read-only afterwards.
Mutable runtime job state lives in simpleparty.jobs.
"""

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
    'allow_pretranscode': True,
    'allow_tag': True,
    'has_tagger': False,
    'allow_download': False,
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
