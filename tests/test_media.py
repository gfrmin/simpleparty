"""Tests for media module: cached-transcode lookup and the background
re-encode queue/worker/scan trigger."""

import os
import time

import pytest

from simpleparty import jobs as sp_jobs
from simpleparty import media as sp_media
from simpleparty import state as sp_state


# --- _cached_transcode ---

def test_cached_transcode_missing(tmp_path):
    src = tmp_path / 'movie.mkv'
    src.write_bytes(b'source')
    assert sp_media._cached_transcode(src) is None


def test_cached_transcode_fresh(tmp_path):
    src = tmp_path / 'movie.mkv'
    src.write_bytes(b'source')
    cached = sp_media._transcoded_path(tmp_path, 'movie.mkv')
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'cached')
    future = time.time() + 10
    os.utime(cached, (future, future))
    assert sp_media._cached_transcode(src) == cached


def test_cached_transcode_stale(tmp_path):
    src = tmp_path / 'movie.mkv'
    src.write_bytes(b'source')
    cached = sp_media._transcoded_path(tmp_path, 'movie.mkv')
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b'cached')
    os.utime(cached, (0, 0))  # far older than src
    assert sp_media._cached_transcode(src) is None


def test_cached_transcode_vanished_source(tmp_path):
    assert sp_media._cached_transcode(tmp_path / 'gone.mkv') is None


# --- jobs.py reencode queue ---

@pytest.fixture(autouse=True)
def _reencode_state_snapshot(monkeypatch):
    """Snapshot/restore the reencode queue state, same idiom as
    test_http_smoke.py's _config_snapshot fixture.

    The worker is a process-wide singleton daemon that another test file may
    already have started, and once running it drains the shared deque
    immediately — which would make the queue-ordering assertions below race.
    Suppressing the condition-variable notify keeps any live worker parked in
    wait() so the queue stays inspectable; tests that genuinely need the
    worker call wake() to opt back in.
    """
    real_notify = sp_jobs._reencode_cv.notify
    monkeypatch.setattr(sp_jobs._reencode_cv, 'notify', lambda *a, **kw: None)

    def wake():
        with sp_jobs._reencode_lock:
            real_notify()

    with sp_jobs._reencode_lock:
        saved_queue = list(sp_jobs.reencode_queue)
        saved_status = dict(sp_jobs.reencode_status)
        saved_current = sp_jobs.reencode_current
        sp_jobs.reencode_queue.clear()
        sp_jobs.reencode_status.clear()
        sp_jobs.reencode_current = None
    with sp_jobs.job_sets_lock:
        saved_scan_jobs = set(sp_jobs.reencode_scan_jobs)
        saved_scanned_at = dict(sp_jobs.reencode_scanned_at)
        sp_jobs.reencode_scan_jobs.clear()
        sp_jobs.reencode_scanned_at.clear()
    yield wake
    with sp_jobs._reencode_lock:
        sp_jobs.reencode_queue.clear()
        sp_jobs.reencode_queue.extend(saved_queue)
        sp_jobs.reencode_status.clear()
        sp_jobs.reencode_status.update(saved_status)
        sp_jobs.reencode_current = saved_current
    with sp_jobs.job_sets_lock:
        sp_jobs.reencode_scan_jobs.clear()
        sp_jobs.reencode_scan_jobs.update(saved_scan_jobs)
        sp_jobs.reencode_scanned_at.clear()
        sp_jobs.reencode_scanned_at.update(saved_scanned_at)


def test_enqueue_reencode_is_idempotent():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.enqueue_reencode('a')
    assert list(sp_jobs.reencode_queue) == ['a']
    assert sp_jobs.reencode_status['a']['state'] == 'queued'


def test_prioritize_reorders_existing_entry():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.enqueue_reencode('b')
    sp_jobs.prioritize_reencode('b')
    assert list(sp_jobs.reencode_queue) == ['b', 'a']


def test_prioritize_inserts_unseen_path_at_head():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.prioritize_reencode('c')
    assert list(sp_jobs.reencode_queue) == ['c', 'a']
    assert sp_jobs.reencode_status['c']['state'] == 'queued'


def test_prioritize_noop_for_currently_encoding():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.reencode_current = 'x'
    sp_jobs.prioritize_reencode('x')
    assert list(sp_jobs.reencode_queue) == ['a']
    assert 'x' not in sp_jobs.reencode_status


def test_enqueue_and_prioritize_noop_for_terminal_state():
    sp_jobs.reencode_status['a'] = {
        'state': 'failed', 'queued_at': 0, 'started_at': 0,
        'finished_at': 0, 'error': 'boom',
    }
    sp_jobs.enqueue_reencode('a')
    sp_jobs.prioritize_reencode('a')
    assert list(sp_jobs.reencode_queue) == []


def test_reencode_snapshot():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.enqueue_reencode('b')
    order, status, current = sp_jobs.reencode_snapshot()
    assert order == ['a', 'b']
    assert current is None
    assert status['a']['state'] == 'queued'
    # The snapshot must be a copy: mutating it can't corrupt live job state.
    status['a']['state'] = 'tampered'
    assert sp_jobs.reencode_status['a']['state'] == 'queued'


def test_worker_survives_exception_and_keeps_processing(monkeypatch, _reencode_state_snapshot):
    outcomes = {'bad': RuntimeError('boom'), 'good': True}

    def stub(path):
        outcome = outcomes[path]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(sp_media, '_reencode_video', stub)

    sp_jobs.enqueue_reencode('bad')
    sp_jobs.enqueue_reencode('good')
    sp_jobs.ensure_reencode_worker()
    _reencode_state_snapshot()  # wake(): let the worker actually run

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (sp_jobs.reencode_status.get('bad', {}).get('state') == 'failed'
                and sp_jobs.reencode_status.get('good', {}).get('state') == 'done'):
            break
        time.sleep(0.02)

    assert sp_jobs.reencode_status['bad']['state'] == 'failed'
    assert sp_jobs.reencode_status['bad']['error']
    assert sp_jobs.reencode_status['good']['state'] == 'done'


# --- _maybe_start_reencode_scan throttling ---

def test_maybe_start_reencode_scan_throttled_by_dir_mtime(tmp_path, monkeypatch):
    (tmp_path / 'a.mp4').write_bytes(b'x')
    videos = [{'name': 'a.mp4', 'path': 'a.mp4', 'size': 1, 'mtime': 0.0}]

    calls = []

    def fake_scan(directory, videos, dir_mtime_ns):
        calls.append(dir_mtime_ns)
        with sp_jobs.job_sets_lock:
            sp_jobs.reencode_scanned_at[str(directory)] = dir_mtime_ns
        sp_jobs.reencode_scan_jobs.discard(str(directory))

    monkeypatch.setattr(sp_media, '_scan_for_reencode', fake_scan)
    monkeypatch.setitem(sp_state.CONFIG, 'has_ffmpeg', True)
    monkeypatch.setitem(sp_state.CONFIG, 'allow_transcode', True)
    monkeypatch.setitem(sp_state.CONFIG, 'allow_pretranscode', True)

    def wait_for(n):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(calls) < n:
            time.sleep(0.01)

    sp_media._maybe_start_reencode_scan(tmp_path, videos)
    wait_for(1)
    assert len(calls) == 1

    # Directory untouched: throttled, no second scan.
    sp_media._maybe_start_reencode_scan(tmp_path, videos)
    time.sleep(0.1)
    assert len(calls) == 1

    # Directory contents changed: a new scan is allowed.
    (tmp_path / 'b.mp4').write_bytes(b'y')
    sp_media._maybe_start_reencode_scan(tmp_path, videos)
    wait_for(2)
    assert len(calls) == 2


def test_maybe_start_reencode_scan_gated_on_pretranscode_flag(tmp_path, monkeypatch):
    (tmp_path / 'a.mp4').write_bytes(b'x')
    videos = [{'name': 'a.mp4', 'path': 'a.mp4', 'size': 1, 'mtime': 0.0}]

    calls = []
    monkeypatch.setattr(sp_media, '_scan_for_reencode', lambda *a: calls.append(a))
    monkeypatch.setitem(sp_state.CONFIG, 'has_ffmpeg', True)
    monkeypatch.setitem(sp_state.CONFIG, 'allow_transcode', True)
    monkeypatch.setitem(sp_state.CONFIG, 'allow_pretranscode', False)

    sp_media._maybe_start_reencode_scan(tmp_path, videos)
    time.sleep(0.1)
    assert calls == []


# --- ffmpeg -progress parsing (pure; no ffmpeg involved) ---

def test_parse_progress_line_accumulates_until_terminator():
    scratch = {}
    assert sp_media._parse_progress_line('frame=12\n', scratch) is None
    assert sp_media._parse_progress_line('out_time_us=5000000\n', scratch) is None
    block = sp_media._parse_progress_line('progress=continue\n', scratch)
    assert block == {'frame': '12', 'out_time_us': '5000000',
                     'progress': 'continue'}
    assert scratch == {}  # cleared, ready for the next block


def test_parse_progress_line_ignores_junk():
    scratch = {}
    assert sp_media._parse_progress_line('', scratch) is None
    assert sp_media._parse_progress_line('   \n', scratch) is None
    assert sp_media._parse_progress_line('no-equals-sign\n', scratch) is None
    assert scratch == {}


def test_progress_metrics_prefer_out_time_us():
    elapsed, speed = sp_media._progress_block_to_metrics(
        {'out_time_us': '15234567', 'out_time': '00:00:99.0', 'speed': '1.02x'})
    assert elapsed == pytest.approx(15.234567)
    assert speed == pytest.approx(1.02)


def test_progress_metrics_fall_back_to_out_time_string():
    elapsed, _ = sp_media._progress_block_to_metrics(
        {'out_time': '01:02:03.500000'})
    assert elapsed == pytest.approx(3723.5)


def test_progress_metrics_never_trust_out_time_ms():
    """ffmpeg populates out_time_ms with MICROseconds despite the name, so
    reading it would under-report elapsed time 1000x. With no out_time_us and
    no out_time, elapsed must be None rather than silently wrong."""
    elapsed, _ = sp_media._progress_block_to_metrics({'out_time_ms': '15234567'})
    assert elapsed is None


def test_progress_metrics_handle_na_speed():
    _, speed = sp_media._progress_block_to_metrics(
        {'out_time_us': '1000000', 'speed': 'N/A'})
    assert speed is None


# --- progress reporting into jobs state ---

def test_report_reencode_progress_updates_and_is_partial():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.report_reencode_progress('a', percent=42, speed=1.5)
    assert sp_jobs.reencode_status['a']['percent'] == 42
    assert sp_jobs.reencode_status['a']['speed'] == 1.5
    # None means "leave unchanged", not "clear".
    sp_jobs.report_reencode_progress('a', eta=12.0)
    assert sp_jobs.reencode_status['a']['percent'] == 42
    assert sp_jobs.reencode_status['a']['eta'] == 12.0


def test_report_reencode_progress_noop_for_unknown_path():
    sp_jobs.report_reencode_progress('never-seen', percent=5)
    assert 'never-seen' not in sp_jobs.reencode_status


def test_worker_preserves_error_from_a_false_return(monkeypatch, _reencode_state_snapshot):
    """Regression: the worker's finally block used to unconditionally assign
    status['error'] = error, clobbering the reason recorded by a plain False
    return (the common ffmpeg-failed case) back to None."""
    def stub(path):
        sp_jobs.report_reencode_progress(path, error='ffmpeg exited 1')
        return False

    monkeypatch.setattr(sp_media, '_reencode_video', stub)
    sp_jobs.enqueue_reencode('bad')
    sp_jobs.ensure_reencode_worker()
    _reencode_state_snapshot()  # wake(): let the worker actually run

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if sp_jobs.reencode_status.get('bad', {}).get('state') == 'failed':
            break
        time.sleep(0.02)

    assert sp_jobs.reencode_status['bad']['state'] == 'failed'
    assert sp_jobs.reencode_status['bad']['error'] == 'ffmpeg exited 1'


def test_evict_reencode_status_keeps_pending(monkeypatch):
    monkeypatch.setattr(sp_jobs, '_REENCODE_STATUS_MAX', 2)
    for i in range(5):
        sp_jobs.reencode_status[f'done{i}'] = {
            'state': 'done', 'finished_at': float(i), 'error': None,
        }
    sp_jobs.reencode_status['pending'] = {'state': 'queued', 'finished_at': None}
    with sp_jobs._reencode_lock:
        sp_jobs._evict_reencode_status()
    assert 'pending' in sp_jobs.reencode_status  # never evicted
    assert 'done0' not in sp_jobs.reencode_status  # oldest dropped first
    assert 'done4' in sp_jobs.reencode_status


def test_clear_pending_reencodes_spares_the_in_flight_item():
    sp_jobs.enqueue_reencode('a')
    sp_jobs.enqueue_reencode('b')
    sp_jobs.reencode_current = 'busy'
    sp_jobs.reencode_status['busy'] = {'state': 'encoding', 'finished_at': None}
    assert sp_jobs.clear_pending_reencodes() == 2
    assert list(sp_jobs.reencode_queue) == []
    assert 'a' not in sp_jobs.reencode_status
    assert sp_jobs.reencode_status['busy']['state'] == 'encoding'
