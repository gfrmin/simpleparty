"""Tests for pure URL/filter helpers."""

from simpleparty.library import filter_videos_by_starred
from simpleparty.urls import (
    ViewState,
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


def test_viewstate_from_params_roundtrip():
    params = {'tags': 'a, b', 'sort': 'name', 'dir': 'asc', 'starred': '1'}
    view = ViewState.from_params(params)
    assert view == ViewState(tags=('a', 'b'), sort='name', direction='asc', starred=True)
    assert view.query_params() == {
        'tags': 'a,b', 'sort': 'name', 'dir': 'asc', 'starred': '1',
    }


def test_viewstate_defaults_omitted():
    assert ViewState().query_params() == {}
    assert ViewState.from_params({}).query_params() == {}
    assert ViewState.from_params({'sort': 'bogus', 'dir': 'bogus'}).query_params() == {}


def test_url_for_browse_byte_equality():
    # Known-good strings captured from the pre-ViewState builders.
    assert url_for_browse('x y', ViewState(tags=('cat',))) == '/browse?path=x+y&tags=cat'
    assert url_for_browse('foo', ViewState(starred=True)) == '/browse?path=foo&starred=1'
    assert url_for_browse(
        'd', ViewState(tags=('a', 'b'), sort='name', direction='asc', starred=True),
    ) == '/browse?path=d&tags=a%2Cb&sort=name&dir=asc&starred=1'
    assert url_for_browse('') == '/'


def test_url_for_play_byte_equality():
    assert url_for_play(
        'd', 3,
        ViewState(tags=('a', 'b'), sort='name', direction='asc', starred=True),
        shuffle=True, seed=7, pos=2, video='v.mp4',
    ) == '/play?path=d&idx=3&video=v.mp4&shuffle=1&seed=7&pos=2&tags=a%2Cb&sort=name&dir=asc&starred=1'
    assert url_for_play('d', 0, ViewState(sort='date', direction='desc')) == '/play?path=d&idx=0'
    assert url_for_play('d', 0) == '/play?path=d&idx=0'


def test_url_for_browse_includes_starred():
    assert 'starred=1' in url_for_browse('foo', ViewState(starred=True))
    assert 'starred' not in url_for_browse('foo', ViewState(starred=False))


def test_url_for_browse_starred_only_yields_browse_url():
    # With no path but starred=True, we still need a real URL (not '/')
    url = url_for_browse('', ViewState(starred=True))
    assert url.startswith('/browse?')
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
