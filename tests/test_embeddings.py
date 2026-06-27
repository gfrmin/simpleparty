"""Tests for the embedding cache path/invalidation logic.

These cover the pure-functional parts that need neither a GPU nor CLIP
weights: cache filenames are stat-keyed, lookups invalidate when a video
changes, the .fail sentinel short-circuits, and pruning drops stale files.
"""

import os
from pathlib import Path

from simpleparty.embeddings import (
    CLIP_MODEL_ID,
    embeddings_dir,
    embed_cache_paths,
    cached_embedding_path,
    fail_marker_path,
    prune_stale_embeddings,
)
from simpleparty.tagger import SIMPLEPARTY_DIR


def _make_video(path, content=b'data'):
    path.write_bytes(content)
    return path


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
