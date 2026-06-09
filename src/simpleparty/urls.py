"""URL building and query-string parsing."""

import urllib.parse
from dataclasses import dataclass


def parse_query(url):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: v[0] for k, v in params.items()}


@dataclass(frozen=True)
class ViewState:
    """Cross-cutting browse/play view parameters carried through every URL.

    Immutable; derive variants with dataclasses.replace().
    """

    tags: tuple = ()
    sort: str = 'date'
    direction: str = 'desc'
    starred: bool = False

    @classmethod
    def from_params(cls, params):
        sort, direction = parse_sort_params(params)
        return cls(
            tags=tuple(parse_tags_param(params)),
            sort=sort,
            direction=direction,
            starred=parse_starred_param(params),
        )

    def query_params(self):
        """URL params dict with default values omitted."""
        params = {}
        if self.tags:
            params['tags'] = ','.join(self.tags)
        if self.sort and self.sort != 'date':
            params['sort'] = self.sort
        if self.direction and self.direction != 'desc':
            params['dir'] = self.direction
        if self.starred:
            params['starred'] = '1'
        return params


def url_for_browse(path='', view=None):
    params = {}
    if path:
        params['path'] = path
    if view is not None:
        params.update(view.query_params())
    return '/' if not params else '/browse?' + urllib.parse.urlencode(params)


def url_for_play(dir_path, idx, view=None, *, video=None, shuffle=False, seed=None, pos=None):
    params = {'path': dir_path, 'idx': str(idx)}
    if video:
        params['video'] = video
    if shuffle:
        params['shuffle'] = '1'
        if seed is not None:
            params['seed'] = str(seed)
        if pos is not None:
            params['pos'] = str(pos)
    if view is not None:
        params.update(view.query_params())
    return '/play?' + urllib.parse.urlencode(params)


def url_for_video(path):
    return '/video/' + '/'.join(urllib.parse.quote(p, safe='') for p in path.split('/'))


def parse_tags_param(params):
    """Parse comma-separated tags from URL params into a list."""
    raw = params.get('tags', '')
    return [t.strip() for t in raw.split(',') if t.strip()] if raw else []


def parse_starred_param(params):
    """Return True if the request asks for starred-only filtering."""
    return params.get('starred', '') == '1'


_SORT_FIELDS = {'name', 'size', 'date', 'length'}
_SORT_DIRS = {'asc', 'desc'}


def parse_sort_params(params):
    """Return (sort_field, direction) with defaults date/desc."""
    sort = params.get('sort', 'date')
    if sort not in _SORT_FIELDS:
        sort = 'date'
    direction = params.get('dir', 'desc')
    if direction not in _SORT_DIRS:
        direction = 'desc'
    return sort, direction


def safe_int(s, default=0):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default
