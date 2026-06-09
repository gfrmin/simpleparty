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

from simpleparty.library import (
    _compute_related_videos,
    durations_from_tags,
    filter_videos_by_starred,
    filter_videos_by_tags,
    find_encrypted_ancestor,
    find_locked_ancestor,
    find_video_idx,
    fscrypt_lock,
    fscrypt_unlock,
    get_fscrypt_status,
    is_video,
    list_directory,
    resolve_path,
    shuffle_indices,
    sort_videos,
)
from simpleparty import jobs
from simpleparty.routes import (
    GET_PREFIXES,
    GET_ROUTES,
    POST_ROUTES,
    dispatch,
    handle_browse,
    handle_confirm_all,
    handle_confirm_tags,
    handle_delete,
    handle_delete_by_tag,
    handle_download_cancel,
    handle_download_clear,
    handle_download_page,
    handle_download_status,
    handle_download_submit,
    handle_lock,
    handle_play,
    handle_reject_tag,
    handle_reject_tags,
    handle_save_tags,
    handle_star_update,
    handle_suggest,
    handle_suggest_one,
    handle_tag_status,
    handle_thumb,
    handle_train,
    handle_unlock,
    handle_video,
    read_form_body,
    send_hx_redirect,
    send_html,
    send_redirect,
)
from simpleparty.render import (
    _render_train_btn,
    fmt_size,
    render_browse_page,
    render_download_form,
    render_download_page,
    render_download_status,
    render_error_page,
    render_file_list,
    render_locked_page,
    render_page,
    render_play_page,
    render_video_tags_inline,
)
from simpleparty.media import (
    _is_mpegts,
    _maybe_start_thumbs,
    _probe_streams,
    _remux_mpegts,
    _serve_transcoded,
    _stream_file,
    _stream_range,
    _transcode_plan,
)
from simpleparty.urls import (
    parse_query,
    parse_sort_params,
    parse_starred_param,
    parse_tags_param,
    safe_int,
    url_for_browse,
    url_for_play,
    url_for_video,
)
from simpleparty.state import (
    CONFIG as _config,
    BROWSER_NATIVE,
    DOWNLOAD_HISTORY_LIMIT,
    MIME_TYPES,
    VIDEO_EXTENSIONS,
)

logger = logging.getLogger('simpleparty.server')


# --- Server ---

class RequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, root, *args, **kwargs):
        self.root = root
        super().__init__(*args, **kwargs)

    def do_GET(self):
        dispatch(self, self.root, GET_ROUTES, GET_PREFIXES)

    def do_POST(self):
        dispatch(self, self.root, POST_ROUTES)

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
