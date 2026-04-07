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

TAGS_FILENAME = '.simpleparty-tags.json'
MODEL_FILENAME = '.simpleparty-model.pt'


# --- Tag file I/O ---

def load_tags(directory_path):
    """Load tags JSON for a directory, or return empty dict."""
    tags_file = Path(directory_path) / TAGS_FILENAME
    if not tags_file.exists():
        return {}
    try:
        with open(tags_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_tags(directory_path, tags):
    """Atomically write tags JSON for a directory."""
    tags_file = Path(directory_path) / TAGS_FILENAME
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=directory_path, suffix='.tmp', prefix='.simpleparty-tags-',
    )
    try:
        with os.fdopen(tmp_fd, 'w') as f:
            json.dump(tags, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, tags_file)
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
