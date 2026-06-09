"""URL building and query-string parsing."""

import urllib.parse


def parse_query(url):
    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    return {k: v[0] for k, v in params.items()}


def url_for_browse(path='', tags=None, sort=None, direction=None, starred=False):
    params = {}
    if path:
        params['path'] = path
    if tags:
        params['tags'] = ','.join(tags)
    if sort and sort != 'date':
        params['sort'] = sort
    if direction and direction != 'desc':
        params['dir'] = direction
    if starred:
        params['starred'] = '1'
    return '/' if not params else '/browse?' + urllib.parse.urlencode(params)


def url_for_play(dir_path, idx, shuffle=False, seed=None, pos=None, tags=None, video=None, sort=None, direction=None, starred=False):
    params = {'path': dir_path, 'idx': str(idx)}
    if video:
        params['video'] = video
    if shuffle:
        params['shuffle'] = '1'
        if seed is not None:
            params['seed'] = str(seed)
        if pos is not None:
            params['pos'] = str(pos)
    if tags:
        params['tags'] = ','.join(tags)
    if sort and sort != 'date':
        params['sort'] = sort
    if direction and direction != 'desc':
        params['dir'] = direction
    if starred:
        params['starred'] = '1'
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
