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
PROMPT_FILENAME = '.simpleparty-prompt.txt'

SYSTEM_PROMPT = 'You tag images. Output comma-separated tags only, nothing else.'

USER_PROMPT = (
    'Tag this image. 1-3 words per tag, comma-separated. '
    'Cover: scene type, people, ethnicity, gender, age, body type, hair, '
    'clothing, actions, poses, setting, objects, mood.'
)


def load_custom_prompt(directory_path):
    """Load per-directory prompt override, or return the default."""
    prompt_file = Path(directory_path) / PROMPT_FILENAME
    if prompt_file.is_file():
        try:
            text = prompt_file.read_text().strip()
            if text:
                return text
        except OSError:
            pass
    return USER_PROMPT


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


# --- Ollama integration ---

def _ollama_chat(ollama_url, model, system, user, images=None):
    """Send a chat request to Ollama and return the response text."""
    import re
    msg = {'role': 'user', 'content': f'/no_think\n{user}'}
    if images:
        msg['images'] = images
    payload = json.dumps({
        'model': model,
        'messages': [
            {'role': 'system', 'content': system},
            msg,
        ],
        'stream': False,
        'think': False,
        'options': {'num_predict': 4096},
    }).encode()

    req = urllib.request.Request(
        f'{ollama_url}/api/chat',
        data=payload,
        headers={'Content-Type': 'application/json'},
    )
    resp = urllib.request.urlopen(req, timeout=300)
    result = json.loads(resp.read())
    content = result.get('message', {}).get('content', '')
    # Strip <think> blocks from models that emit them
    return re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()


def tag_frame(ollama_url, model, image_path, user_prompt=None):
    """Tag a single frame. Returns list of tags. Retries once on empty response."""
    with open(image_path, 'rb') as f:
        img_b64 = base64.b64encode(f.read()).decode()

    for _ in range(2):
        try:
            content = _ollama_chat(
                ollama_url, model, SYSTEM_PROMPT,
                user_prompt or USER_PROMPT, images=[img_b64],
            )
            if content:
                return [t.strip().rstrip('.') for t in content.split(',') if t.strip()]
        except (urllib.error.URLError, OSError):
            pass
    return []


def tag_video(ollama_url, model, video_path, user_prompt=None, max_frames=1):
    """Tag a video by extracting keyframes and tagging each. Returns {tags}."""
    frames = extract_keyframes(video_path, max_frames=max_frames)
    tmpdir = frames[0].parent if frames else None

    try:
        if not frames:
            return {'tags': []}

        all_tags = []
        for frame in frames:
            all_tags.extend(tag_frame(ollama_url, model, frame, user_prompt=user_prompt))

        # Deduplicate tags, case-insensitive, preserving first occurrence's casing
        seen = set()
        unique_tags = []
        for tag in all_tags:
            key = tag.lower().strip()
            if key and key not in seen:
                seen.add(key)
                unique_tags.append(tag.strip())

        return {'tags': unique_tags}

    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def tag_directory(ollama_url, model, directory_path, progress, max_frames=1):
    """Tag all untagged videos in a directory.

    Args:
        progress: a dict that will be mutated with
                  {'running': bool, 'done': int, 'total': int, 'current': str}
    """
    tags = load_tags(directory_path)
    videos = untagged_videos(directory_path, tags)
    user_prompt = load_custom_prompt(directory_path)

    progress['total'] = len(videos)
    progress['done'] = 0
    progress['running'] = True

    for video_name in videos:
        progress['current'] = video_name
        video_path = Path(directory_path) / video_name

        result = tag_video(ollama_url, model, video_path, user_prompt=user_prompt, max_frames=max_frames)
        tags[video_name] = {
            **result,
            'model': model,
            'tagged_at': datetime.now(timezone.utc).isoformat(),
        }

        save_tags(directory_path, tags)
        progress['done'] += 1

    progress['running'] = False
    progress['current'] = ''
