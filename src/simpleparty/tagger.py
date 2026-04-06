"""Optional AI video tagging via Ollama. Enabled with --tag flag."""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTENSIONS = frozenset({
    '.mp4', '.mkv', '.webm', '.mov', '.avi', '.m4v', '.ogv',
})

TAGS_FILENAME = '.simpleparty-tags.json'

SYSTEM_PROMPT = 'You are a concise video tagger. Respond with JSON only.'

USER_PROMPT = (
    'Describe this image in detail. Then extract tags as a flat list of short phrases. '
    'Return JSON only: {"description": "...", "tags": ["tag1", "tag2", ...]}'
)


# --- Prereq checks ---

def check_prereqs(ollama_url, model):
    """Returns (ok, errors). Checks ffmpeg, Ollama reachable, model pulled."""
    errors = []

    if not shutil.which('ffmpeg'):
        errors.append('ffmpeg not found in PATH')

    try:
        req = urllib.request.Request(f'{ollama_url}/api/tags')
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        model_names = [m['name'] for m in data.get('models', [])]
        # Check both exact match and without tag suffix
        model_base = model.split(':')[0]
        found = any(
            m == model or m.startswith(model_base + ':')
            for m in model_names
        )
        if not found:
            errors.append(
                f'Model {model!r} not found in Ollama. '
                f'Pull it with: ollama pull {model}'
            )
    except (urllib.error.URLError, OSError) as e:
        errors.append(f'Cannot reach Ollama at {ollama_url}: {e}')

    return (not errors, errors)


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


# --- Keyframe extraction ---

def _is_dark_frame(jpeg_path, threshold=20):
    """Check if a JPEG is nearly black by sampling pixel bytes.

    Reads raw file bytes and samples from the middle (past JPEG headers)
    to estimate average brightness. Not precise, but good enough to
    reject solid black frames without needing Pillow.
    """
    try:
        data = Path(jpeg_path).read_bytes()
        if len(data) < 1000:
            return True
        # Sample bytes from the middle third of the file
        start = len(data) // 3
        end = 2 * len(data) // 3
        sample = data[start:end:10]
        if not sample:
            return True
        avg = sum(sample) / len(sample)
        return avg < threshold
    except OSError:
        return True


def extract_keyframes(video_path, max_frames=3):
    """Extract I-frames from video using ffmpeg. Returns list of JPEG paths.

    Caller is responsible for cleaning up the returned temp directory
    (parent of the returned paths).
    """
    tmpdir = tempfile.mkdtemp(prefix='simpleparty-frames-')
    pattern = os.path.join(tmpdir, 'frame_%02d.jpg')

    cmd = [
        'ffmpeg', '-i', str(video_path),
        '-vf', 'select=eq(pict_type\\,I)',
        '-fps_mode', 'vfr',
        '-frames:v', str(max_frames),
        '-q:v', '4',
        pattern,
    ]

    try:
        subprocess.run(
            cmd, capture_output=True, timeout=60,
            check=False,  # Some videos may have warnings but still produce frames
        )
    except subprocess.TimeoutExpired:
        pass

    frames = sorted(Path(tmpdir).glob('frame_*.jpg'))
    # Filter out dark/black frames
    return [f for f in frames if not _is_dark_frame(f)]


# --- Ollama integration ---

def describe_frame(ollama_url, model, image_path):
    """Send a single frame to Ollama and get description + tags."""
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': USER_PROMPT, 'images': [img_b64]},
        ],
        'stream': False,
        'options': {'num_predict': 4096},
    }).encode()

    req = urllib.request.Request(
        f'{ollama_url}/api/chat',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=300)
        result = json.loads(resp.read())
        content = result.get('message', {}).get('content', '')
        return json.loads(content)
    except (json.JSONDecodeError, KeyError):
        # Model returned non-JSON — wrap it
        return {'description': content if 'content' in dir() else '', 'tags': []}
    except (urllib.error.URLError, OSError):
        return {'description': '', 'tags': []}


def tag_video(ollama_url, model, video_path):
    """Full tagging pipeline for one video. Returns {description, tags}."""
    frames = extract_keyframes(video_path)
    tmpdir = frames[0].parent if frames else None

    try:
        if not frames:
            return {'description': '', 'tags': []}

        all_descriptions = []
        all_tags = []

        for frame in frames:
            result = describe_frame(ollama_url, model, frame)
            desc = result.get('description', '')
            tags = result.get('tags', [])
            if desc:
                all_descriptions.append(desc)
            all_tags.extend(tags)

        # Deduplicate tags, case-insensitive, preserving first occurrence's casing
        seen = set()
        unique_tags = []
        for tag in all_tags:
            key = tag.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique_tags.append(tag.strip())

        description = ' '.join(all_descriptions)
        return {'description': description, 'tags': unique_tags}

    finally:
        # Clean up temp frames
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def tag_directory(ollama_url, model, directory_path, progress):
    """Tag all untagged videos in a directory.

    Args:
        progress: a dict that will be mutated with
                  {'running': bool, 'done': int, 'total': int, 'current': str}
    """
    tags = load_tags(directory_path)
    videos = untagged_videos(directory_path, tags)

    progress['total'] = len(videos)
    progress['done'] = 0
    progress['running'] = True

    for video_name in videos:
        progress['current'] = video_name
        video_path = Path(directory_path) / video_name

        result = tag_video(ollama_url, model, video_path)
        tags[video_name] = {
            **result,
            'model': model,
            'tagged_at': datetime.now(timezone.utc).isoformat(),
        }

        # Save after each video (incremental progress)
        save_tags(directory_path, tags)
        progress['done'] += 1

    progress['running'] = False
    progress['current'] = ''
