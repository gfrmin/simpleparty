"""Tests for pure functions in server module."""

from simpleparty.server import (
    filter_videos_by_starred,
    parse_starred_param,
    url_for_browse,
    url_for_play,
)


def test_parse_starred_param():
    assert parse_starred_param({'starred': '1'}) is True
    assert parse_starred_param({'starred': '0'}) is False
    assert parse_starred_param({'starred': ''}) is False
    assert parse_starred_param({}) is False
    assert parse_starred_param({'starred': 'true'}) is False  # only '1' counts


def test_url_for_browse_includes_starred():
    assert 'starred=1' in url_for_browse('foo', starred=True)
    assert 'starred' not in url_for_browse('foo', starred=False)


def test_url_for_browse_starred_only_yields_browse_url():
    # With no path but starred=True, we still need a real URL (not '/')
    url = url_for_browse('', starred=True)
    assert url.startswith('/browse?')
    assert 'starred=1' in url


def test_url_for_play_includes_starred():
    url = url_for_play('foo', 0, starred=True)
    assert 'starred=1' in url


def test_filter_videos_by_starred_disabled_returns_input():
    videos = [{'name': 'a.mp4'}, {'name': 'b.mp4'}]
    assert filter_videos_by_starred(videos, {}, False) is videos


def test_filter_videos_by_starred_no_tags_map_returns_empty():
    videos = [{'name': 'a.mp4'}]
    assert filter_videos_by_starred(videos, None, True) == []
    assert filter_videos_by_starred(videos, {}, True) == []


def test_filter_videos_by_starred_keeps_only_starred():
    videos = [
        {'name': 'a.mp4'},
        {'name': 'b.mp4'},
        {'name': 'c.mp4'},
    ]
    tags_map = {
        'a.mp4': {'starred': True},
        'b.mp4': {'tags': ['cat']},  # not starred
        'c.mp4': {'tags': ['dog'], 'starred': True},
    }
    result = filter_videos_by_starred(videos, tags_map, True)
    assert [v['name'] for v in result] == ['a.mp4', 'c.mp4']


def test_filter_videos_by_starred_handles_missing_entries():
    videos = [{'name': 'unknown.mp4'}, {'name': 'a.mp4'}]
    tags_map = {'a.mp4': {'starred': True}}
    result = filter_videos_by_starred(videos, tags_map, True)
    assert [v['name'] for v in result] == ['a.mp4']
