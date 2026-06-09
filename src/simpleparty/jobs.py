"""Mutable runtime job state (tag/thumbnail/download) and the download worker.

All cross-thread mutable state lives here, separate from the write-once
settings in simpleparty.state.CONFIG.
"""

import logging
import queue
import threading
import time
from pathlib import Path

from simpleparty.library import is_video
from simpleparty.state import CONFIG, DOWNLOAD_HISTORY_LIMIT

logger = logging.getLogger('simpleparty.jobs')

_tag_jobs = {}  # resolved dir path -> progress dict
_tag_jobs_lock = threading.Lock()

thumb_jobs = set()  # directories currently generating thumbs
duration_jobs = set()  # directories currently probing durations
_job_sets_lock = threading.Lock()


def try_claim(job_set, key):
    """Atomically claim a job slot. Returns False if already claimed —
    prevents concurrent requests from spawning duplicate workers."""
    with _job_sets_lock:
        if key in job_set:
            return False
        job_set.add(key)
        return True

download_queue = None          # queue.Queue[str], lazy
download_jobs = {}             # job_id -> job dict (see new_download_job)
download_order = []            # job_ids in enqueue order, capped
download_lock = threading.Lock()
download_worker = None         # threading.Thread, lazy


# --- Tag job accessors ---

def get_tag_job(resolved_dir):
    with _tag_jobs_lock:
        return _tag_jobs.get(str(resolved_dir))


def set_tag_job(resolved_dir, progress):
    with _tag_jobs_lock:
        _tag_jobs[str(resolved_dir)] = progress


# --- Download queue ---

def new_download_job(job_id, url, target_dir, target_rel):
    return {
        'id': job_id,
        'url': url,
        'target_dir': target_dir,
        'target_rel': target_rel,
        'state': 'queued',
        'running': False,
        'status': '',
        'phase': 'queued',
        'enqueued_at': time.time(),
        'started_at': None,
        'finished_at': None,
        'title': '',
        'filename': '',
        'downloaded_bytes': 0,
        'total_bytes': 0,
        'percent': 0,
        'speed': None,
        'eta': None,
        'final_path': None,
        'final_name': None,
        'play_dir': None,
        'play_name': None,
        'error': None,
    }


def evict_download_history():
    """Trim non-running jobs beyond the history limit (oldest first). Caller holds download_lock."""
    global download_order
    non_running = [jid for jid in download_order
                   if not download_jobs.get(jid, {}).get('running')]
    excess = len(non_running) - DOWNLOAD_HISTORY_LIMIT
    if excess <= 0:
        return
    to_drop = set(non_running[:excess])
    download_order = [jid for jid in download_order if jid not in to_drop]
    for jid in to_drop:
        download_jobs.pop(jid, None)


def _finalize_download_job(job, root):
    """After a download ends, compute play/browse links if final file is in root."""
    final = job.get('final_path')
    if not final:
        return
    try:
        rel = Path(final).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return
    if not Path(final).exists() or not is_video(Path(final).name):
        return
    rel_str = str(rel)
    if '/' in rel_str:
        parent, name = rel_str.rsplit('/', 1)
    else:
        parent, name = '', rel_str
    job['play_dir'] = parent
    job['play_name'] = name


def _download_worker_loop(root):
    from simpleparty.downloader import download_video
    q = download_queue
    while True:
        job_id = q.get()
        with download_lock:
            job = download_jobs.get(job_id)
        if not job:
            continue
        if job.get('state') == 'cancelled':
            continue
        job['state'] = 'running'
        job['started_at'] = time.time()
        try:
            download_video(
                job['url'], job['target_dir'], job,
                format_str=CONFIG.get('yt_dlp_format'),
            )
            if job.get('cancel_requested'):
                job['state'] = 'cancelled'
                job.pop('error', None)
            elif job.get('error'):
                job['state'] = 'error'
            else:
                job['state'] = 'done'
        except Exception as e:
            logger.exception('download worker: unexpected failure')
            job['error'] = str(e) or e.__class__.__name__
            job['state'] = 'error'
        finally:
            job['running'] = False
            job['finished_at'] = time.time()
            if job.get('state') != 'cancelled':
                _finalize_download_job(job, root)


def ensure_download_worker(root):
    """Create queue + daemon worker on first use."""
    global download_queue, download_worker
    with download_lock:
        if download_queue is None:
            download_queue = queue.Queue()
        if download_worker is None or not download_worker.is_alive():
            t = threading.Thread(
                target=_download_worker_loop,
                args=(root,),
                daemon=True,
            )
            download_worker = t
            t.start()


def snapshot_download_jobs():
    with download_lock:
        order = list(download_order)
        jobs = {jid: dict(download_jobs[jid])
                for jid in order if jid in download_jobs}
    return order, jobs


def any_download_running(jobs):
    return any(j.get('running') for j in jobs.values())
