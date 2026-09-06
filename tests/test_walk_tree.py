"""Tests for library.walk_video_tree and media._scan_tree_for_reencode.

The walker is the only recursive traversal in the codebase, so the things a
bare os.walk gets wrong (dot-dirs, locked fscrypt subtrees, symlinks) are
pinned here explicitly.
"""

import os

import pytest

from simpleparty import jobs as sp_jobs
from simpleparty import library as sp_library
from simpleparty import media as sp_media


def _tree(root):
    (root / 'a.mp4').write_bytes(b'x')
    (root / 'notes.txt').write_text('not a video')
    sub = root / 'sub'
    sub.mkdir()
    (sub / 'b.mp4').write_bytes(b'x')
    deep = sub / 'deep'
    deep.mkdir()
    (deep / 'c.mp4').write_bytes(b'x')
    return root


def _walk(root, rel='', **kw):
    stats = {}
    out = {rel_path: sorted(v[0] for v in vids)
           for rel_path, _resolved, vids in
           sp_library.walk_video_tree(str(root), rel, stats=stats, **kw)}
    return out, stats


def test_walks_nested_directories(tmp_path):
    out, stats = _walk(_tree(tmp_path))
    assert out == {'': ['a.mp4'], 'sub': ['b.mp4'], 'sub/deep': ['c.mp4']}
    assert stats['dirs_visited'] == 3


def test_skips_dot_directories(tmp_path):
    _tree(tmp_path)
    transcoded = tmp_path / '.simpleparty' / 'transcoded'
    transcoded.mkdir(parents=True)
    (transcoded / 'a.mp4.mp4').write_bytes(b'cached')
    out, _ = _walk(tmp_path)
    # Its own cache output must never be re-fed into the scanner.
    assert not any('.simpleparty' in rel for rel in out)
    assert 'a.mp4.mp4' not in [n for names in out.values() for n in names]


def test_skips_locked_encrypted_subtree(tmp_path, monkeypatch):
    _tree(tmp_path)
    locked = tmp_path / 'sub'

    def fake_status(p):
        if str(p) == str(locked):
            return {'encrypted': True, 'unlocked': False}
        return {'encrypted': False, 'unlocked': True}

    monkeypatch.setattr(sp_library, 'get_fscrypt_status', fake_status)
    out, stats = _walk(tmp_path)
    # A locked dir is listable but its names are ciphertext — never descend.
    assert 'sub' not in out and 'sub/deep' not in out
    assert out == {'': ['a.mp4']}
    assert stats['dirs_locked_skipped'] == 1


def _symlink(link, target):
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable')


def test_descends_into_symlinked_dirs(tmp_path):
    """The rest of the app resolves symlinks and happily serves what it finds
    (is_safe_rel_path is purely lexical), so a walk that refused to follow
    them made the recursive scan silently blind to whole libraries — e.g. a
    media root reached through a ~/yo -> /mnt/yo symlink."""
    root = tmp_path / 'root'
    root.mkdir()
    _tree(root)
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'off.mp4').write_bytes(b'x')
    _symlink(root / 'linked', outside)

    out, stats = _walk(root)

    assert out['linked'] == ['off.mp4']
    assert stats['dirs_visited'] == 4


def test_symlinked_root_path_is_walked(tmp_path):
    """Serving a root that itself contains a symlink to another filesystem:
    scanning that subtree must not come back empty."""
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'off.mp4').write_bytes(b'x')
    _symlink(root / 'linked', outside)

    out, stats = _walk(root, rel='linked')

    assert out == {'linked': ['off.mp4']}
    assert stats['dirs_visited'] == 1


def test_symlink_cycle_terminates(tmp_path):
    """Following symlinks means loops are now possible; each real directory
    must be visited at most once, identified by (st_dev, st_ino) rather than
    by path."""
    root = tmp_path / 'root'
    root.mkdir()
    _tree(root)
    _symlink(root / 'sub' / 'back', root)

    out, stats = _walk(root)

    assert stats['truncated'] is False
    assert sorted(out) == ['', 'sub', 'sub/deep']


def test_two_symlinks_to_the_same_dir_yield_it_once(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    outside = tmp_path / 'elsewhere'
    outside.mkdir()
    (outside / 'off.mp4').write_bytes(b'x')
    _symlink(root / 'one', outside)
    _symlink(root / 'two', outside)

    out, _ = _walk(root)

    assert [k for k in out if k in ('one', 'two')] == ['one']


def test_refuses_to_escape_root(tmp_path):
    _tree(tmp_path)
    out, _ = _walk(tmp_path, rel='../..')
    assert out == {}


def test_max_dirs_truncates_and_reports(tmp_path):
    _tree(tmp_path)
    out, stats = _walk(tmp_path, max_dirs=2)
    assert len(out) == 2
    assert stats['truncated'] is True


def test_unreadable_directory_is_counted_not_fatal(tmp_path):
    _tree(tmp_path)
    if os.geteuid() == 0:
        pytest.skip('root ignores directory permissions')
    bad = tmp_path / 'sub' / 'deep'
    os.chmod(bad, 0o000)
    try:
        out, stats = _walk(tmp_path)
        assert 'sub' in out  # siblings still visited
        assert stats['dirs_unreadable_skipped'] == 1
    finally:
        os.chmod(bad, 0o755)


# --- _scan_tree_for_reencode ---

@pytest.fixture(autouse=True)
def _scan_state():
    with sp_jobs.job_sets_lock:
        saved = dict(sp_jobs.reencode_scanned_at)
        sp_jobs.reencode_scanned_at.clear()
    yield
    with sp_jobs.job_sets_lock:
        sp_jobs.reencode_scanned_at.clear()
        sp_jobs.reencode_scanned_at.update(saved)


def test_scan_tree_collects_reencode_candidates(tmp_path, monkeypatch):
    _tree(tmp_path)
    monkeypatch.setattr(sp_media, '_cached_transcode', lambda p: None)
    monkeypatch.setattr(
        sp_media, '_transcode_plan',
        lambda p: 'reencode' if p.name in ('a.mp4', 'c.mp4') else None)

    job = sp_jobs.new_tree_scan_job()
    sp_media._scan_tree_for_reencode(str(tmp_path), '', job)

    assert job['running'] is False
    assert job['error'] is None
    assert sorted(os.path.basename(p) for p in job['found']) == ['a.mp4', 'c.mp4']
    assert job['scanned_videos'] == 3
    assert job['scanned_dirs'] == 3


def test_scan_tree_records_error_rather_than_crashing(tmp_path, monkeypatch):
    _tree(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError('walker exploded')

    monkeypatch.setattr(sp_media, 'walk_video_tree', boom, raising=False)
    monkeypatch.setattr(sp_library, 'walk_video_tree', boom)
    job = sp_jobs.new_tree_scan_job()
    sp_media._scan_tree_for_reencode(str(tmp_path), '', job)
    assert job['running'] is False
    assert 'walker exploded' in job['error']


def test_truncated_directory_is_not_marked_fully_scanned(tmp_path, monkeypatch):
    """A partially-inspected directory must not land in reencode_scanned_at,
    or the automatic scanner would never revisit its unchecked tail."""
    root = tmp_path
    (root / 'v1.mp4').write_bytes(b'x')
    (root / 'v2.mp4').write_bytes(b'x')
    monkeypatch.setattr(sp_media, 'MAX_TREE_SCAN_VIDEOS', 1)
    monkeypatch.setattr(sp_media, '_cached_transcode', lambda p: None)
    monkeypatch.setattr(sp_media, '_transcode_plan', lambda p: 'reencode')

    job = sp_jobs.new_tree_scan_job()
    sp_media._scan_tree_for_reencode(str(root), '', job)

    assert job['truncated'] is True
    with sp_jobs.job_sets_lock:
        assert str(root.resolve()) not in sp_jobs.reencode_scanned_at


def test_mark_dir_scanned_is_bounded(monkeypatch):
    """A recursive scan can visit thousands of directories in one request, so
    this map must not grow without limit the way it originally did."""
    monkeypatch.setattr(sp_jobs, '_SCANNED_AT_MAX', 3)
    for i in range(10):
        sp_jobs.mark_dir_scanned(f'/d{i}', i)
    with sp_jobs.job_sets_lock:
        keys = list(sp_jobs.reencode_scanned_at)
    assert len(keys) == 3
    assert keys == ['/d7', '/d8', '/d9']  # oldest evicted first


def test_mark_dir_scanned_refreshes_recency(monkeypatch):
    monkeypatch.setattr(sp_jobs, '_SCANNED_AT_MAX', 2)
    sp_jobs.mark_dir_scanned('/a', 1)
    sp_jobs.mark_dir_scanned('/b', 2)
    sp_jobs.mark_dir_scanned('/a', 3)  # re-touch: /a is now newest
    sp_jobs.mark_dir_scanned('/c', 4)
    with sp_jobs.job_sets_lock:
        keys = list(sp_jobs.reencode_scanned_at)
    assert keys == ['/a', '/c']  # /b evicted, not /a
