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


def test_browse_parent_traversal_blocked(srv, media_root):
    # Request paths may not escape the served root: an existing sibling
    # directory must 404 rather than be listed.
    sibling = media_root.parent / 'outside'
    sibling.mkdir()
    (sibling / 'leak.mp4').write_bytes(b'\x00' * 64)
    status, _, body = request(srv, 'GET', '/browse?path=../outside')
    assert status == 404
    assert 'leak.mp4' not in body.decode()


def test_video_parent_traversal_blocked(srv, media_root):
    sibling = media_root.parent / 'outside2'
    sibling.mkdir()
    (sibling / 'leak.mp4').write_bytes(b'\x00' * 64)
    status, _, _ = request(srv, 'GET', '/video/../outside2/leak.mp4')
    assert status == 404
    status, _, _ = request(srv, 'GET', '/video/%2e%2e/outside2/leak.mp4')
    assert status == 404


def test_delete_parent_traversal_blocked(srv, media_root):
    victim = media_root.parent / 'victim.mp4'
    victim.write_bytes(b'\x00' * 64)
    status, _, _ = post_form(srv, '/delete', {'path': '../victim.mp4'})
    assert status == 400
    assert victim.exists()


def test_star_update_name_traversal_blocked(srv, media_root):
    victim = media_root.parent / 'victim2.mp4'
    victim.write_bytes(b'\x00' * 64)
    status, _, _ = post_form(
        srv, '/star-update', {'dir': '', 'name': '../victim2.mp4', 'starred': '1'},
    )
    assert status == 404
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert '../victim2.mp4' not in on_disk


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


def test_play_page_flags_suspect_tags(srv, media_root):
    # a.mp4 has confirmed tag 'cat'; the classifier flagged it as likely wrong.
    (media_root / '.simpleparty' / 'suspect_tags.json').write_text(json.dumps({
        'a.mp4': [{'tag': 'cat', 'given': 1, 'prob': 0.01}],
    }))
    status, _, body = request(srv, 'GET', '/play?path=&idx=0&sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert 'video-title">a.mp4' in text  # confirm we're on a.mp4
    assert 'video-tag-pill suspect' in text
    assert 'suspect-badge' in text


def test_suggest_button_hidden_without_model_or_vocab(srv):
    # 'sub' has an untagged c.mp4, no model, and no confirmed tags -> nothing can
    # produce a suggestion, so the 'Suggest tags' button must not be shown.
    status, _, body = request(srv, 'GET', '/play?path=sub&idx=0')
    text = body.decode()
    assert status == 200
    assert 'video-title">c.mp4' in text
    assert 'Suggest tags' not in text


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
    # Must carry Content-Length: 0 so keep-alive clients don't block
    # waiting for a body (regression test: this used to hang).
    status, headers, _ = request(
        srv, 'GET', '/video/a.mp4', headers={'Range': 'bytes=5000-'},
    )
    assert status == 416
    assert headers['Content-Range'] == 'bytes */1024'
    assert headers['Content-Length'] == '0'


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


# --- Lazy-loading pagination ---

@pytest.fixture
def big_srv(tmp_path):
    for i in range(250):
        (tmp_path / f'v{i:03d}.mp4').write_bytes(b'\x00' * 16)
    sp_server._config['root'] = str(tmp_path)
    server = ThreadedServer(
        ('127.0.0.1', 0), partial(RequestHandler, str(tmp_path)),
    )
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    server.shutdown()
    server.server_close()


def test_browse_renders_first_page_plus_sentinel(big_srv):
    status, _, body = request(big_srv, 'GET', '/browse?sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert text.count('class="item item-video"') == 100
    assert 'load-more' in text
    assert 'frag=list' in text and 'offset=100' in text
    # view params propagate through the sentinel URL
    assert 'sort=name' in text.split('load-more')[1][:200]


def test_browse_fragment_middle_page(big_srv):
    status, _, body = request(big_srv, 'GET', '/browse?sort=name&dir=asc&frag=list&offset=100')
    text = body.decode()
    assert status == 200
    assert text.count('class="item item-video"') == 100
    assert 'offset=200' in text
    assert '<nav' not in text and 'file-list' not in text  # bare fragment


def test_browse_fragment_last_page_has_no_sentinel(big_srv):
    status, _, body = request(big_srv, 'GET', '/browse?sort=name&dir=asc&frag=list&offset=200')
    text = body.decode()
    assert status == 200
    assert text.count('class="item item-video"') == 50
    assert 'load-more' not in text


def test_fragment_play_urls_use_absolute_indices(big_srv):
    _, _, body = request(big_srv, 'GET', '/browse?sort=name&dir=asc&frag=list&offset=100')
    text = body.decode()
    # First item of the second page is index 100 in the full sorted list
    assert 'idx=100' in text and 'v100.mp4' in text


def test_playlist_paginated_and_preserves_shuffle_seed(big_srv):
    status, _, body = request(big_srv, 'GET', '/play?path=&shuffle=1&seed=42&pos=0&sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert text.count('<a class="playlist-item') == 50
    sentinel = text.split('load-more')[1][:300]
    assert 'frag=playlist' in sentinel and 'offset=50' in sentinel
    assert 'seed=42' in sentinel


def test_playlist_fragment_continues_order(big_srv):
    status, _, body0 = request(big_srv, 'GET', '/play?path=&idx=0&sort=name&dir=asc')
    assert status == 200
    assert 'v049.mp4' in body0.decode()  # full page ends its first chunk at 49
    _, _, body1 = request(big_srv, 'GET', '/play?path=&idx=0&sort=name&dir=asc&frag=playlist&offset=50')
    text = body1.decode()
    assert text.count('<a class="playlist-item') == 50
    # Continues at playlist position 50 (video v050 when starting from v000)
    assert '>50</span>' in text and 'v050.mp4' in text


# --- Review fixes ---

def test_head_html_route_sends_no_body(srv):
    # do_HEAD falls through to do_GET for HTML routes; the body must be
    # suppressed or keep-alive clients desync on the next response.
    status, headers, body = request(srv, 'HEAD', '/')
    assert status == 200
    assert int(headers['Content-Length']) > 0
    assert body == b''


def test_delete_by_tag_honors_starred_filter(srv, media_root):
    # b.mp4 gets the same tag but is not starred; with starred=1 the bulk
    # delete must only remove the starred subset the user confirmed.
    tags_file = media_root / '.simpleparty' / 'tags.json'
    tags_file.write_text(json.dumps({
        'a.mp4': {'tags': ['cat'], 'status': 'confirmed', 'starred': True},
        'b.mp4': {'tags': ['cat'], 'status': 'confirmed'},
    }))
    status, _, _ = post_form(
        srv, '/delete-by-tag', {'path': '', 'tags': 'cat', 'starred': '1'},
    )
    assert status == 200
    assert not (media_root / 'a.mp4').exists()
    assert (media_root / 'b.mp4').exists()
