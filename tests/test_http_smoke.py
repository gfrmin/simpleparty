"""HTTP-level characterization tests.

These pin the observable behavior of the server (status codes, headers,
body content) so the module split and performance work can be verified
not to change it. They use only stdlib http.client against a real
ThreadedServer bound to an ephemeral port on a tmp directory.
"""

import http.client
import json
import re
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
    saved_tool_error = sp_library._tool_error
    # Keep tests hermetic and fast: no ffmpeg probing, no fscrypt subprocesses.
    # Encryption detection itself is pure ioctl and stays live — on a tmp dir
    # it just reports "not encrypted".
    sp_server._config['has_ffmpeg'] = False
    sp_server._config['has_vlc'] = False
    sp_server._config['allow_transcode'] = False
    sp_server._config['allow_tag'] = True
    sp_server._config['allow_delete'] = True
    sp_server._config['allow_download'] = False
    sp_library._tool_error = 'not installed'
    yield
    sp_server._config.clear()
    sp_server._config.update(saved)
    sp_library._tool_error = saved_tool_error


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
    # `fields` may be a dict or a list of (k, v) tuples (for repeated fields).
    body = urllib.parse.urlencode(fields)
    return request(
        server, 'POST', path, body=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )


def _embed_video(media_root, rel_dir, name):
    """Write a fresh cached embedding so the video counts as 'embedded'."""
    import numpy as np
    from simpleparty.embeddings import cached_embedding_path
    d = media_root if rel_dir == '' else media_root / rel_dir
    npy, _fail = cached_embedding_path(str(d), name)
    npy.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy, np.ones(4, dtype='float32'))


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
    # produce a suggestion, so the 'Suggest' button must not be shown.
    status, _, body = request(srv, 'GET', '/play?path=sub&idx=0')
    text = body.decode()
    assert status == 200
    assert 'video-title">c.mp4' in text
    assert 'Suggest' not in text


def test_suggest_button_labels_zero_shot_when_only_vocab(srv, media_root):
    # b.mp4 is untagged; the directory has a confirmed tag ('cat') but no model,
    # so the per-video button must advertise the zero-shot path. Suggest is now
    # gated on the video being embedded, so embed it first.
    _embed_video(media_root, '', 'b.mp4')
    status, _, body = request(srv, 'GET', '/play?path=&idx=1&sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert 'video-title">b.mp4' in text
    assert 'zero-shot' in text
    assert 'model)' not in text


def test_suggest_button_labels_model_when_model_present(srv, media_root):
    # A model checkpoint existing flips the button to the supervised label.
    (media_root / '.simpleparty' / 'model.pt').write_bytes(b'stub')
    _embed_video(media_root, '', 'b.mp4')  # Suggest requires an embedding
    status, _, body = request(srv, 'GET', '/play?path=&idx=1&sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert 'video-title">b.mp4' in text
    assert 'Suggest (model)' in text


def test_browse_has_no_mass_tagging_affordances(srv, media_root):
    # Per-video review only: the directory bar must not offer whole-directory
    # suggest or one-click confirm-all. The tag bar requires ffmpeg + a model,
    # so force both so the assertion isn't vacuous.
    sp_server._config['has_ffmpeg'] = True
    (media_root / '.simpleparty' / 'model.pt').write_bytes(b'stub')
    (media_root / '.simpleparty' / 'tags.json').write_text(json.dumps({
        'a.mp4': {'tags': ['cat'], 'status': 'confirmed'},
        'b.mp4': {'tags': ['dog'], 'status': 'suggested'},
    }))
    status, _, body = request(srv, 'GET', '/browse?path=')
    text = body.decode()
    assert status == 200
    assert 'hx-post="/suggest"' not in text
    assert '/confirm-all' not in text
    assert 'Confirm all' not in text


def test_dir_suggest_and_confirm_all_routes_removed(srv):
    s1, _, _ = post_form(srv, '/suggest', {'path': ''})
    s2, _, _ = post_form(srv, '/confirm-all', {'path': ''})
    assert s1 == 404
    assert s2 == 404


# --- Embed vs Train: coverage-gated actions ---

def test_browse_shows_embed_and_badge_when_videos_missing(srv):
    # No embeddings yet: the tag bar must offer Embed (all missing) + a coverage
    # badge, and must NOT offer Train (nothing embedded to train on).
    sp_server._config['has_ffmpeg'] = True
    status, _, body = request(srv, 'GET', '/browse?path=')
    text = body.decode()
    assert status == 200
    assert 'embedded' in text                 # coverage badge
    assert 'Embed all missing (2)' in text     # a.mp4 + b.mp4
    assert 'btn-train' not in text


def test_browse_shows_train_when_all_embedded(srv, media_root):
    sp_server._config['has_ffmpeg'] = True
    _embed_video(media_root, '', 'a.mp4')
    _embed_video(media_root, '', 'b.mp4')
    status, _, body = request(srv, 'GET', '/browse?path=')
    text = body.decode()
    assert status == 200
    assert 'btn-train' in text
    assert 'Embed all missing' not in text     # nothing missing


def test_browse_missing_videos_get_embed_checkbox(srv):
    sp_server._config['has_ffmpeg'] = True
    status, _, body = request(srv, 'GET', '/browse?path=')
    text = body.decode()
    assert 'class="embed-check"' in text
    assert 'name="video" value="a.mp4"' in text


def test_embed_all_missing_dispatches_all(srv, media_root, monkeypatch):
    import simpleparty.embeddings as emb
    seen = {}
    done = threading.Event()

    def rec(directory, names, max_frames=8, progress=None):
        seen['names'] = list(names)
        if progress is not None:
            progress['running'] = False
        done.set()

    monkeypatch.setattr(emb, 'embed_videos', rec)
    status, headers, _ = post_form(srv, '/embed', {'path': ''})
    assert status == 200
    assert done.wait(5)
    assert sorted(seen['names']) == ['a.mp4', 'b.mp4']


def test_embed_selected_subset(srv, media_root, monkeypatch):
    import simpleparty.embeddings as emb
    seen = {}
    done = threading.Event()

    def rec(directory, names, max_frames=8, progress=None):
        seen['names'] = list(names)
        if progress is not None:
            progress['running'] = False
        done.set()

    monkeypatch.setattr(emb, 'embed_videos', rec)
    post_form(srv, '/embed', [('path', ''), ('video', 'a.mp4')])
    assert done.wait(5)
    assert seen['names'] == ['a.mp4']


def test_embed_ignores_already_embedded(srv, media_root, monkeypatch):
    import simpleparty.embeddings as emb
    _embed_video(media_root, '', 'a.mp4')  # already done
    seen = {}
    done = threading.Event()

    def rec(directory, names, max_frames=8, progress=None):
        seen['names'] = list(names)
        if progress is not None:
            progress['running'] = False
        done.set()

    monkeypatch.setattr(emb, 'embed_videos', rec)
    post_form(srv, '/embed', [('path', ''), ('video', 'a.mp4'), ('video', 'b.mp4')])
    assert done.wait(5)
    assert seen['names'] == ['b.mp4']  # a.mp4 already embedded -> filtered out


def test_suggest_one_409_on_unembedded_video(srv):
    # Suggest must never embed: an un-embedded video is refused, not computed.
    status, _, _ = post_form(srv, '/suggest-one', {'path': '', 'video': 'b.mp4'})
    assert status == 409


def test_play_page_offers_embed_when_unembedded(srv):
    sp_server._config['has_ffmpeg'] = True
    status, _, body = request(srv, 'GET', '/play?path=&idx=1&sort=name&dir=asc')
    text = body.decode()
    assert status == 200
    assert 'video-title">b.mp4' in text
    assert 'Embed this video' in text
    assert 'Suggest' not in text


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


# --- Directory tag management (rename / remove) ---

def _write_tags(media_root, mapping):
    (media_root / '.simpleparty' / 'tags.json').write_text(json.dumps(mapping))


def test_browse_surfaces_manage_tags_panel(srv, media_root):
    # media_root fixture has a.mp4 tagged 'cat' -> panel + a rename/remove row
    status, _, body = request(srv, 'GET', '/browse')
    text = body.decode()
    assert status == 200
    assert 'Manage tags' in text
    assert 'id="tag-manager"' in text
    assert '/rename-tag' in text and '/remove-tag' in text


def test_browse_no_manage_panel_when_no_tags(srv, media_root):
    _write_tags(media_root, {})
    status, _, body = request(srv, 'GET', '/browse')
    assert status == 200
    assert 'Manage tags' not in body.decode()


def test_rename_tag_persists(srv, media_root):
    _write_tags(media_root, {
        'a.mp4': {'tags': ['scifi'], 'status': 'confirmed'},
        'b.mp4': {'tags': ['Scifi', 'drama'], 'status': 'confirmed'},
    })
    status, _, _ = post_form(
        srv, '/rename-tag', {'path': '', 'old': 'scifi', 'new': 'science fiction'},
    )
    assert status == 200
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert on_disk['a.mp4']['tags'] == ['science fiction']
    assert on_disk['b.mp4']['tags'] == ['science fiction', 'drama']


def test_rename_tag_merges_and_dedups(srv, media_root):
    _write_tags(media_root, {
        'a.mp4': {'tags': ['action', 'fight'], 'status': 'confirmed'},
    })
    status, _, _ = post_form(
        srv, '/rename-tag', {'path': '', 'old': 'fight', 'new': 'action'},
    )
    assert status == 200
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert on_disk['a.mp4']['tags'] == ['action']


def test_remove_tag_persists_keeps_video(srv, media_root):
    _write_tags(media_root, {
        'a.mp4': {'tags': ['generic', 'action'], 'status': 'confirmed'},
    })
    status, _, _ = post_form(srv, '/remove-tag', {'path': '', 'tag': 'generic'})
    assert status == 200
    assert (media_root / 'a.mp4').exists()  # video kept
    on_disk = json.loads((media_root / '.simpleparty' / 'tags.json').read_text())
    assert on_disk['a.mp4']['tags'] == ['action']


def test_rename_tag_empty_new_is_400(srv, media_root):
    status, _, _ = post_form(
        srv, '/rename-tag', {'path': '', 'old': 'cat', 'new': '   '},
    )
    assert status == 400


def test_rename_tag_disabled_403(srv, media_root):
    sp_server._config['allow_tag'] = False
    status, _, _ = post_form(
        srv, '/rename-tag', {'path': '', 'old': 'cat', 'new': 'feline'},
    )
    assert status == 403


def test_remove_tag_disabled_403(srv, media_root):
    sp_server._config['allow_tag'] = False
    status, _, _ = post_form(srv, '/remove-tag', {'path': '', 'tag': 'cat'})
    assert status == 403


def test_rename_tag_traversal_blocked(srv, media_root):
    status, _, _ = post_form(
        srv, '/rename-tag', {'path': '../etc', 'old': 'cat', 'new': 'feline'},
    )
    assert status == 400


def test_remove_tag_traversal_blocked(srv, media_root):
    status, _, _ = post_form(
        srv, '/remove-tag', {'path': '../etc', 'tag': 'cat'},
    )
    assert status == 400


# --- Static assets ---

def test_static_css(srv):
    status, headers, body = request(srv, 'GET', '/static/style.css')
    text = body.decode()
    assert status == 200
    assert headers['Content-Type'].startswith('text/css')
    assert headers['Cache-Control'] == 'public, max-age=3600'
    assert '#file-list' in text
    assert ':root' in text and '--accent:#6366f1' in text.replace(' ', '')
    assert '--bg:#0a0c10' in text.replace(' ', '')


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


# --- Design-system guard tests ---

def test_pages_have_no_emoji(srv):
    sp_server._config['has_ffmpeg'] = True
    banned = ['⬇','\U0001F5D1','\U0001F9E0','\U0001F3F7','\U0001F52E','⚙',
              '⇅','\U0001F4C1','\U0001F512','\U0001F513','\U0001F3AC','✅',
              '❌','✔','✘','❓','⏳','★','☆','▶']
    # /browse?tags=cat exercises the active-filter "Delete all" button + manage
    # panel; the play URL exercises the playlist "Now" marker and star pill —
    # sub-states the bare / and /browse pages don't reach.
    for url in ['/', '/browse?path=sub', '/browse?tags=cat',
                '/play?path=&idx=0&sort=name&dir=asc']:
        status, _, body = request(srv, 'GET', url)
        assert status == 200, f'{url} returned {status}'
        text = body.decode()
        for ch in banned:
            assert ch not in text, f'emoji {ch!r} still in {url}'


def test_css_uses_tokens_not_raw_hex(srv):
    text = request(srv, 'GET', '/static/style.css')[2].decode()
    body = text.split('}', 1)[1] if ':root' in text else text  # drop the :root block
    # allow #fff/#000 shorthands; flag 6-digit hex in component rules
    leaks = re.findall(r'#[0-9a-fA-F]{6}', body)
    assert not leaks, f'raw hex outside tokens: {set(leaks)}'


def test_tag_manager_remove_button_visible(srv):
    # The base .btn-del is a grid-thumbnail overlay (position:absolute,
    # opacity:0, revealed by .item-video:hover). The tag-manager Remove
    # button reuses .btn-del but lives in a .tag-manager-row, so it needs
    # an override resetting it to in-flow and visible — otherwise it's
    # invisible (regression seen in the redesign).
    text = request(srv, 'GET', '/static/style.css')[2].decode()
    m = re.search(r'\.tag-manager-remove \.btn-del\{([^}]*)\}', text)
    assert m, 'missing .tag-manager-remove .btn-del override'
    rule = m.group(1).replace(' ', '')
    assert 'position:static' in rule
    assert 'opacity:1' in rule
