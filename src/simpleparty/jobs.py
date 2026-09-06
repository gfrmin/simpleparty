"""Mutable runtime job state (tag/thumbnail/download) and the download worker.

All cross-thread mutable state lives here, separate from the write-once
settings in simpleparty.state.CONFIG.
"""

import collections
import logging
import os
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
reencode_scan_jobs = set()  # directories currently being scanned for reencode candidates
reencode_scanned_at = {}  # dir str -> dir mtime_ns as of the last completed scan
job_sets_lock = threading.Lock()


_SCANNED_AT_MAX = 4096  # one entry per directory ever scanned; a recursive
                        # tree scan can add thousands in a single request


def try_claim(job_set, key):
    """Atomically claim a job slot. Returns False if already claimed —
    prevents concurrent requests from spawning duplicate workers."""
    with job_sets_lock:
        if key in job_set:
            return False
        job_set.add(key)
        return True


def mark_dir_scanned(dir_str, mtime_ns):
    """Record a directory as fully scanned at `mtime_ns`, evicting the oldest
    entries past the cap. Losing an old entry only costs a redundant re-scan
    later, so oldest-first is safe."""
    with job_sets_lock:
        reencode_scanned_at.pop(dir_str, None)  # re-insert so it counts as newest
        reencode_scanned_at[dir_str] = mtime_ns
        while len(reencode_scanned_at) > _SCANNED_AT_MAX:
            reencode_scanned_at.pop(next(iter(reencode_scanned_at)))

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


def claim_tag_job(resolved_dir, progress):
    """Atomically install `progress` unless a job is already running for the
    directory. Returns False when one is (caller should not spawn)."""
    with _tag_jobs_lock:
        existing = _tag_jobs.get(str(resolved_dir))
        if existing and existing.get('running'):
            return False
        _tag_jobs[str(resolved_dir)] = progress
        return True


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
    """Trim finished jobs beyond the history limit (oldest first). Caller
    holds download_lock. Queued jobs are never evicted — dropping one would
    silently swallow a pending download when its id is dequeued."""
    global download_order
    finished = [jid for jid in download_order
                if download_jobs.get(jid, {}).get('state') in ('done', 'error', 'cancelled')]
    excess = len(finished) - DOWNLOAD_HISTORY_LIMIT
    if excess <= 0:
        return
    to_drop = set(finished[:excess])
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


# --- Background re-encode queue ---
#
# A deque (not queue.Queue) so a video can be reprioritized to the front
# when someone opens it, without disturbing the rest of the pending order.
# Single worker by design: re-encoding is CPU-heavy, and running several at
# once would just make every one of them slower.

reencode_queue = collections.deque()  # pending path strs, head = next up
reencode_status = {}  # path str -> {'state','queued_at','started_at','finished_at','error'}
reencode_current = None  # path str currently encoding, or None
_reencode_lock = threading.Lock()
_reencode_cv = threading.Condition(_reencode_lock)
reencode_worker = None  # threading.Thread, lazy


def enqueue_reencode(path, *, retry=False):
    """Idempotently append `path` to the tail of the reencode queue. Returns
    True if it was actually queued.

    No-op if `path` already has any status (queued/encoding/done/failed) or
    is the item currently encoding: terminal states are sticky, so automatic
    scanning never retries a failed encode in a loop.

    `retry` lifts that for a terminal state only, and exists because the user
    pressing "Queue all" is explicitly asking for another attempt. Without it
    that button silently queued nothing while reporting success, which is
    what a locked-then-unlocked fscrypt directory left behind. Never
    disturbs the item currently encoding.
    """
    path = str(path)
    with _reencode_lock:
        if path == reencode_current:
            return False
        existing = reencode_status.get(path)
        if existing is not None and not (
                retry and existing.get('state') in ('done', 'failed')):
            return False
        try:
            reencode_queue.remove(path)
        except ValueError:
            pass
        reencode_queue.append(path)
        reencode_status[path] = {
            'state': 'queued', 'queued_at': time.time(),
            'started_at': None, 'finished_at': None, 'error': None,
            'percent': None, 'speed': None, 'eta': None,
        }
        _reencode_cv.notify()
        return True


def prioritize_reencode(path):
    """Move `path` to the head of the queue, inserting it fresh if it hasn't
    been seen before. No-op if it's already encoding (no mid-encode
    preemption) or already done/failed."""
    path = str(path)
    with _reencode_lock:
        if path == reencode_current:
            return
        status = reencode_status.get(path)
        if status is not None and status['state'] in ('done', 'failed'):
            return
        try:
            reencode_queue.remove(path)
        except ValueError:
            pass
        reencode_queue.appendleft(path)
        if status is None:
            reencode_status[path] = {
                'state': 'queued', 'queued_at': time.time(),
                'started_at': None, 'finished_at': None, 'error': None,
                'percent': None, 'speed': None, 'eta': None,
            }
        else:
            status['state'] = 'queued'
        _reencode_cv.notify()


def _reencode_worker_loop():
    global reencode_current
    from simpleparty import media as _media
    while True:
        with _reencode_lock:
            while not reencode_queue:
                _reencode_cv.wait()
            path = reencode_queue.popleft()
            reencode_current = path
            reencode_status.setdefault(path, {})
            reencode_status[path]['state'] = 'encoding'
            reencode_status[path]['started_at'] = time.time()

        ok = False
        error = None
        try:
            ok = _media._reencode_video(path)
        except Exception as e:
            logger.exception('reencode worker: unexpected failure for %s', path)
            error = str(e) or e.__class__.__name__
        finally:
            with _reencode_lock:
                status = reencode_status.setdefault(path, {})
                # An *unexplained* failure on a source that is no longer
                # there isn't worth remembering. Locking an fscrypt directory
                # replaces every plaintext name in it at once, so a whole
                # queue "fails" in milliseconds with nothing to report — and
                # since terminal states are sticky, that left every one of
                # those videos unqueueable long after the directory was
                # unlocked again. Forgetting the entry makes them eligible on
                # the next scan, and costs nothing if the file really was
                # deleted. A failure that came with an exception or a
                # recorded reason is a genuine one and is kept either way.
                unexplained = error is None and not status.get('error')
                if not ok and unexplained and not os.path.exists(path):
                    logger.info('reencode: source no longer present, '
                                'forgetting %s', path)
                    reencode_status.pop(path, None)
                    reencode_current = None
                    continue
                status['state'] = 'done' if ok else 'failed'
                status['finished_at'] = time.time()
                if error is not None:
                    status['error'] = error
                elif ok:
                    status['error'] = None
                # else: an ordinary False return, not an exception. Leave
                # whatever _reencode_video recorded via
                # report_reencode_progress() instead of clobbering the only
                # explanation the UI has for the failure.
                reencode_current = None
                _evict_reencode_status()


def ensure_reencode_worker():
    """Create the daemon worker on first use (mirrors ensure_download_worker)."""
    global reencode_worker
    with _reencode_lock:
        if reencode_worker is None or not reencode_worker.is_alive():
            t = threading.Thread(target=_reencode_worker_loop, daemon=True)
            reencode_worker = t
            t.start()


def report_reencode_progress(path, *, percent=None, speed=None, eta=None, error=None):
    """Update live progress fields for an in-flight re-encode.

    `None` means "leave unchanged" — a given encode's duration is either known
    or unknown for its whole run, so callers never need to clear a field back
    to None mid-stream.

    Called from media._reencode_video on the single re-encode worker thread,
    so there is never a write-write race; the lock is held so HTTP reader
    threads in reencode_snapshot() never see a half-updated set of fields.
    No-op for an unknown path (defensive: the worker always creates the entry
    before calling into the encoder).
    """
    path = str(path)
    with _reencode_lock:
        status = reencode_status.get(path)
        if status is None:
            return
        if percent is not None:
            status['percent'] = percent
        if speed is not None:
            status['speed'] = speed
        if eta is not None:
            status['eta'] = eta
        if error is not None:
            status['error'] = error


def reencode_snapshot():
    """Consistent snapshot of the re-encode queue, mirroring
    snapshot_download_jobs() so render.py never touches the lock itself.

    Returns (order, status, current):
      order   -- pending path strs in queue order, head first (excludes the
                 path currently encoding)
      status  -- deep-ish copy of reencode_status (each value dict copied)
      current -- the path presently encoding, or None
    """
    with _reencode_lock:
        order = list(reencode_queue)
        status = {p: dict(s) for p, s in reencode_status.items()}
        current = reencode_current
    return order, status, current


_REENCODE_STATUS_MAX = 500  # finished (done/failed) entries retained


def _evict_reencode_status():
    """Drop the oldest finished entries beyond the cap. Caller holds
    _reencode_lock. Queued/encoding entries are never evicted, however many
    there are — only completed history is trimmed."""
    finished = [(p, s) for p, s in reencode_status.items()
                if s.get('state') in ('done', 'failed')]
    excess = len(finished) - _REENCODE_STATUS_MAX
    if excess <= 0:
        return
    finished.sort(key=lambda kv: kv[1].get('finished_at') or 0)
    for p, _ in finished[:excess]:
        reencode_status.pop(p, None)


# --- Recursive tree-scan jobs ---
#
# Deliberately a separate store from the single-directory reencode_scan_jobs
# set: the automatic per-directory scan and an explicit tree scan of the same
# directory must be able to run concurrently without contending for a claim.

_tree_scan_jobs = {}  # resolved root dir str -> job dict
_tree_scan_jobs_lock = threading.Lock()
_TREE_SCAN_JOBS_MAX = 32  # entries can hold thousands of path strings


def new_tree_scan_job():
    return {
        'running': True, 'started_at': time.time(), 'finished_at': None,
        'scanned_dirs': 0, 'scanned_videos': 0, 'found': [],
        'skipped_locked': 0, 'skipped_unreadable': 0, 'truncated': False,
        'error': None, 'confirmed': False, 'queued_count': 0,
    }


def get_tree_scan_job(resolved_dir):
    with _tree_scan_jobs_lock:
        return _tree_scan_jobs.get(str(resolved_dir))


def claim_tree_scan_job(resolved_dir, job):
    """Install `job` unless a scan is already running for this directory.
    Returns False when one is (render the existing job instead of spawning a
    second thread). Re-scanning a directory whose previous scan finished is
    allowed — clicking again after dismissing should get a fresh result, not
    a replayed stale one."""
    key = str(resolved_dir)
    with _tree_scan_jobs_lock:
        existing = _tree_scan_jobs.get(key)
        if existing and existing.get('running'):
            return False
        _tree_scan_jobs[key] = job
        while len(_tree_scan_jobs) > _TREE_SCAN_JOBS_MAX:
            _tree_scan_jobs.pop(next(iter(_tree_scan_jobs)))
        return True


def drop_tree_scan_job(resolved_dir):
    """Forget a finished scan's result (and its retained path list)."""
    with _tree_scan_jobs_lock:
        _tree_scan_jobs.pop(str(resolved_dir), None)


def clear_pending_reencodes():
    """Drop every queued (not-yet-started) re-encode, returning how many.

    The item currently encoding is left to finish: _reencode_video has no
    cooperative-cancellation hook, so stopping it would mean killing an
    ffmpeg subprocess mid-write.
    """
    with _reencode_lock:
        dropped = list(reencode_queue)
        reencode_queue.clear()
        for p in dropped:
            reencode_status.pop(p, None)
        return len(dropped)
