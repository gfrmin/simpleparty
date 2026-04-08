"""Video tagging: keyframe extraction and tag file I/O."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

SIMPLEPARTY_DIR = '.simpleparty'
TAGS_FILENAME = 'tags.json'
MODEL_FILENAME = 'model.pt'
THUMB_DIR = 'thumbs'
FRAMES_DIR = SIMPLEPARTY_DIR + '/frames'

# Legacy paths for backward compat
_LEGACY_TAGS = '.simpleparty-tags.json'
_LEGACY_MODEL = '.simpleparty-model.pt'


# --- Tag file I/O ---

def _sp_dir(directory_path):
    """Return the .simpleparty directory path, creating it if needed."""
    d = Path(directory_path) / SIMPLEPARTY_DIR
    d.mkdir(exist_ok=True)
    return d


def load_tags(directory_path):
    """Load tags JSON for a directory, or return empty dict."""
    tags_file = Path(directory_path) / SIMPLEPARTY_DIR / TAGS_FILENAME
    if not tags_file.exists():
        # Legacy fallback
        legacy = Path(directory_path) / _LEGACY_TAGS
        if legacy.exists():
            tags_file = legacy
        else:
            return {}
    try:
        with open(tags_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_tags(directory_path, tags):
    """Atomically write tags JSON for a directory."""
    sp = _sp_dir(directory_path)
    tags_file = sp / TAGS_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(sp), suffix='.tmp', prefix='tags-',
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(tags, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(tags_file))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def untagged_videos(directory_path, existing_tags):
    """Return list of video filenames in directory not yet tagged."""
    result = []
    try:
        for name in sorted(os.listdir(directory_path)):
            if name.startswith('.'):
                continue
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                if name not in existing_tags:
                    result.append(name)
    except OSError:
        pass
    return result


def confirmed_entries(tags):
    """Return dict of entries with status != 'suggested'."""
    return {
        name: entry for name, entry in tags.items()
        if entry.get('status', 'confirmed') != 'suggested'
        and entry.get('tags')
    }


# --- Keyframe extraction ---

def _is_dark_frame(jpeg_path, threshold=20):
    """Check if a JPEG is nearly black by sampling pixel bytes."""
    try:
        data = Path(jpeg_path).read_bytes()
        if len(data) < 1000:
            return True
        start = len(data) // 3
        end = 2 * len(data) // 3
        sample = data[start:end:10]
        if not sample:
            return True
        avg = sum(sample) / len(sample)
        return avg < threshold
    except OSError:
        return True


def _get_duration(video_path):
    """Get video duration in seconds via ffprobe."""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'csv=p=0',
        str(video_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10, text=True)
        return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return 0.0


def extract_keyframes(video_path, max_frames=3):
    """Extract frames at evenly spaced positions through the video.

    For max_frames=3, extracts at 25%, 50%, 75% of duration.
    For max_frames=1, extracts at 50%.
    Returns list of JPEG paths. Caller must clean up the temp directory.
    """
    tmpdir = tempfile.mkdtemp(prefix='simpleparty-frames-')
    duration = _get_duration(video_path)
    if duration <= 0:
        return []

    positions = [duration * (i + 1) / (max_frames + 1) for i in range(max_frames)]

    for idx, pos in enumerate(positions):
        out_path = os.path.join(tmpdir, f'frame_{idx:02d}.jpg')
        cmd = [
            'ffmpeg', '-ss', f'{pos:.2f}',
            '-i', str(video_path),
            '-frames:v', '1',
            '-q:v', '4',
            out_path,
        ]
        try:
            subprocess.run(
                cmd, capture_output=True, timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired:
            pass

    frames = sorted(Path(tmpdir).glob('frame_*.jpg'))
    usable = [f for f in frames if not _is_dark_frame(f)]
    return usable[:max_frames]


def model_path(directory_path):
    """Return path to the trained model, checking legacy location as fallback."""
    p = Path(directory_path) / SIMPLEPARTY_DIR / MODEL_FILENAME
    if p.exists():
        return p
    legacy = Path(directory_path) / _LEGACY_MODEL
    return legacy if legacy.exists() else p


def thumb_path(directory_path, video_name):
    """Return path where a video's thumbnail should live."""
    return Path(directory_path) / SIMPLEPARTY_DIR / THUMB_DIR / f'{video_name}.jpg'


def extract_frame(video_path, position, out_path):
    """Extract a single full-res frame at *position* seconds. Returns True on success."""
    cmd = [
        'ffmpeg', '-ss', f'{position:.2f}',
        '-i', str(video_path),
        '-frames:v', '1',
        '-q:v', '4',
        str(out_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        return Path(out_path).exists() and not _is_dark_frame(out_path)
    except subprocess.TimeoutExpired:
        return False


def _downscale_frame(frame_path, output_path):
    """Downscale a JPEG to 320px width for use as a thumbnail. Returns True on success."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'ffmpeg', '-i', str(frame_path),
        '-vf', 'scale=320:-1',
        '-q:v', '6',
        '-y', str(output_path),
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return False
    out = Path(output_path)
    if not out.exists():
        return False
    if _is_dark_frame(out):
        out.unlink()
        return False
    return True


def extract_thumbnail(video_path, output_path):
    """Extract a thumbnail by downscaling a full-res frame.

    Reuses an existing full-res frame from the frames dir if available,
    otherwise extracts one first. Returns True on success.
    """
    video_path = Path(video_path)
    video_name = video_path.name
    frames_dir = video_path.parent / FRAMES_DIR
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Look for an existing full-res frame
    existing = sorted(frames_dir.glob(f'{video_name}.f*.jpg'))
    if existing:
        return _downscale_frame(existing[0], output_path)

    # Extract a new full-res frame at 50% duration
    duration = _get_duration(video_path)
    if duration <= 0:
        return False

    frame_path = frames_dir / f'{video_name}.f0.jpg'
    if not extract_frame(str(video_path), duration / 2, str(frame_path)):
        return False

    return _downscale_frame(str(frame_path), output_path)
