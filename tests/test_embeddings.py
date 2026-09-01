"""Tests for the embedding cache path/invalidation logic.

These cover the pure-functional parts that need neither a GPU nor CLIP
weights: cache filenames are stat-keyed, lookups invalidate when a video
changes, the .fail sentinel short-circuits, and pruning drops stale files.
"""

import os
from pathlib import Path

import simpleparty.embeddings as embeddings_mod
from simpleparty.embeddings import (
    CLIP_MODEL_ID,
    embeddings_dir,
    embed_cache_paths,
    cached_embedding_path,
    fail_marker_path,
    prune_stale_embeddings,
    embedding_coverage,
    video_is_embedded,
    get_video_embedding,
    is_available,
)
from simpleparty.tagger import SIMPLEPARTY_DIR


def _make_video(path, content=b'data'):
    path.write_bytes(content)
    return path


def _write_fresh_npy(directory, name):
    """Write a valid cached embedding for `name` at its current stat key."""
    import numpy as np
    npy, _fail = cached_embedding_path(directory, name)
    npy.parent.mkdir(parents=True, exist_ok=True)
    np.save(npy, np.ones(4, dtype='float32'))
    return npy


def _write_fresh_fail(directory, name):
    _npy, fail = cached_embedding_path(directory, name)
    fail.parent.mkdir(parents=True, exist_ok=True)
    fail.write_bytes(b'')
    return fail


def test_embeddings_dir_namespaced_by_model(tmp_path):
    d = embeddings_dir(str(tmp_path))
    assert d == tmp_path / SIMPLEPARTY_DIR / 'embeddings' / CLIP_MODEL_ID


def test_cache_path_is_stat_keyed(tmp_path):
    video = _make_video(tmp_path / 'clip.mp4')
    st = os.stat(video)
    npy, fail = embed_cache_paths(str(tmp_path), 'clip.mp4', (st.st_mtime_ns, st.st_size))
    expected = embeddings_dir(str(tmp_path)) / f'clip.mp4__{st.st_mtime_ns}_{st.st_size}.npy'
    assert npy == expected
    assert fail == expected.with_suffix('.fail')


def test_cache_path_changes_when_video_changes(tmp_path):
    video = _make_video(tmp_path / 'clip.mp4', b'one')
    first = cached_embedding_path(str(tmp_path), 'clip.mp4')[0]
    os.utime(video, ns=(0, 0))
    video.write_bytes(b'a different size entirely')
    second = cached_embedding_path(str(tmp_path), 'clip.mp4')[0]
    assert first != second


def test_cached_embedding_path_none_for_missing_video(tmp_path):
    assert cached_embedding_path(str(tmp_path), 'gone.mp4') is None


def test_fail_marker_path_matches_cache_key(tmp_path):
    _make_video(tmp_path / 'clip.mp4')
    npy, fail = cached_embedding_path(str(tmp_path), 'clip.mp4')
    assert fail_marker_path(str(tmp_path), 'clip.mp4') == fail
    assert fail.suffix == '.fail'


def test_prune_removes_orphan_and_stale_files(tmp_path):
    video = _make_video(tmp_path / 'keep.mp4')
    st = os.stat(video)
    edir = embeddings_dir(str(tmp_path))
    edir.mkdir(parents=True)

    fresh = edir / f'keep.mp4__{st.st_mtime_ns}_{st.st_size}.npy'
    stale = edir / f'keep.mp4__123_456.npy'           # wrong stat for existing video
    orphan = edir / 'deleted.mp4__1_2.npy'            # video no longer exists
    for f in (fresh, stale, orphan):
        f.write_bytes(b'x')

    prune_stale_embeddings(str(tmp_path))

    assert fresh.exists()
    assert not stale.exists()
    assert not orphan.exists()


def test_prune_keeps_fresh_fail_marker(tmp_path):
    video = _make_video(tmp_path / 'dark.mp4')
    st = os.stat(video)
    edir = embeddings_dir(str(tmp_path))
    edir.mkdir(parents=True)
    fresh_fail = edir / f'dark.mp4__{st.st_mtime_ns}_{st.st_size}.fail'
    fresh_fail.write_bytes(b'')

    prune_stale_embeddings(str(tmp_path))
    assert fresh_fail.exists()


def test_prune_noop_when_no_cache_dir(tmp_path):
    # Should not raise when the embeddings dir was never created.
    prune_stale_embeddings(str(tmp_path))


def test_coverage_classifies_embedded_missing_failed(tmp_path):
    _make_video(tmp_path / 'embedded.mp4')
    _make_video(tmp_path / 'missing.mp4')
    _make_video(tmp_path / 'broken.mp4')
    _write_fresh_npy(str(tmp_path), 'embedded.mp4')
    _write_fresh_fail(str(tmp_path), 'broken.mp4')

    cov = embedding_coverage(str(tmp_path))
    assert cov['total'] == 3
    assert cov['embedded'] == 1
    assert cov['failed'] == 1
    assert cov['missing'] == 1
    # A permanently-failed video is NOT something Embed should retry.
    assert cov['missing_names'] == ['missing.mp4']


def test_coverage_counts_stale_embedding_as_missing(tmp_path):
    video = _make_video(tmp_path / 'clip.mp4', b'one')
    edir = embeddings_dir(str(tmp_path))
    edir.mkdir(parents=True)
    # An embedding from a previous encode (wrong stat key) is stale.
    (edir / 'clip.mp4__123_456.npy').write_bytes(b'x')

    cov = embedding_coverage(str(tmp_path))
    assert cov['embedded'] == 0
    assert cov['missing'] == 1
    assert cov['missing_names'] == ['clip.mp4']


def test_coverage_empty_dir(tmp_path):
    cov = embedding_coverage(str(tmp_path))
    assert cov == {'total': 0, 'embedded': 0, 'failed': 0,
                   'missing': 0, 'missing_names': []}


def test_video_is_embedded(tmp_path):
    _make_video(tmp_path / 'yes.mp4')
    _make_video(tmp_path / 'no.mp4')
    _write_fresh_npy(str(tmp_path), 'yes.mp4')
    assert video_is_embedded(str(tmp_path), 'yes.mp4') is True
    assert video_is_embedded(str(tmp_path), 'no.mp4') is False
    assert video_is_embedded(str(tmp_path), 'gone.mp4') is False


def test_get_embedding_compute_false_returns_cache_hit(tmp_path):
    import numpy as np
    _make_video(tmp_path / 'clip.mp4')
    _write_fresh_npy(str(tmp_path), 'clip.mp4')
    emb = get_video_embedding(str(tmp_path), 'clip.mp4', compute=False)
    assert emb is not None
    assert np.allclose(emb, np.ones(4, dtype='float32'))


def test_get_embedding_compute_false_does_not_compute_on_miss(tmp_path, monkeypatch):
    _make_video(tmp_path / 'clip.mp4')

    def _boom(*a, **k):
        raise AssertionError('must not extract/encode when compute=False')

    monkeypatch.setattr('simpleparty.embeddings.extract_keyframes', _boom)
    monkeypatch.setattr('simpleparty.embeddings._get_duration', _boom)
    assert get_video_embedding(str(tmp_path), 'clip.mp4', compute=False) is None


def test_prune_leaves_inflight_temp_files_alone(tmp_path):
    # A concurrent _atomic_write writes .emb-*.tmp then os.replace()s it; prune
    # must not delete it out from under that write.
    edir = embeddings_dir(str(tmp_path))
    edir.mkdir(parents=True)
    tmp = edir / '.emb-abc123.tmp'
    tmp.write_bytes(b'partial')
    orphan = edir / 'gone.mp4__1_2.npy'
    orphan.write_bytes(b'x')

    prune_stale_embeddings(str(tmp_path))

    assert tmp.exists()       # in-flight temp untouched
    assert not orphan.exists()  # orphan cache entry still pruned


def test_is_available_false_when_a_dependency_is_missing(monkeypatch):
    # Cache is per-process global state; force a fresh probe for this test.
    monkeypatch.setattr(embeddings_mod, '_deps_probed', False)
    monkeypatch.setattr(embeddings_mod, '_deps_available', False)

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == 'numpy':
            raise ImportError('simulated: numpy not installed')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', fake_import)
    assert is_available() is False


def test_is_available_caches_the_probe(monkeypatch):
    monkeypatch.setattr(embeddings_mod, '_deps_probed', True)
    monkeypatch.setattr(embeddings_mod, '_deps_available', True)

    import builtins

    def fail_import(name, *args, **kwargs):
        raise AssertionError('must not re-probe once cached')

    monkeypatch.setattr(builtins, '__import__', fail_import)
    assert is_available() is True
