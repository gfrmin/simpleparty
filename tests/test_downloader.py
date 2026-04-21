"""Tests for downloader module: URL validation, hook mapping, path safety."""

import pytest

from simpleparty.downloader import (
    _apply_pp_hook,
    _apply_progress_hook,
    is_path_within,
    validate_url,
)


def test_validate_url_accepts_http():
    assert validate_url('http://example.com/v') == 'http://example.com/v'
    assert validate_url('https://example.com/v?x=1') == 'https://example.com/v?x=1'


def test_validate_url_strips_whitespace():
    assert validate_url('  https://example.com  ') == 'https://example.com'


def test_validate_url_rejects_empty():
    with pytest.raises(ValueError):
        validate_url('')
    with pytest.raises(ValueError):
        validate_url(None)
    with pytest.raises(ValueError):
        validate_url('   ')


def test_validate_url_rejects_non_http():
    for bad in ('javascript:alert(1)', 'ftp://x', 'file:///etc/passwd', 'example.com'):
        with pytest.raises(ValueError):
            validate_url(bad)


def test_validate_url_rejects_too_long():
    with pytest.raises(ValueError):
        validate_url('https://' + 'a' * 4000)


def test_progress_hook_downloading_with_total_bytes():
    progress = {}
    state = {}
    _apply_progress_hook(
        {
            'status': 'downloading',
            'filename': '/tmp/v/clip.mp4',
            'downloaded_bytes': 500,
            'total_bytes': 1000,
            'speed': 123.4,
            'eta': 5,
        },
        progress,
        state,
    )
    assert progress['status'] == 'downloading'
    assert progress['phase'] == 'downloading'
    assert progress['filename'] == '/tmp/v/clip.mp4'
    assert progress['downloaded_bytes'] == 500
    assert progress['total_bytes'] == 1000
    assert progress['percent'] == 50
    assert progress['speed'] == 123.4
    assert progress['eta'] == 5


def test_progress_hook_downloading_falls_back_to_estimate():
    progress = {}
    _apply_progress_hook(
        {
            'status': 'downloading',
            'downloaded_bytes': 250,
            'total_bytes_estimate': 1000,
        },
        progress,
        {},
    )
    assert progress['total_bytes'] == 1000
    assert progress['percent'] == 25


def test_progress_hook_percent_zero_when_total_unknown():
    progress = {}
    _apply_progress_hook(
        {'status': 'downloading', 'downloaded_bytes': 100},
        progress,
        {},
    )
    assert progress['total_bytes'] == 0
    assert progress['percent'] == 0


def test_progress_hook_finished_captures_final_path():
    progress = {}
    state = {}
    _apply_progress_hook(
        {'status': 'finished', 'filename': '/tmp/v/clip.mp4'},
        progress,
        state,
    )
    assert progress['phase'] == 'post-processing'
    assert progress['percent'] == 100
    assert state['final_path'] == '/tmp/v/clip.mp4'


def test_progress_hook_error_sets_phase():
    progress = {}
    _apply_progress_hook({'status': 'error'}, progress, {})
    assert progress['phase'] == 'error'


def test_pp_hook_updates_final_path_from_info_dict():
    state = {'final_path': '/tmp/v/old.webm'}
    _apply_pp_hook(
        {'status': 'finished', 'info_dict': {'filepath': '/tmp/v/new.mp4'}},
        {},
        state,
    )
    assert state['final_path'] == '/tmp/v/new.mp4'


def test_is_path_within_nested(tmp_path):
    (tmp_path / 'sub').mkdir()
    assert is_path_within(str(tmp_path), str(tmp_path / 'sub'))


def test_is_path_within_self(tmp_path):
    assert is_path_within(str(tmp_path), str(tmp_path))


def test_is_path_within_rejects_traversal(tmp_path):
    assert not is_path_within(str(tmp_path), str(tmp_path.parent))


def test_is_path_within_rejects_sibling(tmp_path):
    sibling = tmp_path.parent / (tmp_path.name + '_other')
    assert not is_path_within(str(tmp_path), str(sibling))
