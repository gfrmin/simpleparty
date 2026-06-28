"""Frozen CLIP image/text embeddings with a per-video on-disk cache.

This is the only module that imports open_clip. The expensive work — decode
frames, run the CLIP image encoder — happens once per video and is cached under
``.simpleparty/embeddings/<model id>/``. The cache is *content-addressed by the
video's (mtime_ns, size)*: a lookup reconstructs the exact expected filename, so
a hit is guaranteed fresh and a changed/re-encoded video misses automatically.
No shared index file, so concurrent train/suggest jobs need no lock.

torch / open_clip / numpy are imported lazily inside the functions that need
them, so the cache path logic stays importable without a GPU or model weights.
"""

import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

from simpleparty.tagger import (
    SIMPLEPARTY_DIR, VIDEO_EXTENSIONS, _get_duration, _stat_key, extract_keyframes,
)

logger = logging.getLogger('simpleparty.embeddings')

# Backbone is configurable here; the id is part of the cache namespace so
# switching models never mixes feature spaces.
CLIP_ARCH = 'ViT-H-14'
CLIP_PRETRAINED = 'laion2b_s32b_b79k'
CLIP_MODEL_ID = f'{CLIP_ARCH}__{CLIP_PRETRAINED}'

EMBED_SUBDIR = 'embeddings'
DEFAULT_EMBED_FRAMES = 8


# --- Cache paths (pure, no torch/numpy) ---

def embeddings_dir(directory):
    """Directory holding cached embeddings for the active CLIP model."""
    return Path(directory) / SIMPLEPARTY_DIR / EMBED_SUBDIR / CLIP_MODEL_ID


def embed_cache_paths(directory, video_name, stat_key):
    """Return (npy_path, fail_path) for an explicit (mtime_ns, size) stat key."""
    mtime_ns, size = stat_key
    npy = embeddings_dir(directory) / f'{video_name}__{mtime_ns}_{size}.npy'
    return npy, npy.with_suffix('.fail')


def cached_embedding_path(directory, video_name):
    """(npy, fail) paths keyed on the video's *current* stat, or None if the
    video is missing. Existence of `npy` is a guaranteed-fresh cache hit."""
    video_path = Path(directory) / video_name
    stat_key = _stat_key(video_path)
    if stat_key is None:
        return None
    return embed_cache_paths(directory, video_name, stat_key)


def fail_marker_path(directory, video_name):
    """Path of the .fail sentinel for the video's current stat, or None."""
    paths = cached_embedding_path(directory, video_name)
    return paths[1] if paths else None


def _valid_cache_names(directory):
    """Filenames that are fresh for videos currently in the directory."""
    valid = set()
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return valid
    for e in entries:
        if e.name.startswith('.') or Path(e.name).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        stat_key = _stat_key(e.path)
        if stat_key is None:
            continue
        npy, fail = embed_cache_paths(directory, e.name, stat_key)
        valid.add(npy.name)
        valid.add(fail.name)
    return valid


def embedding_coverage(directory):
    """Per-video embedding state for a directory (pure filesystem, no GPU).

    Returns ``{total, embedded, failed, missing, missing_names}`` where each
    video is classified by the *current* stat key: a fresh ``.npy`` is
    embedded, a fresh ``.fail`` is failed (permanently unreadable), anything
    else (never embedded, or only a stale entry from a previous encode) is
    missing. ``missing_names`` is sorted and excludes failed videos so Embed
    never retries a file that cannot be read."""
    total = embedded = failed = 0
    missing_names = []
    try:
        entries = list(os.scandir(directory))
    except OSError:
        entries = []
    for e in entries:
        if e.name.startswith('.') or Path(e.name).suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        stat_key = _stat_key(e.path)
        if stat_key is None:
            continue
        total += 1
        npy, fail = embed_cache_paths(directory, e.name, stat_key)
        if npy.exists():
            embedded += 1
        elif fail.exists():
            failed += 1
        else:
            missing_names.append(e.name)
    missing_names.sort()
    return {
        'total': total, 'embedded': embedded, 'failed': failed,
        'missing': len(missing_names), 'missing_names': missing_names,
    }


def video_is_embedded(directory, video_name):
    """True if the video has a fresh cached embedding (cheap, no GPU)."""
    paths = cached_embedding_path(directory, video_name)
    return bool(paths and paths[0].exists())


def prune_stale_embeddings(directory):
    """Delete cached embeddings/markers that are orphaned (video gone) or stale
    (video changed). Cheap; run at the start of training."""
    edir = embeddings_dir(directory)
    if not edir.is_dir():
        return
    valid = _valid_cache_names(directory)
    removed = 0
    for f in edir.iterdir():
        # Only ever delete finished cache entries; never an in-flight
        # `.emb-*.tmp` that a concurrent _atomic_write is about to os.replace.
        if not (f.name.endswith('.npy') or f.name.endswith('.fail')):
            continue
        if f.name not in valid:
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.debug('pruned %d stale embedding files in %s', removed, edir)


def _atomic_write(path, write_fn):
    """Write via a temp file in the same dir + os.replace (atomic on same fs)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.emb-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as f:
            write_fn(f)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- CLIP model singleton (lazy) ---

_clip = None
_clip_lock = threading.Lock()
# Serializes forward passes: a single shared CUDA module is not safe to drive
# from multiple threads at once (a job thread embedding while an HTTP handler
# suggests). The GPU serializes compute anyway, so this costs almost nothing.
_infer_lock = threading.Lock()


def _require_clip():
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
        import numpy  # noqa: F401
        return
    except ImportError:
        from simpleparty import __version__
        raise RuntimeError(
            'open_clip_torch, torch and numpy are required for tagger features. '
            f'Install with: uvx simpleparty[classifier]=={__version__}'
        )


def _weights_cached():
    """Best-effort probe: have the CLIP weights already been downloaded?"""
    roots = [os.environ.get('HF_HOME'), os.path.expanduser('~/.cache/huggingface')]
    needle = CLIP_PRETRAINED.split('_')[0]  # 'laion2b'
    for root in roots:
        if root and os.path.isdir(root):
            for dirpath, _dirs, files in os.walk(root):
                if any(f.endswith(('.bin', '.safetensors')) for f in files) \
                        and ('CLIP-ViT-H-14' in dirpath or needle in dirpath.lower()):
                    return True
    return False


def clip_model(progress=None):
    """Load (once) and return the CLIP model bundle on the GPU.

    Bundle: {model, preprocess, tokenizer, device, dim, dtype, model_id}.
    """
    global _clip
    with _clip_lock:
        if _clip is not None and _clip['model_id'] == CLIP_MODEL_ID:
            return _clip
        _require_clip()
        import open_clip
        import torch

        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        if device.type != 'cuda':
            logger.warning(
                'CUDA not available — CLIP will run on CPU and embedding a large '
                'library will be very slow. A GPU is strongly recommended.')
            if progress is not None:
                progress['phase'] = 'WARNING: no GPU, CLIP running on CPU (slow)'

        if progress is not None and not _weights_cached():
            progress['phase'] = (
                f'downloading CLIP model {CLIP_ARCH} (~3.9GB, first run only)')
        logger.info('loading CLIP %s (%s) on %s', CLIP_ARCH, CLIP_PRETRAINED, device)

        model, _, preprocess = open_clip.create_model_and_transforms(
            CLIP_ARCH, pretrained=CLIP_PRETRAINED)
        model.eval().to(device)
        use_half = device.type == 'cuda'
        if use_half:
            model = model.half()
        tokenizer = open_clip.get_tokenizer(CLIP_ARCH)
        dim = int(getattr(model.visual, 'output_dim', 1024))

        _clip = {
            'model': model, 'preprocess': preprocess, 'tokenizer': tokenizer,
            'device': device, 'dim': dim, 'half': use_half,
            'model_id': CLIP_MODEL_ID,
        }
        return _clip


def embed_dim(progress=None):
    return clip_model(progress)['dim']


# --- Embedding ---

def embed_images(pil_images, progress=None):
    """Encode PIL images, L2-normalize each, mean-pool, renormalize. -> (D,) float32."""
    import torch
    c = clip_model(progress)
    batch = torch.stack([c['preprocess'](im) for im in pil_images]).to(c['device'])
    if c['half']:
        batch = batch.half()
    with _infer_lock, torch.no_grad():
        feats = c['model'].encode_image(batch)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    pooled = feats.mean(0)
    pooled = pooled / pooled.norm()
    return pooled.float().cpu().numpy()


def embed_paths(paths, progress=None):
    from PIL import Image
    images = [Image.open(p).convert('RGB') for p in paths]
    return embed_images(images, progress=progress)


def embed_texts(prompts, progress=None):
    """Encode text prompts, L2-normalized. -> (C, D) float32."""
    import torch
    c = clip_model(progress)
    tokens = c['tokenizer'](list(prompts)).to(c['device'])
    with _infer_lock, torch.no_grad():
        feats = c['model'].encode_text(tokens)
        feats = feats / feats.norm(dim=-1, keepdim=True)
    return feats.float().cpu().numpy()


def get_video_embedding(directory, video_name, max_frames=DEFAULT_EMBED_FRAMES,
                        progress=None, compute=True):
    """Return the cached (or freshly computed) embedding for a video, or None
    if it has no usable frames. Computing one writes the cache atomically.

    With ``compute=False`` this is a pure cache read: a fresh ``.npy`` is
    returned, but a miss returns None without ever running ffmpeg/CLIP. Used by
    training, which must consume embeddings rather than produce them."""
    import numpy as np
    paths = cached_embedding_path(directory, video_name)
    if paths is None:
        return None
    npy, fail = paths
    if npy.exists():
        try:
            return np.load(npy)
        except (OSError, ValueError):
            pass  # corrupt cache file; recompute
    if fail.exists():
        return None
    if not compute:
        return None

    video_path = str(Path(directory) / video_name)
    # Persist a sentinel only for genuinely unreadable files (no probeable
    # duration) so we don't re-probe them every pass. An empty frame list with a
    # valid duration is treated as transient (ffmpeg timeout / all-dark) and
    # retried on the next run rather than permanently excluded.
    if _get_duration(video_path) <= 0:
        logger.debug('embed: %s -> FAILED, no probeable duration; wrote .fail', video_name)
        _atomic_write(fail, lambda f: None)
        return None

    frames = extract_keyframes(video_path, max_frames=max_frames)
    try:
        if not frames:
            # No usable frames despite a valid duration. We write NO marker, so
            # this video stays "missing" and is retried on the next Embed run.
            # If it recurs every run, the cause is upstream (see the keyframe /
            # duration debug lines above) — frame extraction is failing, not a
            # transient blip.
            logger.debug(
                'embed: %s -> no usable frames; left UNMARKED (will retry next '
                'Embed; if this repeats every run, extraction is failing for '
                'this file)', video_name)
            return None
        emb = embed_paths([str(p) for p in frames], progress=progress).astype('float32')
        _atomic_write(npy, lambda f: np.save(f, emb))
        return emb
    finally:
        if frames:
            shutil.rmtree(frames[0].parent, ignore_errors=True)


def embed_videos(directory, names, max_frames=DEFAULT_EMBED_FRAMES, progress=None):
    """Explicitly compute (and cache) embeddings for the named videos.

    The slow, GPU-bound step, run as a background job. Drives the same
    ``progress`` keys as training's embedding pass so ``/tag-status`` renders it
    unchanged. Cache hits are instant, so this is resumable: embed, drop in new
    videos, embed again — only the new ones do work."""
    if progress is None:
        progress = {}
    try:
        prune_stale_embeddings(directory)
        progress['phase'] = 'embedding videos'
        progress['total'] = len(names)
        progress['done'] = 0
        no_embedding = []
        for i, name in enumerate(names):
            progress['done'] = i
            progress['current'] = name
            emb = get_video_embedding(directory, name, max_frames=max_frames, progress=progress)
            if emb is None:
                no_embedding.append(name)
        progress['done'] = len(names)
        progress['phase'] = 'done'
        if no_embedding:
            # Surfaced at WARNING (visible without --debug) because these videos
            # will keep showing as "missing" every run — the per-file reason is
            # in the DEBUG lines above (no duration / no usable frames).
            logger.warning(
                '%d of %d videos produced no embedding and remain "missing": %s'
                '%s (run with --debug for the per-file reason)',
                len(no_embedding), len(names), ', '.join(no_embedding[:10]),
                '' if len(no_embedding) <= 10 else f', … (+{len(no_embedding) - 10} more)')
    except Exception as e:
        logger.exception('embedding failed')
        progress['error'] = str(e)
    finally:
        progress['running'] = False
