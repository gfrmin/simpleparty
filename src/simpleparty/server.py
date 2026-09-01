#!/usr/bin/env python3
"""SimpleParty - Easily enjoy your private video collection."""

import argparse
import logging
import shutil
import urllib.parse
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

from simpleparty import library
from simpleparty.routes import (
    GET_PREFIXES,
    GET_ROUTES,
    POST_ROUTES,
    dispatch,
    handle_video,
)
from simpleparty.state import CONFIG as _config

logger = logging.getLogger('simpleparty.server')


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

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s %(name)s %(message)s',
            datefmt='%H:%M:%S',
        )
    else:
        # Other modules stay at WARNING (matching the old default); only
        # this module's banner/lifecycle messages come through, unformatted.
        logging.basicConfig(level=logging.WARNING, format='%(message)s')
        logger.setLevel(logging.INFO)

    root = str(Path(args.root).resolve())
    if not Path(root).is_dir():
        logger.error('Error: %s is not a directory', root)
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
    fscrypt_error = library.fscrypt_tool_error()
    features.append(
        'fscrypt: on' if fscrypt_error is None
        else f'fscrypt: unavailable ({fscrypt_error})'
    )
    if _config['allow_tag']:
        from simpleparty.embeddings import is_available as _tagger_available
        _config['has_tagger'] = _tagger_available()
        features.append('tag: on' if _config['has_tagger'] else 'tag: on (tagger unavailable)')
    if _config['allow_download']:
        features.append('download: on')
    elif not args.no_download and not _config['has_ytdlp']:
        features.append('download: on (yt-dlp unavailable)')

    from simpleparty import __version__
    url = f'http://{args.bind}:{args.port}'
    logger.info('SimpleParty %s serving %s', __version__, root)
    logger.info('  %s', url)
    if features:
        logger.info('  [%s]', ', '.join(features))
    if _config['allow_tag'] and not _config['has_tagger']:
        logger.info('  To train a tagger: uvx simpleparty[classifier]==%s', __version__)
    if (not args.no_download) and (not _config['has_ytdlp']):
        logger.info('  To enable downloads: uvx simpleparty[download]==%s', __version__)
    if fscrypt_error is not None and library.has_encrypted_dir(root):
        prose, command = library.fscrypt_remedy(fscrypt_error)
        logger.info('  Encrypted directories found here, but fscrypt is %s so they', fscrypt_error)
        logger.info('  cannot be unlocked. %s `%s`', prose, command)
        logger.info('  https://github.com/google/fscrypt')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('\nShutting down.')
        server.shutdown()


if __name__ == '__main__':
    main()
