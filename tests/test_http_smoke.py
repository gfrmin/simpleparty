"""HTTP-level characterization tests.

These pin the observable behavior of the server (status codes, headers,
body content) so the module split and performance work can be verified
not to change it. They use only stdlib http.client against a real
ThreadedServer bound to an ephemeral port on a tmp directory.
"""

import http.client
import json
import threading
import urllib.parse
from functools import partial
from pathlib import Path

import pytest

from simpleparty import library as sp_library
from simpleparty import server as sp_server
from simpleparty.server import RequestHandler, ThreadedServer


@pytest.fixture(autouse=True)
def _config_snapshot():
    """Snapshot/restore mutable server config so tests can flip flags."""
    saved = dict(sp_server._config)
    saved_fscrypt_missing = sp_library._fscrypt_missing
    # Keep tests hermetic and fast: no ffmpeg probing, no fscrypt subprocesses.
    sp_server._config['has_ffmpeg'] = False
    sp_server._config['has_vlc'] = False
    sp_server._config['allow_transcode'] = False
    sp_server._config['allow_tag'] = True
    sp_server._config['allow_delete'] = True
    sp_server._config['allow_download'] = False
    sp_library._fscrypt_missing = True
    yield
    sp_server._config.clear()
    sp_server._config.update(saved)
    sp_library._fscrypt_missing = saved_fscrypt_missing


@pytest.fixture
def media_root(tmp_path):
    (tmp_path / 'a.mp4').write_bytes(b'\x00' * 1024)
    (tmp_path / 'b.mp4').write_bytes(b'\x00' * 2048)
    sub = tmp_path / 'sub'
    sub.mkdir()
    (sub / 'c.mp4').write_bytes(b'\x00' * 512)
    sp = tmp_path / '.simpleparty'
    sp.mkdir()
    (sp / 'tags.json').write_text(json.dumps({
        'a.mp4': {'tags': ['cat'], 'status': 'confirmed', 'starred': True},
    }))
    return tmp_path


@pytest.fixture
def srv(media_root):
    sp_server._config['root'] = str(media_root)
    server = ThreadedServer(
        ('127.0.0.1', 0), partial(RequestHandler, str(media_root)),
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


def request(server, method, path, body=None, headers=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, dict(resp.getheaders()), data
    finally:
        conn.close()


def post_form(server, path, fields):
    body = urllib.parse.urlencode(fields)
    return request(
        server, 'POST', path, body=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )


# --- Browse ---

def test_root_lists_everything(srv):
    status, _, body = request(srv, 'GET', '/')
    text = body.decode()
    assert status == 200
    assert 'a.mp4' in text and 'b.mp4' in text and 'sub' in text


def test_browse_subdir(srv):
    status, _, body = request(srv, 'GET', '/browse?path=sub')
    assert status == 200
    assert 'c.mp4' in body.decode()


def test_browse_missing_path_404(srv):
    status, _, body = request(srv, 'GET', '/browse?path=nope')
    assert status == 404
    assert 'Not found' in body.decode()


def test_browse_tag_filter(srv):
    status, _, body = request(srv, 'GET', '/browse?tags=cat')
    text = body.decode()
    assert status == 200
    assert 'a.mp4' in text
    assert 'b.mp4' not in text


def test_browse_starred_filter(srv):
    status, _, body = request(srv, 'GET', '/browse?starred=1')
    text = body.decode()
    assert status == 200
    assert 'a.mp4' in text
    assert 'b.mp4' not in text


def test_browse_parent_traversal(srv, media_root):
    # CHARACTERIZATION, NOT ENDORSEMENT: resolve_path() does not constrain
    # paths to the served root, so ../<sibling> resolves outside it. Today a
    # nonexistent sibling yields 404; an existing one would be served. If
    # root containment is ever enforced, update this test.
    sibling = media_root.parent / 'outside'
    sibling.mkdir()
    (sibling / 'leak.mp4').write_bytes(b'\x00' * 64)
    status, _, body = request(srv, 'GET', '/browse?path=../outside')
    assert status == 200
    assert 'leak.mp4' in body.decode()


# --- Play ---

def test_play_page(srv):
    status, _, body = request(srv, 'GET', '/play?path=&idx=0')
    text = body.decode()
    assert status == 200
    assert '<video' in text
    assert 'Playlist' in text


def test_play_empty_dir_redirects_to_browse(srv, media_root):
    (media_root / 'empty').mkdir()
    status, headers, _ = request(srv, 'GET', '/play?path=empty&idx=0')
    assert status == 302
    assert headers['Location'] == '/browse?' + urllib.parse.urlencode({'path': 'empty'})


# --- Video serving ---

def test_video_full(srv):
    status, headers, body = request(srv, 'GET', '/video/a.mp4')
    assert status == 200
    assert headers['Content-Length'] == '1024'
    assert headers['Content-Type'] == 'video/mp4'
    assert headers['Accept-Ranges'] == 'bytes'
    assert len(body) == 1024


def test_video_range(srv):
    status, headers, body = request(
        srv, 'GET', '/video/a.mp4', headers={'Range': 'bytes=100-199'},
    )
    assert status == 206
    assert headers['Content-Range'] == 'bytes 100-199/1024'
    assert len(body) == 100


def test_video_range_out_of_bounds(srv):
    # The 416 response carries no Content-Length, so a keep-alive client
    # would block waiting for a body; ask the server to close instead.
    status, headers, _ = request(
        srv, 'GET', '/video/a.mp4',
        headers={'Range': 'bytes=5000-', 'Connection': 'close'},
    )
    assert status == 416
    assert headers['Content-Range'] == 'bytes */1024'


def test_video_head(srv):
    status, headers, body = request(srv, 'HEAD', '/video/a.mp4')
    assert status == 200
    assert headers['Content-Length'] == '1024'
    assert body == b''


def test_video_missing_404(srv):
    status, _, _ = request(srv, 'GET', '/video/nope.mp4')
    assert status == 404


# --- Unknown routes ---

def test_unknown_get_404(srv):
    status, _, _ = request(srv, 'GET', '/nope')
    assert status == 404


def test_unknown_post_404(srv):
    status, _, _ = post_form(srv, '/nope', {})
    assert status == 404


# --- Delete ---

def test_delete_removes_file_and_redirects(srv, media_root):
    status, headers, _ = post_form(
        srv, '/delete', {'path': 'b.mp4', 'redirect': '/'},
    )
    assert status == 200
    assert headers['HX-Redirect'] == '/'
    assert not (media_root / 'b.mp4').exists()


def test_delete_disabled_403(srv, media_root):
    sp_server._config['allow_delete'] = False
    status, _, _ = post_form(srv, '/delete', {'path': 'b.mp4'})
    assert status == 403
    assert (media_root / 'b.mp4').exists()


def test_delete_invalid_path_400(srv):
    status, _, _ = post_form(srv, '/delete', {'path': 'nope.mp4'})
    assert status == 400


# --- Tags ---

def test_star_update_persists(srv, media_root):
    status, _, _ = post_form(
        srv, '/star-update', {'dir': '', 'name': 'b.mp4', 'starred': '1'},
    )
    assert status == 204
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert on_disk['b.mp4']['starred'] is True


def test_save_tags_returns_pills_and_persists(srv, media_root):
    status, _, body = post_form(
        srv, '/save-tags', {'path': '', 'video': 'b.mp4', 'tags': 'dog, fast'},
    )
    assert status == 200
    assert 'dog' in body.decode()
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert on_disk['b.mp4']['tags'] == ['dog', 'fast']


def test_tag_status(srv):
    status, _, body = request(srv, 'GET', '/tag-status?path=')
    assert status == 200
    assert 'tag-progress' in body.decode()


# --- Static assets ---

def test_static_css(srv):
    status, headers, body = request(srv, 'GET', '/static/style.css')
    assert status == 200
    assert headers['Content-Type'].startswith('text/css')
    assert headers['Cache-Control'] == 'public, max-age=3600'
    assert b'#file-list' in body


def test_static_unknown_404(srv):
    status, _, _ = request(srv, 'GET', '/static/nope.css')
    assert status == 404


def test_browse_sort_by_length_returns_immediately(srv):
    # Durations come from the tags cache; missing ones sort as 0 and are
    # probed in the background (no ffmpeg here, so no probe is spawned).
    status, _, body = request(srv, 'GET', '/browse?sort=length')
    assert status == 200
    assert 'a.mp4' in body.decode()
