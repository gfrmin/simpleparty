"""HTTP route handlers and response helpers."""

import importlib.resources
import logging
import os
import random
import re
import threading
import time
import urllib.parse
import uuid
from html import escape as esc
from pathlib import Path

from simpleparty import jobs
from simpleparty.library import (
    _populate_durations,
    filter_videos_by_starred,
    filter_videos_by_tags,
    find_video_idx,
    fscrypt_lock,
    fscrypt_unlock,
    is_video,
    list_directory,
    resolve_path,
    shuffle_indices,
    sort_videos,
)
from simpleparty.media import (
    _is_mpegts,
    _maybe_start_thumbs,
    _probe_streams,
    _remux_mpegts,
    _serve_transcoded,
    _stream_file,
    _stream_range,
    _transcode_plan,
)
from simpleparty.render import (
    _render_train_btn,
    render_browse_page,
    render_download_page,
    render_download_status,
    render_error_page,
    render_locked_page,
    render_play_page,
    render_video_tags_inline,
)
from simpleparty.state import CONFIG as _config, BROWSER_NATIVE, MIME_TYPES
from simpleparty.urls import (
    ViewState,
    parse_query,
    safe_int,
    url_for_browse,
    url_for_play,
)

logger = logging.getLogger('simpleparty.routes')


# --- HTTP helpers ---

def send_html(handler, content, status=200):
    body = content.encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type', 'text/html; charset=utf-8')
    handler.send_header('Content-Length', str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def send_redirect(handler, url):
    handler.send_response(302)
    handler.send_header('Location', url)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def send_hx_redirect(handler, url):
    handler.send_response(200)
    handler.send_header('HX-Redirect', url)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def read_form_body(handler):
    length = int(handler.headers.get('Content-Length', 0))
    body = handler.rfile.read(length).decode('utf-8')
    params = urllib.parse.parse_qs(body)
    return {k: v[0] for k, v in params.items()}



# --- Route handlers ---

def get_dir_context(root, rel_path, params):
    """One-stop per-request snapshot of a directory.

    Returns {'data': ...} when listing failed/locked; otherwise also
    resolved path, ViewState, tags (+ lowercased index), and the set of
    existing thumbnails — each computed exactly once per request.
    """
    data = list_directory(root, rel_path)
    if data.get('locked') or 'error' in data:
        return {'data': data}
    view = ViewState.from_params(params)
    resolved = resolve_path(root, rel_path)
    tags_map = None
    lower_index = None
    if _config['allow_tag']:
        from simpleparty.tagger import list_thumbs, load_tags_index
        tags_map, lower_index = load_tags_index(resolved)
        videos = filter_videos_by_tags(data['videos'], lower_index, view.tags)
        data['videos'] = filter_videos_by_starred(videos, tags_map, view.starred)
        thumbs = list_thumbs(resolved)
    else:
        from simpleparty.tagger import list_thumbs
        thumbs = list_thumbs(resolved)
    return {
        'data': data, 'resolved': resolved, 'view': view,
        'tags_map': tags_map, 'lower_index': lower_index, 'thumbs': thumbs,
    }


def handle_browse(handler, root):
    params = parse_query(handler.path)
    rel_path = params.get('path', '')
    ctx = get_dir_context(root, rel_path, params)
    data = ctx['data']
    if data.get('locked'):
        send_html(handler, render_locked_page(rel_path, data['encryptedDir']))
        return
    if 'error' in data:
        status = 404 if data['error'] == 'Not found' else 400
        send_html(handler, render_error_page(rel_path, data['error']), status)
        return
    view, resolved, tags_map = ctx['view'], ctx['resolved'], ctx['tags_map']
    if view.sort == 'length':
        if tags_map:
            tags_map = {k: dict(v) for k, v in tags_map.items()}
        _populate_durations(root, data['videos'], tags_map, resolved)
    data['videos'] = sort_videos(data['videos'], view.sort, view.direction)
    from simpleparty.tagger import videos_with_frames
    _maybe_start_thumbs(resolved, data['videos'],
                        thumbs=ctx['thumbs'], frames=videos_with_frames(resolved))
    send_html(handler, render_browse_page(
        data, view, tags_map=tags_map,
        lower_index=ctx['lower_index'], thumbs=ctx['thumbs'],
    ))


def handle_play(handler, root):
    params = parse_query(handler.path)
    dir_path = params.get('path', '')
    ctx = get_dir_context(root, dir_path, params)
    data = ctx['data']

    if data.get('locked'):
        send_html(handler, render_locked_page(dir_path, data['encryptedDir']))
        return

    view = ctx.get('view') or ViewState.from_params(params)
    tags_map = ctx.get('tags_map')
    resolved = ctx.get('resolved')
    if view.sort == 'length' and resolved is not None:
        if tags_map:
            tags_map = {k: dict(v) for k, v in tags_map.items()}
        _populate_durations(root, data['videos'], tags_map, resolved)
    if 'videos' in data:
        data['videos'] = sort_videos(data['videos'], view.sort, view.direction)

    if 'error' in data or not data.get('videos'):
        send_redirect(handler, url_for_browse(dir_path, view))
        return

    n = len(data['videos'])
    shuffled = params.get('shuffle') == '1'
    video_name = params.get('video')

    play_order = None
    shuffle_seed = None
    if shuffled:
        seed = safe_int(params.get('seed'), random.randint(0, 2**31))
        pos = safe_int(params.get('pos')) % n
        order = shuffle_indices(n, seed)
        idx = order[pos]
        # If a video name was provided and doesn't match, find it by name
        # (handles stale URLs after deletion)
        if video_name and data['videos'][idx]['name'] != video_name:
            found = find_video_idx(data['videos'], video_name)
            if found is not None:
                idx = found
                pos = order.index(idx) if idx in order else pos
        next_pos = (pos + 1) % n
        prev_pos = (pos - 1) % n
        next_url = url_for_play(dir_path, order[next_pos], view, shuffle=True, seed=seed, pos=next_pos, video=data['videos'][order[next_pos]]['name'])
        prev_url = url_for_play(dir_path, order[prev_pos], view, shuffle=True, seed=seed, pos=prev_pos, video=data['videos'][order[prev_pos]]['name'])
        pos_info = f'{pos + 1}/{n}'
        shuffle_url = url_for_play(dir_path, idx, view, video=data['videos'][idx]['name'])
        play_order = order
        shuffle_seed = seed
    else:
        idx_param = safe_int(params.get('idx'))
        # Prefer video name lookup (stable across deletions)
        found = find_video_idx(data['videos'], video_name) if video_name else None
        idx = found if found is not None else max(0, min(idx_param, n - 1))
        next_idx = (idx + 1) % n
        prev_idx = (idx - 1) % n
        next_url = url_for_play(dir_path, next_idx, view, video=data['videos'][next_idx]['name'])
        prev_url = url_for_play(dir_path, prev_idx, view, video=data['videos'][prev_idx]['name'])
        pos_info = f'{idx + 1}/{n}'
        shuffle_params = {'path': dir_path, 'shuffle': '1'}
        if view.tags:
            shuffle_params['tags'] = ','.join(view.tags)
        if view.sort and view.sort != 'name':
            shuffle_params['sort'] = view.sort
        if view.direction and view.direction != 'asc':
            shuffle_params['dir'] = view.direction
        if view.starred:
            shuffle_params['starred'] = '1'
        shuffle_url = '/play?' + urllib.parse.urlencode(shuffle_params)

    transcode_plan = None
    if _config['allow_transcode'] and (_config['has_ffmpeg'] or _config['has_vlc']):
        try:
            video_fs_path = resolve_path(root, data['videos'][idx]['path'])
            if video_fs_path.is_file():
                transcode_plan = _transcode_plan(video_fs_path) if _config['has_ffmpeg'] else (
                    None if video_fs_path.suffix.lower() in BROWSER_NATIVE else 'reencode'
                )
        except OSError:
            pass

    send_html(handler, render_play_page(data, idx, next_url, prev_url, shuffle_url, shuffled, pos_info, view, tags_map=tags_map, lower_index=ctx.get('lower_index'), thumbs=ctx.get('thumbs', frozenset()), play_order=play_order, shuffle_seed=shuffle_seed, transcode_plan=transcode_plan))


def handle_video(handler, root):
    parsed = urllib.parse.urlparse(handler.path)
    rel_path = urllib.parse.unquote(parsed.path[len('/video/'):])
    resolved = resolve_path(root, rel_path)

    if not resolved.is_file():
        handler.send_error(404)
        return

    if _config['allow_transcode'] and (_config['has_ffmpeg'] or _config['has_vlc']):
        plan = _transcode_plan(resolved) if _config['has_ffmpeg'] else (
            None if resolved.suffix.lower() in BROWSER_NATIVE else 'reencode'
        )
        if plan is not None:
            if _is_mpegts(resolved) and _config['has_ffmpeg'] and _remux_mpegts(resolved):
                pass  # file is now a proper MP4, fall through to normal serving
            else:
                vcodec, acodec = _probe_streams(resolved) if _config['has_ffmpeg'] else (None, None)
                logger.info('transcode plan=%s v=%s a=%s file=%s', plan, vcodec, acodec, resolved)
                _serve_transcoded(handler, resolved, plan)
                return

    file_size = resolved.stat().st_size
    content_type = MIME_TYPES.get(resolved.suffix.lower(), 'application/octet-stream')
    range_header = handler.headers.get('Range')

    if range_header:
        match = re.match(r'bytes=(\d+)-(\d*)', range_header)
        if match:
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else file_size - 1
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                handler.send_response(416)
                handler.send_header('Content-Range', f'bytes */{file_size}')
                handler.end_headers()
                return
            length = end - start + 1
            handler.send_response(206)
            handler.send_header('Content-Type', content_type)
            handler.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
            handler.send_header('Content-Length', str(length))
            handler.send_header('Accept-Ranges', 'bytes')
            handler.end_headers()
            if handler.command != 'HEAD':
                _stream_range(handler, resolved, start, length)
            return

    handler.send_response(200)
    handler.send_header('Content-Type', content_type)
    handler.send_header('Content-Length', str(file_size))
    handler.send_header('Accept-Ranges', 'bytes')
    handler.end_headers()
    if handler.command != 'HEAD':
        _stream_file(handler, resolved)


def handle_delete(handler, root):
    if not _config['allow_delete']:
        handler.send_error(403, 'Delete disabled')
        return
    form = read_form_body(handler)
    rel_path = form.get('path', '')
    redirect_url = form.get('redirect')
    resolved = resolve_path(root, rel_path)
    if not resolved.is_file() or not is_video(resolved.name):
        handler.send_error(400, 'Invalid video path')
        return
    try:
        os.remove(resolved)
    except OSError as e:
        handler.send_error(500, str(e))
        return
    # Clean up tags entry for deleted video
    if _config['allow_tag']:
        try:
            from simpleparty.tagger import load_tags, update_tags
            dir_path = resolved.parent
            if resolved.name in load_tags(dir_path):
                update_tags(dir_path, lambda t: {
                    k: v for k, v in t.items() if k != resolved.name
                })
        except Exception:
            pass  # best-effort cleanup
    if redirect_url:
        send_hx_redirect(handler, redirect_url)
    else:
        handler.send_response(200)
        handler.send_header('Content-Length', '0')
        handler.end_headers()


def handle_delete_by_tag(handler, root):
    if not _config['allow_delete']:
        handler.send_error(403, 'Delete disabled')
        return
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    form = read_form_body(handler)
    rel_path = form.get('path', '')
    raw_tags = form.get('tags', '')
    selected_tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    if not selected_tags:
        handler.send_error(400, 'No tags specified')
        return
    resolved_dir = resolve_path(root, rel_path)
    if not resolved_dir.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    data = list_directory(root, rel_path)
    if 'error' in data or data.get('locked'):
        handler.send_error(400, 'Cannot list directory')
        return

    from simpleparty.tagger import load_tags_index, update_tags
    tags_map, lower_index = load_tags_index(resolved_dir)
    targets = filter_videos_by_tags(data['videos'], lower_index, selected_tags)

    removed = set()
    for video in targets:
        video_path = resolved_dir / video['name']
        try:
            os.remove(video_path)
        except OSError as e:
            logger.warning('delete-by-tag: failed to remove %s: %s', video_path, e)
            continue
        removed.add(video['name'])

    try:
        update_tags(resolved_dir, lambda t: {
            k: v for k, v in t.items() if k not in removed
        })
    except OSError as e:
        logger.warning('delete-by-tag: failed to save tags for %s: %s', resolved_dir, e)

    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_unlock(handler, root):
    form = read_form_body(handler)
    encrypted_path = form.get('path', '')
    passphrase = form.get('passphrase', '')
    redirect_url = form.get('redirect', url_for_browse(encrypted_path))
    resolved = resolve_path(root, encrypted_path)
    ok, msg = fscrypt_unlock(resolved, passphrase)
    del passphrase
    if ok:
        send_hx_redirect(handler, redirect_url)
    else:
        send_html(handler, esc(msg or 'Unlock failed'))


def handle_lock(handler, root):
    form = read_form_body(handler)
    path = form.get('path', '')
    redirect_url = form.get('redirect', url_for_browse(''))
    resolved = resolve_path(root, path)
    fscrypt_lock(resolved)
    send_hx_redirect(handler, redirect_url)


def handle_train(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import train

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    max_frames = int(form.get('frames', '1'))
    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    resolved_str = str(resolved)
    existing = jobs.get_tag_job(resolved_str)
    if existing and existing.get('running'):
        send_hx_redirect(handler, url_for_browse(rel_path))
        return

    progress = {'running': True, 'done': 0, 'total': 0, 'current': '', 'phase': 'preparing'}
    jobs.set_tag_job(resolved_str, progress)

    t = threading.Thread(
        target=train,
        args=(resolved_str,),
        kwargs={'max_frames': max_frames, 'progress': progress},
        daemon=True,
    )
    t.start()
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_suggest(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import suggest_for_directory
    from simpleparty.tagger import model_path as _model_path

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir():
        handler.send_error(400, 'Not a directory')
        return

    mp = _model_path(resolved)
    model_path = str(mp)
    if not mp.exists():
        handler.send_error(400, 'No trained model found. Train first.')
        return

    resolved_str = str(resolved)
    existing = jobs.get_tag_job(resolved_str)
    if existing and existing.get('running'):
        send_hx_redirect(handler, url_for_browse(rel_path))
        return

    progress = {'running': True, 'done': 0, 'total': 0, 'current': '', 'phase': 'suggesting'}
    jobs.set_tag_job(resolved_str, progress)

    t = threading.Thread(
        target=suggest_for_directory,
        args=(resolved_str, model_path),
        kwargs={'progress': progress, 'max_tags': _config['max_tags']},
        daemon=True,
    )
    t.start()
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_suggest_one(handler, root):
    """Suggest tags for a single video and return updated tag HTML."""
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.classifier import suggest_for_video
    from simpleparty.tagger import update_tags, model_path as _model_path

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    mp = _model_path(resolved)
    if not mp.exists():
        handler.send_error(400, 'No trained model found. Train first.')
        return

    video_path = resolved / video_name
    if not video_path.exists():
        handler.send_error(404, 'Video not found')
        return

    results = suggest_for_video(str(video_path), str(mp), max_tags=_config['max_tags'])
    if results:
        avg_conf = sum(c for _, c in results) / len(results)
        update_tags(resolved, lambda tags: {**tags, video_name: {
            **tags.get(video_name, {}),
            'tags': [tag for tag, _ in results],
            'status': 'suggested',
            'confidence': round(avg_conf, 3),
        }})
        send_html(handler, render_video_tags_inline(
            rel_path, video_name,
            [tag for tag, _ in results],
            status='suggested',
        ))
    else:
        send_html(handler, render_video_tags_inline(rel_path, video_name, []))


def handle_confirm_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import update_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    updated = update_tags(resolved, lambda tags: {**tags, video_name: {
        **tags.get(video_name, {}), 'status': 'confirmed', 'confirmed_at': now,
    }})

    tags_list = updated.get(video_name, {}).get('tags', [])
    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list, status='confirmed'))


def handle_confirm_all(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import update_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir():
        handler.send_error(400, 'Invalid request')
        return

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    def _confirm_all(tags):
        for entry in tags.values():
            if entry.get('status') == 'suggested':
                entry['status'] = 'confirmed'
                entry['confirmed_at'] = now
        return tags

    update_tags(resolved, _confirm_all)
    send_hx_redirect(handler, url_for_browse(rel_path))


def handle_reject_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import load_tags, update_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    if video_name in load_tags(resolved):
        update_tags(resolved, lambda tags: {**tags, video_name: {
            **tags.get(video_name, {}),
            'rejected_tags': (tags.get(video_name, {}).get('rejected_tags', [])
                              + tags.get(video_name, {}).get('tags', [])),
            'tags': [],
            'status': 'rejected',
        }})

    send_html(handler, render_video_tags_inline(rel_path, video_name, []))


def handle_reject_tag(handler, root):
    """Reject a single suggested tag, keeping remaining suggestions."""
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import update_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    tag = form.get('tag', '')
    resolved = resolve_path(root, rel_path)

    if not resolved.is_dir() or not video_name or not tag:
        handler.send_error(400, 'Invalid request')
        return

    def _reject_one(tags):
        entry = tags.get(video_name, {})
        tags_list = list(entry.get('tags', []))
        if tag in tags_list:
            tags_list.remove(tag)
            entry['tags'] = tags_list
            entry['rejected_tags'] = list(entry.get('rejected_tags', [])) + [tag]
        if not tags_list:
            entry['status'] = 'rejected'
        tags[video_name] = entry
        return tags

    updated = update_tags(resolved, _reject_one)
    entry = updated.get(video_name, {})
    tags_list = entry.get('tags', [])
    status = 'rejected' if not tags_list else entry.get('status', 'confirmed')

    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list, status=status))


def handle_tag_status(handler, root):
    if not _config['allow_tag']:
        send_html(handler, '')
        return
    params = parse_query(handler.path)
    rel_path = params.get('path', '')
    resolved = str(resolve_path(root, rel_path))
    status_url = f'/tag-status?{urllib.parse.urlencode({"path": rel_path})}'
    path_param = esc(rel_path)

    progress = jobs.get_tag_job(resolved)

    # OOB swap to keep train button in sync
    def train_btn_oob(busy):
        btn = _render_train_btn(path_param, busy)
        return btn.replace('id="train-form"', 'id="train-form" hx-swap-oob="true"', 1)

    def wrap(inner, poll=None, active=False):
        if poll:
            return (
                f'<div hx-get="{status_url}" hx-trigger="{poll}" '
                f'hx-swap="outerHTML" class="tag-progress-panel'
                f'{" active" if active else ""}" role="status" aria-live="polite" id="tag-progress">'
                f'{inner}</div>'
            )
        return f'<div class="tag-progress-panel" id="tag-progress" role="status" aria-live="polite">{inner}</div>'

    if not progress:
        send_html(handler, wrap('', poll='every 10s'))
        return

    if progress.get('error'):
        send_html(handler,
            wrap(f'<span class="tag-error">\u274C {esc(progress["error"])}</span>')
            + train_btn_oob(False))
        return

    if not progress.get('running'):
        if progress.get('phase') == 'done':
            msg = esc(progress.get('current', 'Done'))
            suggest = (
                f'<form hx-post="/suggest" style="display:inline">'
                f'<input type="hidden" name="path" value="{path_param}">'
                f'<button class="btn">\U0001F3F7 Suggest tags</button>'
                f'</form>'
            )
            send_html(handler,
                wrap(f'<span class="tag-done">\u2705 {msg} {suggest}</span>')
                + train_btn_oob(False))
        else:
            send_html(handler, wrap('', poll='every 10s'))
        return

    phase = progress.get('phase', '')
    done = progress.get('done', 0)
    total = progress.get('total', 0)
    current = progress.get('current', '')

    pct = int(done * 100 / total) if total > 0 else 0
    bar = (
        f'<div class="tag-progress-bar-wrap">'
        f'<div class="tag-progress-bar" style="width:{pct}%"></div>'
        f'</div>'
    ) if total > 0 else ''

    phase_label = phase.replace('_', ' ').replace('(', '(').title()
    text = f'{done}/{total}' if total else ''
    if current:
        text += f' \u2014 {esc(current)}' if text else esc(current)
    text += '\u2026'

    inner = (
        f'<span class="tag-progress-phase">{esc(phase_label)}</span>'
        f'{bar}'
        f'<span class="tag-progress-text">{text}</span>'
    )
    send_html(handler,
        wrap(inner, poll='every 2s', active=True)
        + train_btn_oob(True))


def handle_save_tags(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import update_tags

    form = read_form_body(handler)
    rel_path = form.get('path', '')
    video_name = form.get('video', '')
    raw_tags = form.get('tags', '')

    resolved = resolve_path(root, rel_path)
    if not resolved.is_dir() or not video_name:
        handler.send_error(400, 'Invalid request')
        return

    tags_list = [t.strip() for t in raw_tags.split(',') if t.strip()]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    update_tags(resolved, lambda tags: {**tags, video_name: {
        **tags.get(video_name, {}),
        'tags': tags_list, 'status': 'confirmed', 'tagged_at': now,
    }})

    # Return updated pill HTML for HTMX swap
    send_html(handler, render_video_tags_inline(rel_path, video_name, tags_list))


def handle_star_update(handler, root):
    if not _config['allow_tag']:
        handler.send_error(403, 'Tagging not enabled')
        return
    from simpleparty.tagger import set_starred, update_tags

    form = read_form_body(handler)
    rel_dir = form.get('dir', '')
    video_name = form.get('name', '')
    starred_flag = form.get('starred', '') == '1'

    if not video_name:
        handler.send_error(400, 'Missing video name')
        return

    resolved = resolve_path(root, rel_dir)
    if not resolved.is_dir():
        handler.send_error(404, 'Directory not found')
        return
    if not (resolved / video_name).exists():
        handler.send_error(404, 'Video not found')
        return

    update_tags(resolved, lambda tags: set_starred(tags, video_name, starred_flag))

    handler.send_response(204)
    handler.send_header('Content-Length', '0')
    handler.end_headers()


def handle_thumb(handler, root):
    """Serve a thumbnail JPEG from .simpleparty/thumbs/."""
    raw = urllib.parse.urlparse(handler.path).path
    rel = raw[len('/thumb/'):]  # strip prefix
    rel = urllib.parse.unquote(rel)
    if not rel:
        handler.send_error(404)
        return
    # rel is "dir/subdir/video.mp4" — thumb is at dir/subdir/.simpleparty/thumbs/video.mp4.jpg
    from simpleparty.tagger import thumb_path
    video_dir = Path(root) / Path(rel).parent
    video_name = Path(rel).name
    tp = thumb_path(str(video_dir), video_name)
    if not tp.exists():
        handler.send_error(404)
        return
    data = tp.read_bytes()
    handler.send_response(200)
    handler.send_header('Content-Type', 'image/jpeg')
    handler.send_header('Content-Length', str(len(data)))
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.end_headers()
    handler.wfile.write(data)


def handle_download_page(handler, root):
    if not _config['allow_download']:
        handler.send_error(404)
        return
    params = parse_query(handler.path)
    rel = params.get('path', '')
    send_html(handler, render_download_page(rel))


def handle_download_submit(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    from simpleparty.downloader import validate_url, is_path_within

    form = read_form_body(handler)
    rel_path = form.get('path', '') or ''
    redirect_url = form.get('redirect') or ''
    try:
        url = validate_url(form.get('url', ''))
    except ValueError as e:
        handler.send_error(400, str(e))
        return

    resolved = resolve_path(root, rel_path)
    if not is_path_within(root, resolved) or not resolved.is_dir():
        handler.send_error(400, 'Invalid target directory')
        return

    job_id = uuid.uuid4().hex
    job = jobs.new_download_job(job_id, url, str(resolved), rel_path)
    with jobs.download_lock:
        jobs.download_jobs[job_id] = job
        jobs.download_order.append(job_id)
        jobs.evict_download_history()
    jobs.ensure_download_worker(root)
    jobs.download_queue.put(job_id)

    send_hx_redirect(handler, redirect_url or '/download')


def handle_download_status(handler, root):
    if not _config['allow_download']:
        send_html(handler, '')
        return
    params = parse_query(handler.path)
    # The browse-page inline panel carries inline=1 so it stays scoped (and
    # hidden when empty) even for the root dir, where the blank path is dropped.
    if 'inline' in params:
        path_filter = params.get('path', '')
    else:
        path_filter = params.get('path') if 'path' in params else None
    send_html(handler, render_download_status(path_filter=path_filter))


def handle_download_cancel(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    form = read_form_body(handler)
    job_id = form.get('id', '')
    with jobs.download_lock:
        job = jobs.download_jobs.get(job_id)
        if job and job.get('state') == 'queued':
            job['state'] = 'cancelled'
            job['finished_at'] = time.time()
        elif job and job.get('state') == 'running':
            # Signal the worker's progress hook to abort the in-flight download.
            job['cancel_requested'] = True
    send_hx_redirect(handler, '/download')


def handle_download_clear(handler, root):
    if not _config['allow_download']:
        handler.send_error(403, 'Download disabled')
        return
    with jobs.download_lock:
        keep = [jid for jid in jobs.download_order
                if jobs.download_jobs.get(jid, {}).get('state') in ('queued', 'running')]
        dropped = [jid for jid in jobs.download_order if jid not in keep]
        for jid in dropped:
            jobs.download_jobs.pop(jid, None)
        jobs.download_order = keep
    send_hx_redirect(handler, '/download')


# --- Static assets ---

_STATIC_MIME = {
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
}


def _load_static():
    """Read packaged static assets once at import (works from wheels/zipapps)."""
    base = importlib.resources.files('simpleparty') / 'static'
    out = {}
    for entry in base.iterdir():
        if entry.is_file():
            suffix = Path(entry.name).suffix
            mime = _STATIC_MIME.get(suffix, 'application/octet-stream')
            out[entry.name] = (entry.read_bytes(), mime)
    return out


_STATIC = _load_static()


def handle_static(handler, root):
    """Serve a packaged asset from the preloaded dict (no filesystem access)."""
    name = urllib.parse.urlparse(handler.path).path[len('/static/'):]
    item = _STATIC.get(name)
    if item is None:
        handler.send_error(404)
        return
    body, ctype = item
    handler.send_response(200)
    handler.send_header('Content-Type', ctype)
    handler.send_header('Content-Length', str(len(body)))
    handler.send_header('Cache-Control', 'public, max-age=3600')
    handler.end_headers()
    if handler.command != 'HEAD':
        handler.wfile.write(body)


# --- Dispatch ---

GET_ROUTES = {
    '/': handle_browse,
    '/browse': handle_browse,
    '/play': handle_play,
    '/tag-status': handle_tag_status,
    '/download': handle_download_page,
    '/download-status': handle_download_status,
}

GET_PREFIXES = (
    ('/video/', handle_video),
    ('/thumb/', handle_thumb),
    ('/static/', handle_static),
)

POST_ROUTES = {
    '/delete': handle_delete,
    '/delete-by-tag': handle_delete_by_tag,
    '/unlock': handle_unlock,
    '/lock': handle_lock,
    '/train': handle_train,
    '/suggest': handle_suggest,
    '/suggest-one': handle_suggest_one,
    '/confirm-tags': handle_confirm_tags,
    '/confirm-all': handle_confirm_all,
    '/reject-tags': handle_reject_tags,
    '/reject-tag': handle_reject_tag,
    '/save-tags': handle_save_tags,
    '/star-update': handle_star_update,
    '/download': handle_download_submit,
    '/download-cancel': handle_download_cancel,
    '/download-clear': handle_download_clear,
}


def dispatch(handler, root, exact, prefixes=()):
    path = urllib.parse.urlparse(handler.path).path
    fn = exact.get(path) or next(
        (f for p, f in prefixes if path.startswith(p)), None)
    if fn is None:
        handler.send_error(404)
    else:
        fn(handler, root)
