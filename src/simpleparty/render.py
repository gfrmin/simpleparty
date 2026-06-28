"""HTML rendering. All pages and fragments are built as strings here."""

import json
import urllib.parse
from dataclasses import replace
from html import escape as esc
from pathlib import Path

from simpleparty import __version__, jobs
from simpleparty.icons import icon
from simpleparty.library import _compute_related_videos, resolve_path
from simpleparty.state import CONFIG as _config
from simpleparty.urls import ViewState, url_for_browse, url_for_play, url_for_shuffle, url_for_video


# --- Format helpers ---

def fmt_size(b):
    if b < 1024:
        return f'{b} B'
    if b < 1048576:
        return f'{b / 1024:.1f} KB'
    if b < 1073741824:
        return f'{b / 1048576:.1f} MB'
    return f'{b / 1073741824:.1f} GB'



# --- HTML rendering ---



def render_page(title, body):
    return (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        f'<title>{esc(title)}</title>\n'
        f'<link rel="stylesheet" href="/static/style.css?v={__version__}">\n'
        '<script src="https://unpkg.com/htmx.org@2.0.4"></script>\n'
        '</head>\n<body>\n'
        f'{body}\n'
        '</body>\n</html>'
    )


def render_nav(path, encrypted_dir=None):
    parts = path.split('/') if path else []
    pieces = ['<a class="crumb brand" href="/">SimpleParty</a>']
    # Build (label, href) for each segment, then collapse the middle on deep
    # paths so the sticky breadcrumb stays compact (and doesn't wrap on phones).
    segs = []
    acc = ''
    for part in parts:
        acc += ('/' if acc else '') + part
        segs.append((part, url_for_browse(acc)))
    if len(segs) > 4:
        ellipsis_href = segs[-3][1]
        segs = [('…', ellipsis_href)] + segs[-2:]
    last = len(segs) - 1
    for i, (label, href) in enumerate(segs):
        pieces.append('<span class="crumb-sep" aria-hidden="true">/</span>')
        cur = ' aria-current="page"' if i == last else ''
        pieces.append(f'<a class="crumb" href="{esc(href)}"{cur}>{esc(label)}</a>')
    pieces.append('<span class="nav-spacer"></span>')
    if _config.get('allow_download'):
        pieces.append(f'<a class="btn" href="/download">{icon("download")} Downloads</a>')
    if encrypted_dir is not None:
        parent = str(Path(encrypted_dir).parent)
        if parent == '.':
            parent = ''
        pieces.append(
            f'<form hx-post="/lock" hx-confirm="Lock this directory?" style="display:inline">'
            f'<input type="hidden" name="path" value="{esc(encrypted_dir)}">'
            f'<input type="hidden" name="redirect" value="{esc(url_for_browse(parent))}">'
            f'<button type="submit" class="btn btn-lock">Lock</button>'
            f'</form>'
        )
    return '<nav aria-label="Breadcrumb">' + ''.join(pieces) + '</nav>'


def _render_train_btn(path_param, is_busy):
    cls = 'btn btn-train busy' if is_busy else 'btn btn-train'
    disabled = ' disabled' if is_busy else ''
    label = f'{icon("tag")} Training\u2026' if is_busy else f'{icon("tag")} Train'
    return (
        f'<form hx-post="/train" style="display:inline" id="train-form">'
        f'<input type="hidden" name="path" value="{path_param}">'
        f'<button class="{cls}"{disabled} hx-disabled-elt="this">'
        f'<span class="btn-spinner"></span>'
        f'<span class="btn-label">{label}</span>'
        f'</button>'
        f'</form>'
    )


def render_coverage_controls(rel_path, coverage, is_busy):
    """The tag-bar controls: a coverage badge plus the two coverage-gated
    actions. Embed (only when videos are un-embedded) is the sole thing that
    runs CLIP; Train (only when embeddings exist) consumes them. Rendered both
    at page load and OOB-swapped by /tag-status on every poll, so finishing a
    job live-updates the badge and which actions are offered."""
    path_param = esc(rel_path)
    total = coverage['total']
    embedded = coverage['embedded']
    missing = coverage['missing']
    badge = (
        f'<span class="coverage-badge" title="videos with CLIP embeddings">'
        f'{icon("embed")} {embedded}/{total} embedded'
        + (f' · {missing} missing' if missing else '')
        + '</span>'
    )
    embed_html = ''
    if missing and not is_busy:
        # "Embed selected" gathers checked per-row boxes; the hidden #embed-path
        # rides along so the handler knows the directory. "Embed all missing"
        # is the no-selection shortcut.
        embed_html = (
            f'<form hx-post="/embed" style="display:inline" id="embed-form">'
            f'<input type="hidden" name="path" value="{path_param}">'
            f'<button class="btn btn-embed" hx-disabled-elt="this">'
            f'{icon("embed")} Embed all missing ({missing})</button>'
            f'</form>'
            f'<input type="hidden" id="embed-path" name="path" value="{path_param}">'
            f'<button class="btn btn-embed-selected" hx-post="/embed" '
            f'hx-include=".embed-check:checked, #embed-path" hx-disabled-elt="this">'
            f'{icon("embed")} Embed selected</button>'
        )
    train_html = _render_train_btn(path_param, is_busy) if embedded else ''
    return (
        f'<span id="tag-controls" class="tag-controls">'
        f'{badge}{embed_html}{train_html}</span>'
    )


PAGE_SIZE = 100        # browse grid items per chunk
PLAYLIST_PAGE = 50     # play-page playlist items per chunk


def _load_more_sentinel(url):
    """Invisible htmx trigger: fetches the next chunk when scrolled into view."""
    return (
        f'<div class="load-more" hx-get="{esc(url)}" '
        f'hx-trigger="revealed" hx-swap="outerHTML"></div>'
    )


def _browse_frag_url(path, view, offset):
    params = {}
    if path:
        params['path'] = path
    params.update(view.query_params())
    params['frag'] = 'list'
    params['offset'] = str(offset)
    return '/browse?' + urllib.parse.urlencode(params)


def render_video_item(v, i, data_path, view, *, current_idx=-1, tags_map=None,
                      thumbs=frozenset(), embed_missing=frozenset()):
    """One video card. `i` is the video's index in the full filtered+sorted
    list (play URLs are index-based)."""
    cls = ' playing' if i == current_idx else ''
    play_url = url_for_play(data_path, i, view, video=v['name'])
    pieces = [f'<div class="item item-video{cls}">']
    if v['name'] in embed_missing:
        # Un-embedded: a checkbox so this row can join an "Embed selected" batch.
        pieces.append(
            f'<label class="embed-check-label" title="select to embed">'
            f'<input type="checkbox" class="embed-check" name="video" '
            f'value="{esc(v["name"])}" aria-label="Select {esc(v["name"])} to embed">'
            f'</label>'
        )
    thumb_url = f'/thumb/{urllib.parse.quote(v["path"])}'
    if v['name'] in thumbs:
        thumb_html = f'<img src="{thumb_url}" loading="lazy" class="item-thumb" alt="">'
    else:
        thumb_html = f'<div class="item-thumb item-thumb-placeholder" aria-hidden="true">{icon("film", cls="item-thumb-placeholder-icon")}</div>'
    pieces.append(
        f'<a class="item-link" href="{esc(play_url)}">'
        f'{thumb_html}'
        f'<span class="item-info">'
        f'<span class="item-name">{esc(v["name"])}</span>'
        f'<span class="item-size">{fmt_size(v["size"])}</span>'
        f'</span>'
        f'</a>'
    )
    if _config['allow_delete']:
        pieces.append(
            f'<form hx-post="/delete" hx-target="closest .item" hx-swap="delete" '
            f'hx-confirm="Delete {esc(v["name"])}?">'
            f'<input type="hidden" name="path" value="{esc(v["path"])}">'
            f'<button type="submit" class="btn-del" title="Delete {esc(v["name"])}" '
            f'aria-label="Delete {esc(v["name"])}">'
            f'{icon("trash", label="Delete " + v["name"])}</button>'
            f'</form>'
        )
    if tags_map and v['name'] in tags_map:
        entry = tags_map[v['name']]
        video_tags = entry.get('tags', [])
        if video_tags:
            is_suggested = entry.get('status') == 'suggested'
            tags_text = esc(' \u00B7 '.join(video_tags[:8]))
            tcls = ' suggested' if is_suggested else ''
            prefix = (
                '<span class="visually-hidden">Suggested tags: </span>'
                '<span class="badge-suggested" aria-hidden="true">Suggested</span> '
            ) if is_suggested else ''
            pieces.append(f'<div class="item-tags{tcls}">{prefix}{tags_text}</div>')
    pieces.append('</div>')
    return ''.join(pieces)


def render_video_items(data, view, offset, *, current_idx=-1, tags_map=None,
                       thumbs=frozenset(), embed_missing=frozenset()):
    """A chunk of video cards plus, when more remain, a lazy-load sentinel."""
    videos = data['videos']
    pieces = [
        render_video_item(v, offset + j, data['path'], view,
                          current_idx=current_idx, tags_map=tags_map, thumbs=thumbs,
                          embed_missing=embed_missing)
        for j, v in enumerate(videos[offset:offset + PAGE_SIZE])
    ]
    if offset + PAGE_SIZE < len(videos):
        pieces.append(_load_more_sentinel(
            _browse_frag_url(data['path'], view, offset + PAGE_SIZE)))
    return ''.join(pieces)


def render_file_list(data, view, current_idx=-1, show_shuffle=True, tags_map=None, thumbs=frozenset()):
    pieces = ['<div id="file-list">']

    shuffle_btn = ''
    if show_shuffle and data['videos']:
        shuffle_url = url_for_shuffle(data['path'], view)
        shuffle_btn = f'<a class="btn btn-primary" href="{esc(shuffle_url)}">{icon("shuffle")} Shuffle Play</a>'
    has_tags = bool(tags_map) and any(e.get('tags') for e in tags_map.values())
    want_action_bar = bool(shuffle_btn) or (
        _config.get('allow_download') and (data['videos'] or data['dirs'])
    ) or (_config['allow_tag'] and has_tags)
    embed_missing = frozenset()
    if want_action_bar:
        tag_controls_html = ''
        tag_status_html = ''
        if data['videos'] and _config['allow_tag'] and _config['has_ffmpeg']:
            from simpleparty.embeddings import embedding_coverage
            resolved_dir = resolve_path(_config.get('root', '.'), data['path'])
            job = jobs.get_tag_job(str(resolved_dir))
            is_busy = bool(job and job.get('running'))
            coverage = embedding_coverage(str(resolved_dir))
            embed_missing = frozenset(coverage['missing_names'])
            # Embed (produce) and Train (consume) are split, coverage-gated, and
            # explicit. Suggesting/accepting tags stay per-video on the play page
            # so a whole-folder pass can't silently bulk-write tags you never saw.
            tag_controls_html = render_coverage_controls(data['path'], coverage, is_busy)
            status_url = f'/tag-status?{urllib.parse.urlencode({"path": data["path"]})}'
            poll = 'every 2s' if is_busy else 'every 10s'
            tag_status_html = (
                f'<div hx-get="{status_url}" '
                f'hx-trigger="load,{poll}" hx-swap="outerHTML" '
                f'class="tag-progress-panel{" active" if is_busy else ""}" '
                f'role="status" aria-live="polite" id="tag-progress"></div>'
            )
        download_inner_html = ''
        download_status_html = ''
        manage_html = ''
        if _config['allow_tag'] and has_tags:
            manage_html = (
                f'<details class="tag-manager-details">'
                f'<summary class="btn">{icon("tag")} Manage tags</summary>'
                f'<div style="flex-basis:100%">'
                f'{render_tag_manager(data["path"], tags_map)}</div>'
                f'</details>'
            )
        if _config['allow_download']:
            path_q = urllib.parse.urlencode({'path': data['path']})
            download_inner_html = (
                f'<details class="download-details">'
                f'<summary class="btn">{icon("download")} Download URL</summary>'
                f'<div style="flex-basis:100%">{render_download_form(data["path"], autofocus=False)}</div>'
                f'</details>'
                f'<a class="btn" href="/download">Manage</a>'
            )
            download_status_html = (
                f'<div hx-get="/download-status?{path_q}&inline=1" hx-trigger="load" '
                f'hx-swap="outerHTML" class="download-progress-panel" '
                f'role="status" aria-live="polite" '
                f'id="download-progress"></div>'
            )
        sort_html = render_sort_pills(data['path'], view) if data['videos'] else ''
        # Build toolbar-lib groups (coverage/embed/train | download/manage)
        lib_groups = ''
        if tag_controls_html:
            lib_groups += f'<div class="lib-group">{tag_controls_html}</div>'
        if download_inner_html:
            lib_groups += f'<div class="lib-group">{download_inner_html}</div>'
        if manage_html:
            lib_groups += f'<div class="lib-group">{manage_html}</div>'
        toolbar_view = f'<div class="toolbar-view">{shuffle_btn}{sort_html}</div>'
        toolbar_lib = f'<div class="toolbar-lib">{lib_groups}</div>' if lib_groups else ''
        pieces.append(
            f'<div class="action-bar">'
            f'<div class="toolbar">{toolbar_view}{toolbar_lib}</div>'
            f'{tag_status_html}'
            f'{download_status_html}'
            f'</div>'
        )

    for d in data['dirs']:
        if d['encrypted'] and not d['unlocked']:
            dir_icon = icon('lock')
            state = ' <span class="visually-hidden">(encrypted, locked)</span>'
        elif d['encrypted']:
            dir_icon = icon('lock-open')
            state = ' <span class="visually-hidden">(encrypted, unlocked)</span>'
        else:
            dir_icon = icon('folder')
            state = ''
        pieces.append(
            f'<a class="item" href="{esc(url_for_browse(d["path"]))}">'
            f'<span class="item-icon" aria-hidden="true">{dir_icon}</span>'
            f'<span class="item-name">{esc(d["name"])}{state}</span>'
            f'</a>'
        )

    pieces.append(render_video_items(
        data, view, 0,
        current_idx=current_idx, tags_map=tags_map, thumbs=thumbs,
        embed_missing=embed_missing,
    ))

    if not data['dirs'] and not data['videos']:
        pieces.append('<div class="empty">Empty directory</div>')

    pieces.append('</div>')
    return ''.join(pieces)


def render_related_videos(data, idx, lower_index, view, thumbs=frozenset()):
    """Render a 'Related Videos' section based on tag overlap."""
    related = _compute_related_videos(data, idx, lower_index)
    if not related:
        return ''
    pieces = [
        '<div id="related-videos">'
        '<h2 class="related-heading">Related Videos</h2>'
        '<div class="related-list">'
    ]
    for video_idx, _overlap in related:
        v = data['videos'][video_idx]
        play_url = url_for_play(data['path'], video_idx, view, video=v['name'])
        has_thumb = v['name'] in thumbs
        thumb_url = f'/thumb/{urllib.parse.quote(v["path"])}'
        if has_thumb:
            thumb_html = f'<img src="{thumb_url}" loading="lazy" class="item-thumb" alt="">'
        else:
            thumb_html = f'<div class="item-thumb item-thumb-placeholder" aria-hidden="true">{icon("film", cls="item-thumb-placeholder-icon")}</div>'
        pieces.append(
            f'<div class="item item-video">'
            f'<a class="item-link" href="{esc(play_url)}">'
            f'{thumb_html}'
            f'<span class="item-info">'
            f'<span class="item-name">{esc(v["name"])}</span>'
            f'</span>'
            f'</a>'
            f'</div>'
        )
    pieces.append('</div></div>')
    return ''.join(pieces)


def render_playlist_item(v, play_url, position, thumbs):
    """One playlist row; position 0 is the currently playing video."""
    is_current = position == 0
    if v['name'] in thumbs:
        thumb_html = f'<img src="/thumb/{urllib.parse.quote(v["path"])}" loading="lazy" class="playlist-thumb" alt="">'
    else:
        thumb_html = f'<div class="playlist-thumb-placeholder" aria-hidden="true">{icon("film", cls="item-thumb-placeholder-icon")}</div>'
    cls = ' playing' if is_current else ''
    label = '\u25B6 Now' if is_current else str(position)
    return (
        f'<a class="playlist-item{cls}" href="{esc(play_url)}">'
        f'{thumb_html}'
        f'<span class="playlist-name">{esc(v["name"])}</span>'
        f'<span class="playlist-pos">{label}</span>'
        f'</a>'
    )


def render_playlist(data, current_idx, play_order, shuffle_seed, view, thumbs=frozenset(), offset=0, frag_only=False):
    """Render playlist in playback order, PLAYLIST_PAGE items at a time.

    The order is deterministic from (current_idx | seed/pos already in the
    URL), so each lazily loaded chunk continues exactly where the previous
    one stopped. With frag_only, returns just the rows + next sentinel.
    """
    n = len(data['videos'])
    is_shuffled = play_order is not None

    # Build ordered list: (video_index, playlist_position)
    # Starting from current position, wrapping around
    if is_shuffled:
        # Find current position in shuffle order
        current_pos = play_order.index(current_idx)
        ordered = [(play_order[(current_pos + i) % n], i) for i in range(n)]
    else:
        current_pos = None
        ordered = [((current_idx + i) % n, i) for i in range(n)]

    pieces = []
    for video_idx, position in ordered[offset:offset + PLAYLIST_PAGE]:
        v = data['videos'][video_idx]
        if is_shuffled:
            pos_in_order = (current_pos + position) % n
            play_url = url_for_play(data['path'], video_idx, view, shuffle=True, seed=shuffle_seed, pos=pos_in_order, video=v['name'])
        else:
            play_url = url_for_play(data['path'], video_idx, view, video=v['name'])
        pieces.append(render_playlist_item(v, play_url, position, thumbs))

    next_offset = offset + PLAYLIST_PAGE
    if next_offset < n:
        cur = data['videos'][current_idx]
        if is_shuffled:
            base = url_for_play(data['path'], current_idx, view, shuffle=True, seed=shuffle_seed, pos=current_pos, video=cur['name'])
        else:
            base = url_for_play(data['path'], current_idx, view, video=cur['name'])
        frag_url = base + '&' + urllib.parse.urlencode(
            {'frag': 'playlist', 'offset': str(next_offset)})
        pieces.append(_load_more_sentinel(frag_url))

    if frag_only:
        return ''.join(pieces)
    return (
        '<div class="playlist">'
        '<h2 class="playlist-heading">Playlist</h2>'
        '<div class="playlist-items">' + ''.join(pieces) + '</div></div>'
    )


def _compute_viable_tags(lower_index, selected_tags):
    """Return set of lowercased tags that can be added without producing zero results."""
    selected_lower = {t.lower() for t in selected_tags} if selected_tags else set()
    viable = set()
    for vtags in lower_index.values():
        if selected_lower <= vtags:
            viable |= vtags
    # Remove already-selected tags from viable set
    viable -= selected_lower
    return viable


def _hx_browse(href):
    """htmx attrs that swap only the browse content (sort/filter without a full
    page reload, preserving scroll), with the href kept as a no-JS fallback."""
    return (
        f'hx-get="{esc(href)}" hx-target="#browse-content" '
        f'hx-select="#browse-content" hx-swap="outerHTML" hx-push-url="true"'
    )


def render_sort_pills(path, view):
    """Render three sort pills (Name/Size/Date). Active pill shows direction arrow;
    clicking the active pill flips direction, clicking an inactive one switches
    to that field at a sensible default direction."""
    fields = [
        ('name', 'Name', 'asc'),
        ('size', 'Size', 'desc'),
        ('date', 'Date', 'desc'),
        ('length', 'Length', 'desc'),
    ]
    pieces = ['<div class="sort-pills">']
    for field, label, default_dir in fields:
        active = field == view.sort
        if active:
            new_dir = 'desc' if view.direction == 'asc' else 'asc'
            arrow = ' ▲' if view.direction == 'asc' else ' ▼'
        else:
            new_dir = default_dir
            arrow = ''
        href = url_for_browse(path, replace(view, sort=field, direction=new_dir))
        cls = 'sort-pill' + (' active' if active else '')
        aria = ' aria-current="true"' if active else ''
        pieces.append(f'<a class="{cls}" href="{esc(href)}" {_hx_browse(href)}{aria}>{label}{arrow}</a>')
    pieces.append('</div>')
    return ''.join(pieces)


def render_tag_manager(rel_path, tags_map):
    """Directory tag-management panel: every tag (by lowercased key) with its
    video count, an inline Rename form, and a Remove button. Returns the
    swappable `<div id="tag-manager">` fragment; the rename/remove routes
    re-render it after each edit. Renaming onto an existing tag merges them;
    Remove drops the tag from every video but KEEPS the videos (unlike
    delete-by-tag)."""
    counts = {}
    for entry in tags_map.values():
        for tag in entry.get('tags', []):
            key = tag.lower().strip()
            if key:
                counts[key] = counts.get(key, 0) + 1

    pieces = ['<div id="tag-manager" class="tag-manager">']
    if not counts:
        pieces.append('<p class="tag-manager-empty">No tags in this directory yet.</p>')
        pieces.append('</div>')
        return ''.join(pieces)

    pieces.append(
        '<p class="tag-manager-hint">Rename merges onto an existing tag; '
        'Remove drops a tag from every video but keeps the videos. '
        'Large edits may warrant re-Training the classifier.</p>'
    )
    path_attr = esc(rel_path)
    for tag, count in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
        tag_attr = esc(tag)
        confirm = esc(
            f"Remove tag '{tag}' from {count} "
            f"video{'' if count == 1 else 's'}? The videos are kept."
        )
        pieces.append(
            f'<div class="tag-manager-row">'
            f'<span class="tag-manager-name">{tag_attr} '
            f'<span class="cnt">({count})</span></span>'
            f'<form class="tag-manager-rename" hx-post="/rename-tag" '
            f'hx-target="#tag-manager" hx-swap="outerHTML">'
            f'<input type="hidden" name="path" value="{path_attr}">'
            f'<input type="hidden" name="old" value="{tag_attr}">'
            f'<input type="text" name="new" value="{tag_attr}" '
            f'aria-label="Rename tag {tag_attr}" required>'
            f'<button type="submit" class="btn">Rename</button>'
            f'</form>'
            f'<form class="tag-manager-remove" hx-post="/remove-tag" '
            f'hx-target="#tag-manager" hx-swap="outerHTML" '
            f'hx-confirm="{confirm}">'
            f'<input type="hidden" name="path" value="{path_attr}">'
            f'<input type="hidden" name="tag" value="{tag_attr}">'
            f'<button type="submit" class="btn-del" '
            f'title="Remove tag from all videos (keeps videos)">'
            f'<span aria-hidden="true">\U0001F5D1</span> Remove</button>'
            f'</form>'
            f'</div>'
        )
    pieces.append('</div>')
    return ''.join(pieces)


def render_tag_filter(tags_map, view, path, filtered_count=None, lower_index=None):
    """Render tag filter: selected pills + searchable dropdown of viable tags."""
    if not tags_map:
        return ''
    # Count all tags
    counts = {}
    for video_data in tags_map.values():
        for tag in video_data.get('tags', []):
            key = tag.lower().strip()
            if key:
                counts[key] = counts.get(key, 0) + 1
    has_starred = any(e.get('starred') for e in tags_map.values())
    if not counts and not has_starred:
        return ''

    # NOTE: pre-existing behavior preserved below — tag/star pill links drop
    # the current sort and reset to the default (date/desc).
    viable = _compute_viable_tags(lower_index or {}, view.tags)

    pieces = ['<div class="tag-filter">']

    # Star filter pill (if any starred videos exist in this directory)
    if has_starred:
        if view.starred:
            href = url_for_browse(path, ViewState(tags=view.tags, starred=False))
            pieces.append(
                f'<a class="tag-pill star-pill active" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Showing starred only — show all videos" '
                f'title="Show all videos">'
                f'★ Starred only <span class="tag-pill-x" aria-hidden="true">×</span></a>'
            )
        else:
            href = url_for_browse(path, ViewState(tags=view.tags, starred=True))
            pieces.append(
                f'<a class="tag-pill star-pill" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Show only starred videos" title="Show only starred videos">★ Starred only</a>'
            )

    # Selected tag pills
    if view.tags:
        pieces.append('<div class="tag-selected-pills">')
        for tag in view.tags:
            remove_tags = tuple(t for t in view.tags if t.lower() != tag.lower())
            href = url_for_browse(path, ViewState(tags=remove_tags, starred=view.starred))
            pieces.append(
                f'<a class="tag-pill active" href="{esc(href)}" {_hx_browse(href)} '
                f'aria-label="Remove filter {esc(tag)}">'
                f'{esc(tag)} <span class="tag-pill-x" aria-hidden="true">\u00d7</span></a>'
            )
        clear_href = url_for_browse(path, ViewState(starred=view.starred))
        pieces.append(
            f'<a class="tag-clear" href="{esc(clear_href)}" {_hx_browse(clear_href)}>Clear all</a>'
        )
        if _config.get('allow_delete') and filtered_count:
            tags_csv = ','.join(view.tags)
            tags_label = ', '.join(view.tags)
            confirm = (
                f'Delete all {filtered_count} video'
                f'{"" if filtered_count == 1 else "s"} tagged "{tags_label}"? '
                'This cannot be undone.'
            )
            pieces.append(
                f'<form class="tag-delete-all" hx-post="/delete-by-tag" '
                f'hx-confirm="{esc(confirm)}" style="display:inline">'
                f'<input type="hidden" name="path" value="{esc(path)}">'
                f'<input type="hidden" name="tags" value="{esc(tags_csv)}">'
                + ('<input type="hidden" name="starred" value="1">' if view.starred else '') +
                f'<button type="submit" class="btn-del" '
                f'title="Delete all videos with these tags">'
                f'<span aria-hidden="true">\U0001F5D1</span> Delete all ({filtered_count})</button>'
                f'</form>'
            )
        pieces.append('</div>')

    # Search input + dropdown
    viable_sorted = sorted(
        [(tag, counts[tag]) for tag in viable if tag in counts],
        key=lambda x: (-x[1], x[0]),
    )

    if viable_sorted:
        pieces.append('<div class="tag-dropdown-wrap">')
        pieces.append(
            '<input type="text" id="tag-search" class="tag-search" '
            'placeholder="Filter by tag\u2026" autocomplete="off" '
            'role="combobox" aria-expanded="false" aria-controls="tag-dropdown" '
            'aria-autocomplete="list" aria-label="Filter videos by tag">'
        )
        pieces.append('<div class="tag-dropdown" id="tag-dropdown" role="listbox">')
        for oi, (tag, count) in enumerate(viable_sorted):
            new_tags = view.tags + (tag,)
            href = url_for_browse(path, ViewState(tags=new_tags, starred=view.starred))
            cnt = f' <span class="cnt">({count})</span>' if count > 1 else ''
            pieces.append(f'<a href="{esc(href)}" {_hx_browse(href)} role="option" id="tagopt-{oi}">{esc(tag)}{cnt}</a>')
        pieces.append('</div></div>')

        # JS: keyboard-operable combobox (filter, arrow/enter/escape, outside-click)
        pieces.append(
            '<script>'
            '(function(){'
            'var s=document.getElementById("tag-search"),'
            'd=document.getElementById("tag-dropdown");'
            'if(!s||s.dataset.bound)return;s.dataset.bound="1";'
            'var items=Array.prototype.slice.call(d.querySelectorAll("a"));'
            'var empty=document.createElement("div");empty.textContent="No matching tags";'
            'empty.setAttribute("role","presentation");'
            'empty.style.cssText="padding:10px 12px;color:#94a3b8;font-size:12px";empty.hidden=true;'
            'd.appendChild(empty);var hi=-1;'
            'function vis(){return items.filter(function(a){return a.style.display!=="none"})}'
            'function paint(){items.forEach(function(a){a.style.background=""});'
            'var v=vis();if(hi>=0&&hi<v.length){v[hi].style.background="#2d2d44";'
            'v[hi].scrollIntoView({block:"nearest"});s.setAttribute("aria-activedescendant",v[hi].id)}'
            'else{s.removeAttribute("aria-activedescendant")}}'
            'function open(){d.classList.add("open");s.setAttribute("aria-expanded","true")}'
            'function close(){d.classList.remove("open");s.setAttribute("aria-expanded","false");hi=-1;paint()}'
            's.addEventListener("focus",open);'
            's.addEventListener("input",function(){'
            'var q=this.value.toLowerCase();'
            'items.forEach(function(a){a.style.display=a.textContent.toLowerCase().includes(q)?"":"none"});'
            'hi=-1;empty.hidden=vis().length>0;open();paint()});'
            's.addEventListener("keydown",function(e){var v=vis();'
            'if(e.key==="ArrowDown"){e.preventDefault();open();hi=Math.min(v.length-1,hi+1);paint()}'
            'else if(e.key==="ArrowUp"){e.preventDefault();hi=Math.max(0,hi-1);paint()}'
            'else if(e.key==="Enter"&&hi>=0&&v[hi]){e.preventDefault();v[hi].click()}'
            'else if(e.key==="Escape"){close();s.blur()}});'
            '})();'
            # Register the outside-click closer ONCE (survives #browse-content swaps)
            'if(!window.__spTagOutside){window.__spTagOutside=1;'
            'document.addEventListener("click",function(e){'
            'if(!e.target.closest(".tag-dropdown-wrap")){'
            'var dd=document.getElementById("tag-dropdown"),ts=document.getElementById("tag-search");'
            'if(dd)dd.classList.remove("open");if(ts)ts.setAttribute("aria-expanded","false")}});}'
            '</script>'
        )

    pieces.append('</div>')
    return ''.join(pieces)


def render_browse_page(data, view, tags_map=None, lower_index=None, thumbs=frozenset(), duration_pending=False):
    title = f'SimpleParty \u2014 {data["path"].split("/")[-1]}' if data['path'] else 'SimpleParty'
    heading = data['path'].split('/')[-1] if data['path'] else 'Library'
    body = render_nav(data['path'], data.get('encryptedDir'))
    body += '<main id="main">'
    body += f'<h1 class="visually-hidden">{esc(heading)}</h1>'
    if not _config.get('has_ffmpeg'):
        body += (
            '<div class="notice" role="status">'
            'ffmpeg not found: video thumbnails and AI tagging are disabled. '
            'Install ffmpeg to enable them.</div>'
        )
    body += '<div id="browse-content">'
    if duration_pending:
        # Self-terminating poller: refreshes #browse-content until the
        # background duration probe finishes, at which point the rendered
        # content no longer includes this element and polling stops.
        poll_url = url_for_browse(data['path'], view)
        body += (
            f'<div class="notice" role="status" hx-get="{esc(poll_url)}" '
            f'hx-trigger="every 4s" hx-target="#browse-content" '
            f'hx-select="#browse-content" hx-swap="outerHTML">'
            f'\u23F3 Calculating video lengths\u2026</div>'
        )
    body += render_tag_filter(
        tags_map, view, data['path'],
        filtered_count=len(data['videos']),
        lower_index=lower_index,
    )
    body += render_file_list(data, view, tags_map=tags_map, thumbs=thumbs)
    body += '</div></main>'
    return render_page(title, body)


def render_locked_page(path, encrypted_dir, redirect_path=None, error=None):
    body = render_nav(path)
    dir_name = encrypted_dir.split('/')[-1] if encrypted_dir else 'directory'
    redir = redirect_path or path
    parent = str(Path(path).parent) if '/' in path else ''
    if parent == '.':
        parent = ''
    body += (
        f'<main id="main"><div class="unlock-box">'
        f'<h1>{icon("lock")} Unlock {esc(dir_name)}</h1>'
        f'<form hx-post="/unlock" hx-target="#unlock-error" hx-swap="innerHTML">'
        f'<input type="hidden" name="path" value="{esc(encrypted_dir)}">'
        f'<input type="hidden" name="redirect" value="{esc(url_for_browse(redir))}">'
        f'<input type="password" name="passphrase" placeholder="Passphrase" '
        f'aria-label="Passphrase" autofocus>'
        f'<div id="unlock-error" class="unlock-error" role="alert">{esc(error) if error else ""}</div>'
        f'<div class="unlock-actions">'
        f'<a class="btn" href="{esc(url_for_browse(parent))}">Cancel</a>'
        f'<button class="btn btn-primary" type="submit">{icon("lock-open")} Unlock</button>'
        f'</div></form></div></main>'
    )
    return render_page('SimpleParty \u2014 Unlock', body)


def render_error_page(path, error):
    body = render_nav(path)
    body += (
        f'<main id="main"><div class="unlock-box error-box" role="alert">'
        f'<h1>{icon("warning")} Something went wrong</h1>'
        f'<p class="error-msg">{esc(error)}</p>'
        f'<div class="error-back"><a class="btn" href="/">\u2190 Back to library</a></div>'
        f'</div></main>'
    )
    return render_page('SimpleParty \u2014 Error', body)


def render_video_tags_inline(rel_path, video_name, tags_list, status='confirmed',
                             suspect_tags=(), scores=None, source=None):
    """Render tag pills with inline add/remove for the video play page.

    `suspect_tags` is a set of lowercased confirmed tags the classifier flagged
    as likely mislabeled; they get a visual badge. The existing remove button is
    the one-click fix.

    For suggested tags, `source` ('model' or 'zero-shot') and `scores`
    (tag -> confidence) are surfaced so the reviewer can judge each suggestion:
    a sharp model hit reads very differently from a weak zero-shot cosine match.
    """
    is_suggested = status == 'suggested'
    suspect_set = {t.lower() for t in suspect_tags}
    scores = scores or {}
    src_icon = icon('wand') if source == 'zero-shot' else icon('tag')
    pieces = ['<div class="video-tag-pills">']

    if is_suggested:
        if source:
            pieces.append(
                f'<span class="suggest-source" title="how these were generated">'
                f'{src_icon} via {esc(source)}</span> ')
        pieces.append(
            '<span class="visually-hidden">Suggested (unconfirmed) tags — accept or reject:</span> '
            f'<form hx-post="/confirm-tags" hx-target="#video-meta" hx-swap="innerHTML" '
            f'style="display:inline;margin:0;padding:0">'
            f'<input type="hidden" name="path" value="{esc(rel_path)}">'
            f'<input type="hidden" name="video" value="{esc(video_name)}">'
            f'<button type="submit" class="btn btn-confirm" title="Accept suggested tags">'
            f'{icon("check")} Accept</button></form> '
            f'<form hx-post="/reject-tags" hx-target="#video-meta" hx-swap="innerHTML" '
            f'style="display:inline;margin:0;padding:0">'
            f'<input type="hidden" name="path" value="{esc(rel_path)}">'
            f'<input type="hidden" name="video" value="{esc(video_name)}">'
            f'<button type="submit" class="btn btn-reject" title="Reject suggested tags">'
            f'{icon("x")} Reject</button></form> '
        )

    pill_class = 'video-tag-pill suggested' if is_suggested else 'video-tag-pill'
    for i, tag in enumerate(tags_list):
        if is_suggested:
            score = scores.get(tag)
            score_html = (f'<span class="tag-score" aria-hidden="true">{score:.2f}</span>'
                          if isinstance(score, (int, float)) else '')
            pieces.append(
                f'<span class="{pill_class}">{esc(tag)}{score_html}'
                f'<form hx-post="/reject-tag" hx-target="#video-meta" hx-swap="innerHTML" '
                f'style="display:inline;margin:0;padding:0">'
                f'<input type="hidden" name="path" value="{esc(rel_path)}">'
                f'<input type="hidden" name="video" value="{esc(video_name)}">'
                f'<input type="hidden" name="tag" value="{esc(tag)}">'
                f'<button type="submit" class="video-tag-remove" title="Reject tag" '
                f'aria-label="Reject suggested tag {esc(tag)}">'
                f'<span aria-hidden="true">\u00d7</span></button>'
                f'</form></span>'
            )
        else:
            remaining = ', '.join(t for j, t in enumerate(tags_list) if j != i)
            is_suspect = tag.lower() in suspect_set
            if is_suspect:
                span_open = (f'<span class="{pill_class} suspect" '
                             f'title="Likely mislabeled — remove if wrong">'
                             + icon('warning', cls='suspect-badge'))
            else:
                span_open = f'<span class="{pill_class}">'
            pieces.append(
                f'{span_open}{esc(tag)}'
                f'<form hx-post="/save-tags" hx-target="#video-meta" hx-swap="innerHTML" '
                f'style="display:inline;margin:0;padding:0">'
                f'<input type="hidden" name="path" value="{esc(rel_path)}">'
                f'<input type="hidden" name="video" value="{esc(video_name)}">'
                f'<input type="hidden" name="tags" value="{esc(remaining)}">'
                f'<button type="submit" class="video-tag-remove" title="Remove tag" '
                f'aria-label="Remove tag {esc(tag)}">'
                f'<span aria-hidden="true">\u00d7</span></button>'
                f'</form></span>'
            )
    # Inline add input
    all_csv = ', '.join(tags_list)
    prefix = (all_csv + ', ') if all_csv else ''
    pieces.append(
        f'<form hx-post="/save-tags" hx-target="#video-meta" hx-swap="innerHTML" '
        f'style="display:inline;margin:0;padding:0" data-prefix="{esc(prefix)}" '
        f'onsubmit="var f=this,i=f.querySelector(&quot;.video-tag-add&quot;);'
        f'f.querySelector(&quot;[name=tags]&quot;).value=f.dataset.prefix+i.value;return true">'
        f'<input type="hidden" name="path" value="{esc(rel_path)}">'
        f'<input type="hidden" name="video" value="{esc(video_name)}">'
        f'<input type="hidden" name="tags" value="">'
        f'<input type="text" class="video-tag-add" placeholder="add tag\u2026" aria-label="Add a tag">'
        f'</form>'
    )
    pieces.append('</div>')
    return ''.join(pieces)


def render_play_page(data, idx, next_url, prev_url, shuffle_url, is_shuffled, pos_info, view=None, tags_map=None, lower_index=None, thumbs=frozenset(), play_order=None, shuffle_seed=None, transcode_plan=None):
    v = data['videos'][idx]
    video_src = url_for_video(v['path'])
    view = view if view is not None else ViewState(sort='name', direction='asc')
    browse_url = url_for_browse(data['path'], view)

    body = render_nav(data['path'], data.get('encryptedDir'))
    body += '<main id="main">'
    if transcode_plan == 'reencode':
        body += (
            '<div id="transcode-notice" role="status">'
            f'<span>{icon("gear")} Re-encoding this video in real time (source codec not '
            'supported by your browser); start-up and seeking may be slower.</span>'
            '<button type="button" class="tn-close" aria-label="Dismiss notice" '
            'onclick="this.parentNode.remove()">\u00d7</button>'
            '</div>'
        )
    body += (
        f'<div id="player-area">'
        f'<video id="video" src="{esc(video_src)}" controls playsinline autoplay></video>'
        f'<div id="video-overlay" role="status" aria-live="polite"></div>'
        f'<h1 id="video-title">{esc(v["name"])}</h1>'
        f'</div>'
        f'<div id="controls">'
        f'<div class="tier tier-transport">'
        f'<a class="btn" href="{esc(prev_url)}" title="Previous (p)">{icon("prev")} Prev</a>'
        f'<a class="btn" href="{esc(next_url)}" title="Next (n)">Next {icon("next")}</a>'
        f'</div>'
        f'<div class="tier-sep"></div>'
        f'<div class="tier">'
        f'<div class="skip-group">'
        f'<button class="btn btn-skip" onclick="skip(-30)" title="Back 30s (J)">-30s</button>'
        f'<button class="btn btn-skip" onclick="skip(-10)" title="Back 10s (j)">-10s</button>'
        f'<button class="btn btn-skip" onclick="skip(10)" title="Forward 10s (l)">+10s</button>'
        f'<button class="btn btn-skip" onclick="skip(30)" title="Forward 30s (L)">+30s</button>'
        f'</div>'
        f'<select id="speed-select" class="speed-select" onchange="setSpeed(this.value)" aria-label="Playback speed" title="Speed (&lt; &gt;)">'
        f'<option value="0.5">0.5x</option>'
        f'<option value="0.75">0.75x</option>'
        f'<option value="1" selected>1x</option>'
        f'<option value="1.25">1.25x</option>'
        f'<option value="1.5">1.5x</option>'
        f'<option value="2">2x</option>'
        f'<option value="3">3x</option>'
        f'</select>'
        f'</div>'
        f'<span id="now-playing">{pos_info}</span>'
        f'<div class="spacer"></div>'
        f'<div class="tier">'
        f'<a class="btn btn-toggle{" active" if is_shuffled else ""}" '
        f'href="{esc(shuffle_url)}" title="Shuffle (s)">{icon("shuffle")} Shuffle</a>'
        f'<button id="btn-autoplay" class="btn btn-toggle" title="Autoplay (a)" aria-pressed="false">Autoplay</button>'
        f'<button id="btn-repeat" class="btn btn-toggle" title="Repeat (r)" aria-pressed="false">Repeat</button>'
    )
    if _config['allow_tag']:
        is_video_starred = bool(tags_map and tags_map.get(v['name'], {}).get('starred'))
        body += (
            f'<button id="btn-star" type="button" class="btn btn-star{" active" if is_video_starred else ""}" '
            f'data-starred="{"1" if is_video_starred else "0"}" '
            f'aria-pressed="{"true" if is_video_starred else "false"}" '
            f'aria-label="Star this video" '
            f'data-dir="{esc(data["path"])}" data-video="{esc(v["name"])}" '
            f'title="Star this video">'
            f'<span class="star-icon" aria-hidden="true">{icon("star-outline")}</span></button>'
        )
    if _config['allow_delete']:
        body += (
            f'<form id="delete-form" hx-post="/delete" hx-confirm="Delete {esc(v["name"])}?">'
            f'<input type="hidden" name="path" value="{esc(v["path"])}">'
            f'<input type="hidden" name="redirect" value="{esc(browse_url)}">'
            f'<button type="submit" class="btn btn-danger" title="Delete (d)" '
            f'aria-label="Delete {esc(v["name"])}">'
            f'{icon("trash")}</button></form>'
        )
    body += '</div></div>'

    if _config['allow_tag']:
        video_entry = tags_map.get(v['name'], {}) if tags_map else {}
        video_tags = video_entry.get('tags', [])
        video_status = video_entry.get('status', 'confirmed')
        suspect_tags = ()
        if video_status not in ('suggested', 'rejected'):
            from simpleparty.classifier import load_suspects
            resolved_dir = resolve_path(_config.get('root', '.'), data['path'])
            flagged = load_suspects(str(resolved_dir)).get(v['name'], [])
            suspect_tags = [s['tag'] for s in flagged if s.get('given') == 1]
        meta_html = render_video_tags_inline(
            data['path'], v['name'], video_tags, status=video_status,
            suspect_tags=suspect_tags,
            scores=video_entry.get('suggest_scores'),
            source=video_entry.get('suggest_source'))
        from simpleparty.embeddings import video_is_embedded
        resolved_dir = resolve_path(_config.get('root', '.'), data['path'])
        if video_is_embedded(str(resolved_dir), v['name']):
            if not video_tags or video_status in ('suggested', 'rejected'):
                # Show the button only when something can actually produce a
                # suggestion: a trained head, or — for the zero-shot fallback — at
                # least one confirmed tag in this directory to use as a label. The
                # label names which path will run so the result isn't a surprise.
                from simpleparty.tagger import model_path as _model_path
                has_model = _model_path(resolved_dir).exists()
                has_vocab = bool(tags_map) and any(
                    e.get('tags') and e.get('status', 'confirmed') != 'suggested'
                    for e in tags_map.values())
                if has_model or has_vocab:
                    mode = 'model' if has_model else 'zero-shot'
                    meta_html += (
                        f'<form hx-post="/suggest-one" hx-target="#video-meta" '
                        f'hx-swap="innerHTML" style="display:inline">'
                        f'<input type="hidden" name="path" value="{esc(data["path"])}">'
                        f'<input type="hidden" name="video" value="{esc(v["name"])}">'
                        f'<button class="btn">{icon("tag")} Suggest ({mode})</button>'
                        f'</form>'
                    )
        elif _config['has_ffmpeg']:
            # Suggest needs an embedding first; embedding is the explicit step.
            self_url = url_for_play(data['path'], idx, view, video=v['name'])
            job = jobs.get_tag_job(str(resolved_dir))
            if job and job.get('running'):
                # Poll this page's #video-meta until the embedding lands, then
                # the re-fetched fragment (now Suggest, no poller) self-terminates.
                meta_html += (
                    f'<span class="embed-pending">\U0001F9E0 Embedding this video…</span>'
                    f'<span hx-get="{esc(self_url)}" hx-trigger="every 2s" '
                    f'hx-select="#video-meta" hx-target="#video-meta" '
                    f'hx-swap="outerHTML" style="display:none"></span>'
                )
            else:
                meta_html += (
                    f'<form hx-post="/embed" hx-swap="none" style="display:inline">'
                    f'<input type="hidden" name="path" value="{esc(data["path"])}">'
                    f'<input type="hidden" name="video" value="{esc(v["name"])}">'
                    f'<input type="hidden" name="redirect" value="{esc(self_url)}">'
                    f'<button class="btn btn-embed">{icon("embed")} Embed this video</button>'
                    f'</form>'
                )
        body += f'<div class="video-meta" id="video-meta">{meta_html}</div>'

    if tags_map:
        body += render_related_videos(data, idx, lower_index, view, thumbs)

    body += render_playlist(data, idx, play_order, shuffle_seed, view, thumbs)
    body += '</main>'

    sp_data = json.dumps({'next': next_url, 'prev': prev_url, 'browse': browse_url})
    body += (
        f'<script>const SP={sp_data};</script>\n'
        f'<script src="/static/play.js?v={__version__}"></script>'
    )

    return render_page(f'SimpleParty \u2014 {v["name"]}', body)



def render_download_form(target_rel='', *, autofocus=False):
    rel = esc(target_rel)
    af = ' autofocus' if autofocus else ''
    return (
        f'<form hx-post="/download" class="download-form">'
        f'<input type="hidden" name="path" value="{rel}">'
        f'<input type="url" name="url" placeholder="https://… (paste a URL)" aria-label="Download URL" required{af}>'
        f'<button type="submit" class="btn btn-primary">{icon("download")} Queue</button>'
        f'</form>'
    )


def _render_bytes(n):
    if not n:
        return ''
    return fmt_size(n)


def _render_speed(s):
    if not s:
        return ''
    return fmt_size(int(s)) + '/s'


def _render_eta(eta):
    if eta is None or eta < 0:
        return ''
    if eta < 60:
        return f'{int(eta)}s'
    m, s = divmod(int(eta), 60)
    if m < 60:
        return f'{m}m {s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h {m:02d}m'


def _render_download_job_card(job, *, full=True):
    state = job.get('state', 'queued')
    title = job.get('title') or Path(job.get('filename') or '').name or job['url']
    err = ''
    if job.get('error'):
        err = f'<div class="tag-error">{icon("x")} {esc(job["error"])}</div>'
    card_cls = 'download-card err' if state == 'error' else 'download-card'

    bar = ''
    meta = ''
    if state == 'running':
        pct = job.get('percent', 0)
        bar = (
            f'<div class="tag-progress-bar-wrap">'
            f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
            f'</div>'
        )
        line1 = [job.get('phase', 'downloading')]
        total = job.get('total_bytes', 0)
        done = job.get('downloaded_bytes', 0)
        if total:
            line1.append(f'{_render_bytes(done)} / {_render_bytes(total)} ({pct}%)')
        elif done:
            line1.append(_render_bytes(done))
        line2 = []
        sp = _render_speed(job.get('speed'))
        if sp:
            line2.append(sp)
        eta = _render_eta(job.get('eta'))
        if eta:
            line2.append('ETA ' + eta)
        # Two short rows so the progress doesn't wrap unpredictably on a 360px card
        meta = f'<div class="meta">{esc(" · ".join(line1))}</div>'
        if line2:
            meta += f'<div class="meta">{esc(" · ".join(line2))}</div>'
    elif state == 'done':
        meta_bits = ['Done']
        final_name = job.get('final_name')
        if final_name:
            meta_bits.append(final_name)
        links = ''
        if job.get('play_dir') is not None and job.get('play_name'):
            play_url = url_for_play(job['play_dir'], 0, video=job['play_name'])
            links += f' <a class="btn" href="{esc(play_url)}">{icon("play")} Play</a>'
            browse_url = url_for_browse(job['play_dir'])
            links += f' <a class="btn" href="{esc(browse_url)}">{icon("folder")} Folder</a>'
        meta = (
            f'<div class="meta"><span class="tag-done">{icon("check")} '
            f'{esc(" · ".join(meta_bits))}</span>{links}</div>'
        )
    elif state == 'cancelled':
        meta = '<div class="meta">Cancelled</div>'
    elif state == 'queued':
        meta = '<div class="meta">Queued</div>'

    cancel = ''
    if full and state in ('queued', 'running'):
        cancel = (
            f'<form hx-post="/download-cancel" class="dl-cancel-form">'
            f'<input type="hidden" name="id" value="{esc(job["id"])}">'
            f'<button class="btn">Cancel</button>'
            f'</form>'
        )

    target_link = ''
    if full:
        browse_url = url_for_browse(job.get('target_rel', ''))
        tlabel = job.get('target_rel') or '/'
        target_link = (
            f'<span class="meta">→ '
            f'<a class="crumb" href="{esc(browse_url)}">{esc(tlabel)}</a></span>'
        )

    return (
        f'<div class="{card_cls}">'
        f'<div class="row">'
        f'<span class="title">{esc(title)}</span>'
        f'{target_link}'
        f'{cancel}'
        f'</div>'
        f'<div class="row">'
        f'<span class="url">{esc(job["url"])}</span>'
        f'</div>'
        f'{bar}'
        f'{meta}'
        f'{err}'
        f'</div>'
    )


def render_download_status(path_filter=None):
    """Returns the status fragment — self-polling element.

    `path_filter` (rel path) scopes to jobs targeting that directory, for the
    inline panel on the browse page. Without it, returns the full board for
    the dedicated page.
    """
    order, jobs_map = jobs.snapshot_download_jobs()
    running = jobs.any_download_running(jobs_map)
    poll = 'every 1s' if running else 'every 10s'

    if path_filter is not None:
        scoped = [jobs_map[jid] for jid in order
                  if jobs_map[jid].get('target_rel') == path_filter]
        active = [j for j in scoped if j.get('state') in ('queued', 'running')]
        inner = ''
        if active:
            parts = []
            for j in active:
                if j.get('state') == 'running':
                    pct = j.get('percent', 0)
                    title = j.get('title') or Path(j.get('filename') or '').name or j['url']
                    parts.append(
                        f'<span class="tag-progress-phase">{icon("download")} {esc(title[:60])}</span>'
                        f'<div class="tag-progress-bar-wrap">'
                        f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
                        f'</div>'
                        f'<span class="tag-progress-text">{pct}%</span>'
                    )
                else:
                    parts.append(f'<span class="tag-progress-text">{icon("download")} queued</span>')
            inner = ''.join(parts) + ' <a class="btn" href="/download">Manage</a>'
        return (
            f'<div hx-get="/download-status?{urllib.parse.urlencode({"path": path_filter, "inline": "1"})}" '
            f'hx-trigger="{poll}" hx-swap="outerHTML" '
            f'role="status" aria-live="polite" '
            f'class="download-progress-panel" id="download-progress">{inner}</div>'
        )

    # Full board
    active = [jobs_map[jid] for jid in order if jobs_map[jid].get('state') == 'running']
    queued = [jobs_map[jid] for jid in order if jobs_map[jid].get('state') == 'queued']
    finished = [jobs_map[jid] for jid in order
                if jobs_map[jid].get('state') in ('done', 'error', 'cancelled')]
    finished.reverse()  # most recent first

    pieces = []
    if active:
        pieces.append('<div class="download-section-title">Now downloading</div>')
        for j in active:
            pieces.append(_render_download_job_card(j))
    if queued:
        pieces.append('<div class="download-section-title">Queued</div>')
        for j in queued:
            pieces.append(_render_download_job_card(j))
    if finished:
        pieces.append(
            '<div class="download-section-title">Recent '
            '<form hx-post="/download-clear" class="dl-section-form">'
            '<button class="btn">Clear completed</button></form></div>'
        )
        for j in finished:
            pieces.append(_render_download_job_card(j))
    if not pieces:
        pieces.append('<div class="empty">No downloads yet.</div>')

    return (
        f'<div hx-get="/download-status" hx-trigger="{poll}" '
        f'hx-swap="outerHTML" class="download-board" id="download-board" role="status" aria-live="polite">'
        f'{"".join(pieces)}</div>'
    )


def render_download_page(target_rel=''):
    nav = render_nav('')
    hint = (
        '<div class="download-hint">'
        'Paste a URL. Downloads land in the chosen directory '
        '(default: server root). One at a time.'
        '</div>'
    )
    full_form = (
        f'<form hx-post="/download" class="download-form download-form-page">'
        f'<input type="url" name="url" class="download-url-input" placeholder="https://…" aria-label="Download URL" required autofocus>'
        f'<input type="text" name="path" class="download-path-input" placeholder="subdir/ (blank = root)" aria-label="Target subdirectory" value="{esc(target_rel)}">'
        f'<button type="submit" class="btn btn-primary">{icon("download")} Queue</button>'
        f'</form>'
    )
    board = render_download_status(path_filter=None)
    body = (
        nav + '<main id="main"><h1 class="visually-hidden">Downloads</h1>'
        + hint + full_form + board + '</main>'
    )
    return render_page('Downloads — SimpleParty', body)
