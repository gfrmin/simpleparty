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
    is_starred, set_starred, rewrite_tags,
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


def test_is_starred():
    assert is_starred({'starred': True}) is True
    assert is_starred({'starred': False}) is False
    assert is_starred({}) is False
    assert is_starred(None) is False
    assert is_starred({'tags': ['x']}) is False


def test_set_starred_adds_flag_to_existing_entry():
    tags = {'a.mp4': {'tags': ['x'], 'status': 'confirmed'}}
    set_starred(tags, 'a.mp4', True)
    assert tags['a.mp4'] == {'tags': ['x'], 'status': 'confirmed', 'starred': True}


def test_set_starred_creates_minimal_entry():
    tags = {}
    set_starred(tags, 'a.mp4', True)
    assert tags == {'a.mp4': {'starred': True}}


def test_set_starred_unstar_preserves_entry_with_other_state():
    tags = {'a.mp4': {'tags': ['x'], 'starred': True}}
    set_starred(tags, 'a.mp4', False)
    assert tags == {'a.mp4': {'tags': ['x']}}


def test_set_starred_unstar_drops_now_empty_entry():
    tags = {'a.mp4': {'starred': True}}
    set_starred(tags, 'a.mp4', False)
    assert tags == {}


def test_set_starred_unstar_missing_is_noop():
    tags = {}
    set_starred(tags, 'a.mp4', False)
    assert tags == {}


def test_starred_round_trip(tmp_path):
    save_tags(str(tmp_path), {'a.mp4': {'starred': True}})
    loaded = load_tags(str(tmp_path))
    assert is_starred(loaded.get('a.mp4', {})) is True
    set_starred(loaded, 'a.mp4', False)
    save_tags(str(tmp_path), loaded)
    assert load_tags(str(tmp_path)) == {}


def test_untagged_videos(tmp_path):
    (tmp_path / 'a.mp4').write_text('')
    (tmp_path / 'b.mkv').write_text('')
    (tmp_path / 'notes.txt').write_text('')
    (tmp_path / '.hidden.mp4').write_text('')

    existing = {'a.mp4': {'tags': ['x']}}
    result = untagged_videos(str(tmp_path), existing)
    assert result == ['b.mkv']


# --- Tags cache + update_tags ---

def test_load_tags_cached_until_file_changes(tmp_path):
    from simpleparty.tagger import load_tags, save_tags
    save_tags(str(tmp_path), {'a.mp4': {'tags': ['x']}})
    first = load_tags(str(tmp_path))
    assert load_tags(str(tmp_path)) is first  # served from cache

    # Rewrite out-of-band (different mtime/size) -> reloaded
    tags_file = tmp_path / SIMPLEPARTY_DIR / TAGS_FILENAME
    os.utime(tags_file, ns=(0, 0))
    tags_file.write_text(json.dumps({'b.mp4': {'tags': ['y']}}))
    assert load_tags(str(tmp_path)) == {'b.mp4': {'tags': ['y']}}


def test_save_tags_refreshes_cache(tmp_path):
    from simpleparty.tagger import load_tags, save_tags
    save_tags(str(tmp_path), {'a.mp4': {'tags': ['x']}})
    load_tags(str(tmp_path))
    save_tags(str(tmp_path), {'a.mp4': {'tags': ['z']}})
    assert load_tags(str(tmp_path))['a.mp4']['tags'] == ['z']


def test_load_tags_index_lowercases(tmp_path):
    from simpleparty.tagger import load_tags_index, save_tags
    save_tags(str(tmp_path), {'a.mp4': {'tags': ['Cat', 'DOG']}, 'b.mp4': {}})
    _, index = load_tags_index(str(tmp_path))
    assert index == {'a.mp4': frozenset({'cat', 'dog'}), 'b.mp4': frozenset()}


def test_update_tags_concurrent_writers_lose_nothing(tmp_path):
    import threading
    from simpleparty.tagger import load_tags, update_tags

    def writer(i):
        update_tags(str(tmp_path), lambda tags: {**tags, f'v{i}.mp4': {'tags': [f't{i}']}})

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    loaded = load_tags(str(tmp_path))
    assert set(loaded) == {f'v{i}.mp4' for i in range(8)}


def test_update_tags_transform_gets_copy(tmp_path):
    from simpleparty.tagger import load_tags, save_tags, update_tags
    save_tags(str(tmp_path), {'a.mp4': {'tags': ['x'], 'starred': True}})
    before = load_tags(str(tmp_path))

    def transform(tags):
        tags['a.mp4']['status'] = 'confirmed'
        return tags

    update_tags(str(tmp_path), transform)
    assert 'status' not in before['a.mp4']  # cached snapshot untouched
    assert load_tags(str(tmp_path))['a.mp4']['status'] == 'confirmed'


# --- Thumb / frame set scans ---

def test_list_thumbs(tmp_path):
    from simpleparty.tagger import list_thumbs
    thumbs = tmp_path / SIMPLEPARTY_DIR / THUMB_DIR
    thumbs.mkdir(parents=True)
    (thumbs / 'a.mp4.jpg').write_bytes(b'')
    (thumbs / 'b.mkv.jpg').write_bytes(b'')
    (thumbs / 'notes.txt').write_bytes(b'')
    assert list_thumbs(str(tmp_path)) == frozenset({'a.mp4', 'b.mkv'})
    assert list_thumbs(str(tmp_path / 'nope')) == frozenset()


def test_videos_with_frames(tmp_path):
    from simpleparty.tagger import FRAMES_DIR, videos_with_frames
    frames = tmp_path / FRAMES_DIR
    frames.mkdir(parents=True)
    (frames / 'a.mp4.f0.jpg').write_bytes(b'')
    (frames / 'a.mp4.f1.jpg').write_bytes(b'')
    (frames / 'b.mkv.f0.jpg').write_bytes(b'')
    assert videos_with_frames(str(tmp_path)) == frozenset({'a.mp4', 'b.mkv'})


def test_untagged_videos_accepts_prelisted_names(tmp_path):
    names = ['a.mp4', 'b.mp4', '.hidden.mp4', 'c.txt']
    existing = {'a.mp4': {'tags': ['x'], 'status': 'confirmed'}}
    assert untagged_videos(str(tmp_path), existing, names=names) == ['b.mp4']


# --- rewrite_tags: directory-level rename/merge/remove ---

def test_rewrite_tags_rename():
    tags = {'v.mp4': {'tags': ['scifi', 'action'], 'status': 'confirmed'}}
    out = rewrite_tags(tags, {'scifi': 'science fiction'})
    assert out['v.mp4']['tags'] == ['science fiction', 'action']


def test_rewrite_tags_rename_uses_entered_casing_across_variants():
    tags = {'v.mp4': {'tags': ['Scifi', 'scifi'], 'status': 'confirmed'}}
    out = rewrite_tags(tags, {'scifi': 'Science Fiction'})
    # both case variants collapse to the single entered-casing target
    assert out['v.mp4']['tags'] == ['Science Fiction']


def test_rewrite_tags_merge_dedups():
    # video already has both the source and the target tag -> one survives
    tags = {'v.mp4': {'tags': ['action', 'fight'], 'status': 'confirmed'}}
    out = rewrite_tags(tags, {'fight': 'action'})
    assert out['v.mp4']['tags'] == ['action']


def test_rewrite_tags_remove():
    tags = {'v.mp4': {'tags': ['generic', 'action'], 'status': 'confirmed'}}
    out = rewrite_tags(tags, {'generic': None})
    assert out['v.mp4']['tags'] == ['action']


def test_rewrite_tags_case_insensitive_match():
    tags = {
        'a.mp4': {'tags': ['Scifi'], 'status': 'confirmed'},
        'b.mp4': {'tags': ['scifi'], 'status': 'suggested'},
    }
    out = rewrite_tags(tags, {'scifi': 'SF'})
    assert out['a.mp4']['tags'] == ['SF']
    assert out['b.mp4']['tags'] == ['SF']


def test_rewrite_tags_suggest_scores_rename():
    tags = {'v.mp4': {'tags': ['scifi'], 'status': 'suggested',
                      'suggest_scores': {'scifi': 0.7}}}
    out = rewrite_tags(tags, {'scifi': 'SF'})
    assert out['v.mp4']['suggest_scores'] == {'SF': 0.7}


def test_rewrite_tags_suggest_scores_collision_keeps_higher():
    tags = {'v.mp4': {'tags': ['action', 'fight'], 'status': 'suggested',
                      'suggest_scores': {'action': 0.5, 'fight': 0.9}}}
    out = rewrite_tags(tags, {'fight': 'action'})
    assert out['v.mp4']['suggest_scores'] == {'action': 0.9}


def test_rewrite_tags_suggest_scores_drop():
    tags = {'v.mp4': {'tags': ['generic'], 'status': 'suggested',
                      'suggest_scores': {'generic': 0.4, 'keep': 0.6}}}
    out = rewrite_tags(tags, {'generic': None})
    assert out['v.mp4']['suggest_scores'] == {'keep': 0.6}


def test_rewrite_tags_preserves_status_and_starred():
    tags = {'v.mp4': {'tags': ['scifi'], 'status': 'suggested',
                      'starred': True, 'suggest_source': 'model'}}
    out = rewrite_tags(tags, {'scifi': 'SF'})
    assert out['v.mp4']['status'] == 'suggested'
    assert out['v.mp4']['starred'] is True
    assert out['v.mp4']['suggest_source'] == 'model'


def test_rewrite_tags_keeps_entry_when_tags_become_empty():
    tags = {'v.mp4': {'tags': ['generic'], 'status': 'confirmed', 'starred': True}}
    out = rewrite_tags(tags, {'generic': None})
    assert 'v.mp4' in out
    assert out['v.mp4']['tags'] == []
    assert out['v.mp4']['starred'] is True


def test_rewrite_tags_unknown_key_is_noop():
    tags = {'v.mp4': {'tags': ['scifi'], 'status': 'confirmed'}}
    out = rewrite_tags(tags, {'nonexistent': 'x'})
    assert out['v.mp4']['tags'] == ['scifi']


def test_rewrite_tags_does_not_mutate_input():
    tags = {'v.mp4': {'tags': ['scifi', 'action'], 'status': 'confirmed'}}
    rewrite_tags(tags, {'scifi': 'SF'})
    assert tags['v.mp4']['tags'] == ['scifi', 'action']
