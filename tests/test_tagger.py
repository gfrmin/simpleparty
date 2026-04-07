"""Tests for tagger module: tag I/O, paths, and thumbnail helpers."""

import json
import os
import tempfile
from pathlib import Path

from simpleparty.tagger import (
    SIMPLEPARTY_DIR, TAGS_FILENAME, MODEL_FILENAME, THUMB_DIR,
    _LEGACY_TAGS, _LEGACY_MODEL,
    load_tags, save_tags, model_path, thumb_path,
    untagged_videos, confirmed_entries,
)


def test_save_and_load_tags(tmp_path):
    tags = {'video.mp4': {'tags': ['cat', 'dog'], 'status': 'confirmed'}}
    save_tags(str(tmp_path), tags)

    # Should save under .simpleparty/tags.json
    sp = tmp_path / SIMPLEPARTY_DIR
    assert sp.is_dir()
    assert (sp / TAGS_FILENAME).exists()

    loaded = load_tags(str(tmp_path))
    assert loaded == tags


def test_load_tags_legacy_fallback(tmp_path):
    """Old .simpleparty-tags.json should still be readable."""
    tags = {'old.mp4': {'tags': ['legacy'], 'status': 'confirmed'}}
    legacy_file = tmp_path / _LEGACY_TAGS
    legacy_file.write_text(json.dumps(tags))

    loaded = load_tags(str(tmp_path))
    assert loaded == tags


def test_load_tags_new_path_preferred_over_legacy(tmp_path):
    """If both old and new exist, new wins."""
    old_tags = {'old.mp4': {'tags': ['old']}}
    new_tags = {'new.mp4': {'tags': ['new']}}

    (tmp_path / _LEGACY_TAGS).write_text(json.dumps(old_tags))
    sp = tmp_path / SIMPLEPARTY_DIR
    sp.mkdir()
    (sp / TAGS_FILENAME).write_text(json.dumps(new_tags))

    loaded = load_tags(str(tmp_path))
    assert loaded == new_tags


def test_load_tags_empty_dir(tmp_path):
    assert load_tags(str(tmp_path)) == {}


def test_model_path_new_location(tmp_path):
    sp = tmp_path / SIMPLEPARTY_DIR
    sp.mkdir()
    model_file = sp / MODEL_FILENAME
    model_file.write_text('model')

    mp = model_path(str(tmp_path))
    assert mp == model_file
    assert mp.exists()


def test_model_path_legacy_fallback(tmp_path):
    legacy = tmp_path / _LEGACY_MODEL
    legacy.write_text('model')

    mp = model_path(str(tmp_path))
    assert mp == legacy
    assert mp.exists()


def test_model_path_returns_new_when_neither_exists(tmp_path):
    mp = model_path(str(tmp_path))
    assert str(mp) == str(tmp_path / SIMPLEPARTY_DIR / MODEL_FILENAME)
    assert not mp.exists()


def test_thumb_path(tmp_path):
    tp = thumb_path(str(tmp_path), 'clip.mp4')
    expected = tmp_path / SIMPLEPARTY_DIR / THUMB_DIR / 'clip.mp4.jpg'
    assert tp == expected


def test_confirmed_entries_filters_suggested():
    tags = {
        'a.mp4': {'tags': ['x'], 'status': 'confirmed'},
        'b.mp4': {'tags': ['y'], 'status': 'suggested'},
        'c.mp4': {'tags': ['z']},  # no status = confirmed
    }
    result = confirmed_entries(tags)
    assert 'a.mp4' in result
    assert 'b.mp4' not in result
    assert 'c.mp4' in result


def test_untagged_videos(tmp_path):
    (tmp_path / 'a.mp4').write_text('')
    (tmp_path / 'b.mkv').write_text('')
    (tmp_path / 'notes.txt').write_text('')
    (tmp_path / '.hidden.mp4').write_text('')

    existing = {'a.mp4': {'tags': ['x']}}
    result = untagged_videos(str(tmp_path), existing)
    assert result == ['b.mkv']
