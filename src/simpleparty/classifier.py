"""Train and run a multi-label tagger from frozen CLIP embeddings.

Each video is reduced once to a cached CLIP image embedding (see
``embeddings.py``); training then fits a tiny multi-label ``nn.Linear`` head on
those cached vectors. Because the embedding is the expensive, stable artifact and
the head is disposable, retraining — and the label-cleanup loop below — is cheap.

Robustness to noisy ground-truth labels is built in: k-fold out-of-fold (OOF)
predictions flag confirmed tags the model strongly disagrees with (persisted for
UI review) and auto-clean the most egregious ones out of the final fit. Frozen
features + weight decay + label smoothing keep the head from memorizing noise.
"""

import json
import logging
import time
from collections import Counter
from pathlib import Path

logger = logging.getLogger('simpleparty.classifier')

from simpleparty.tagger import (
    SIMPLEPARTY_DIR, MODEL_FILENAME,
    load_tags, training_entries,
)
from simpleparty.embeddings import (
    CLIP_MODEL_ID, DEFAULT_EMBED_FRAMES,
    embed_texts, get_video_embedding, prune_stale_embeddings,
)

# Templates averaged per tag for zero-shot text prompts.
ZERO_SHOT_TEMPLATES = ('{}', 'a photo of {}', 'a video of {}')
ZERO_SHOT_MIN_SIM = 0.20

SUSPECT_FILENAME = 'suspect_tags.json'

# Noise thresholds. A label whose leak-free OOF prediction confidently
# contradicts its given value is both surfaced for review (suspect badge) and
# excluded from the training signal (auto-clean). We only ever touch the
# training mask — never tags.json — so the user still sees and decides on every
# flagged label. The positive floor stops auto-clean from erasing a rare tag.
NOISE_FLAG_LOW = 0.10
NOISE_FLAG_HIGH = 0.90
MIN_POSITIVES_FLOOR = 2   # never auto-drop a tag below this many positives


class RetrainRequired(RuntimeError):
    """Raised when a saved model is incompatible and must be retrained."""


def _require_torch():
    try:
        import torch
        import torchvision  # kept in the extra; harmless to require
        return torch, torchvision
    except ImportError:
        from simpleparty import __version__
        raise RuntimeError(
            'torch and torchvision are required for tagger features. '
            f'Install with: uvx simpleparty[classifier]=={__version__}'
        )


# --- Vocabulary (unchanged behavior) ---

def build_vocabulary(tags_data, min_count=5):
    """Sorted list of confirmed tags appearing >= min_count times."""
    counts = Counter()
    for entry in tags_data.values():
        if entry.get('status', 'confirmed') == 'suggested':
            continue
        for tag in entry.get('tags', []):
            key = tag.lower().strip()
            if key:
                counts[key] += 1
    return sorted(tag for tag, count in counts.items() if count >= min_count)


def _encode_tags(tags, vocab, tag_to_idx, rejected_tags=()):
    """(target, mask) tensors for one confirmed video.

    Assume-absent: every vocab tag is a known label (mask=1). Confirmed tags are
    positives (target=1); everything else — including explicitly rejected tags —
    is a negative (target=0). This is the standard fixed-vocabulary multi-label
    setup; the auto-clean pass removes confident false-negatives so genuinely
    under-tagged videos don't poison training. `rejected_tags` is accepted for
    signature stability (already negatives by default)."""
    import torch
    target = torch.zeros(len(vocab))
    mask = torch.ones(len(vocab))
    for tag in tags:
        key = tag.lower().strip()
        if key in tag_to_idx:
            target[tag_to_idx[key]] = 1.0
    return target, mask


def _pos_weights_from_tensors(Y, M, cap=10.0):
    """Per-tag positive class weights (neg/pos) over known (masked) positions."""
    pos = (Y * M).sum(dim=0)
    known = M.sum(dim=0)
    neg = known - pos
    return (neg.clamp(min=1) / pos.clamp(min=1)).clamp(max=cap)


# --- Model: tiny linear head ---

def _build_head(dim, num_classes):
    """Multi-label linear probe head over frozen CLIP features."""
    torch, _ = _require_torch()
    return torch.nn.Sequential(
        torch.nn.Dropout(0.1),
        torch.nn.Linear(dim, num_classes),
    )


def save_model(path, head, vocab, thresholds, embed_dim):
    """Persist a v2 checkpoint (head weights + metadata)."""
    torch, _ = _require_torch()
    torch.save({
        'head_state_dict': head.state_dict(),
        'vocab': list(vocab),
        'thresholds': list(thresholds),
        'clip_model_id': CLIP_MODEL_ID,
        'embed_dim': int(embed_dim),
        'num_classes': len(vocab),
        'format_version': 2,
    }, str(path))


_loaded_model = None


def load_model(model_path):
    """Load a v2 checkpoint for inference (cached singleton).

    Raises RetrainRequired for legacy EfficientNet checkpoints or a backbone
    mismatch — the embedding feature space would otherwise be incompatible.
    """
    global _loaded_model
    torch, _ = _require_torch()
    if _loaded_model and _loaded_model.get('path') == model_path:
        return _loaded_model

    ckpt = torch.load(model_path, map_location='cpu', weights_only=False)
    if 'clip_model_id' not in ckpt or ckpt.get('format_version', 1) < 2:
        raise RetrainRequired(
            'This model was trained by an older version (image backbone). '
            'Retrain it — training is now fast.')
    if ckpt['clip_model_id'] != CLIP_MODEL_ID:
        raise RetrainRequired(
            f"Model was trained with {ckpt['clip_model_id']} but the current "
            f"backbone is {CLIP_MODEL_ID}. Retrain to use the new backbone.")

    dim = ckpt['embed_dim']
    vocab = ckpt['vocab']
    head = _build_head(dim, len(vocab))
    head.load_state_dict(ckpt['head_state_dict'])
    head.eval()

    _loaded_model = {
        'path': model_path, 'head': head, 'vocab': vocab,
        'thresholds': ckpt['thresholds'], 'clip_model_id': ckpt['clip_model_id'],
        'embed_dim': dim,
    }
    return _loaded_model


# --- Splits ---

def _per_video_split(names, frac=0.8, seed=42):
    """Deterministic per-video index split. Returns (train_idx, val_idx)."""
    import random
    idx = list(range(len(names)))
    random.Random(seed).shuffle(idx)
    split = int(len(idx) * frac)
    return sorted(idx[:split]), sorted(idx[split:])


def _kfold_indices(n, k, seed=42):
    """List of k validation-index lists covering 0..n-1 disjointly."""
    import random
    idx = list(range(n))
    random.Random(seed).shuffle(idx)
    k = max(2, min(k, n))
    return [sorted(idx[f::k]) for f in range(k)]


# --- Loss / fitting ---

def _bce_masked(logits, targets, masks, pos_weight, smoothing=0.05):
    import torch
    t = targets * (1.0 - smoothing) + 0.5 * smoothing
    raw = torch.nn.functional.binary_cross_entropy_with_logits(
        logits, t, pos_weight=pos_weight, reduction='none')
    return (raw * masks).sum() / masks.sum().clamp(min=1)


def _fit_head(X, Y, M, dim, num_classes, pos_weight, *, epochs=500, lr=3e-3,
              weight_decay=1e-2, smoothing=0.05, val=None):
    """Full-batch train a linear head; if val=(Xv,Yv,Mv), keep best-on-val."""
    import torch
    head = _build_head(dim, num_classes).to(X.device)
    opt = torch.optim.AdamW(head.parameters(), lr=lr, weight_decay=weight_decay)
    best_state, best_val = None, float('inf')
    for _ in range(epochs):
        head.train()
        opt.zero_grad()
        loss = _bce_masked(head(X), Y, M, pos_weight, smoothing)
        loss.backward()
        opt.step()
        if val is not None:
            Xv, Yv, Mv = val
            head.eval()
            with torch.no_grad():
                vl = _bce_masked(head(Xv), Yv, Mv, pos_weight, smoothing=0.0).item()
            if vl < best_val:
                best_val, best_state = vl, {k: v.clone() for k, v in head.state_dict().items()}
    if best_state is not None:
        head.load_state_dict(best_state)
    head.eval()
    return head


def _kfold_oof_probs(X, Y, M, dim, num_classes, pos_weight, k=5, seed=42):
    """Out-of-fold sigmoid probabilities (N, C) — leak-free predictions."""
    import torch
    n = X.shape[0]
    probs = torch.zeros(n, num_classes, device=X.device)
    for val_idx in _kfold_indices(n, k, seed):
        val_set = set(val_idx)
        train_idx = [i for i in range(n) if i not in val_set]
        if not train_idx or not val_idx:
            continue
        ti = torch.tensor(train_idx, device=X.device)
        vi = torch.tensor(val_idx, device=X.device)
        # These heads are only used to *detect* noise, never deployed, so they
        # run sharp: no label smoothing and light weight decay. Smoothing/strong
        # decay would floor the probabilities and mask confident disagreements.
        head = _fit_head(X[ti], Y[ti], M[ti], dim, num_classes, pos_weight,
                         epochs=400, smoothing=0.0, weight_decay=1e-3)
        with torch.no_grad():
            probs[vi] = torch.sigmoid(head(X[vi]))
    return probs


# --- Thresholds ---

def _best_f1_threshold(probs_i, labels_i):
    """Sweep thresholds for one tag; return (best_threshold)."""
    best_f1, best_t = 0.0, 0.5
    for t in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        pred = (probs_i >= t).astype('float32')
        tp = float((pred * labels_i).sum())
        fp = float((pred * (1 - labels_i)).sum())
        fn = float(((1 - pred) * labels_i).sum())
        precision = tp / (tp + fp + 1e-8)
        recall = tp / (tp + fn + 1e-8)
        f1 = 2 * precision * recall / (precision + recall + 1e-8)
        if f1 > best_f1:
            best_f1, best_t = f1, t
    return best_t


def _thresholds_from_probs(probs, targets, masks, vocab):
    """Per-tag F1-optimal thresholds from (leak-free OOF) probabilities."""
    import numpy as np
    probs = np.asarray(probs)
    targets = np.asarray(targets)
    masks = np.asarray(masks)
    thresholds = []
    for c in range(len(vocab)):
        m = masks[:, c] > 0.5
        if not m.any():
            thresholds.append(0.5)
            continue
        thresholds.append(_best_f1_threshold(probs[m, c], targets[m, c]))
    return thresholds


# --- Label-noise scoring (pure) ---

def score_label_noise(probs, targets, masks, vocab, names,
                      low=NOISE_FLAG_LOW, high=NOISE_FLAG_HIGH):
    """Suspect labels: confirmed positives the model thinks absent, or
    rejected/negatives it thinks present. Returns {video_name: [{tag,given,prob}]}."""
    import numpy as np
    probs = np.asarray(probs)
    targets = np.asarray(targets)
    masks = np.asarray(masks)
    out = {}
    for i in range(probs.shape[0]):
        items = []
        for c in range(probs.shape[1]):
            if masks[i, c] <= 0.5:
                continue
            given = int(round(float(targets[i, c])))
            p = float(probs[i, c])
            if given == 1 and p < low:
                items.append({'tag': vocab[c], 'given': 1, 'prob': p})
            elif given == 0 and p > high:
                items.append({'tag': vocab[c], 'given': 0, 'prob': p})
        if items:
            out[names[i]] = items
    return out


def _auto_clean_mask(M, probs, targets, vocab):
    """Zero the mask at flagged (confidently-disagreeing) positions so they don't
    train the head. Positives are protected by a per-tag floor; bad negatives
    drop freely. Mutates and returns M (a tensor). Returns (M, dropped_count)."""
    pos_per_tag = (targets * (M > 0.5).float()).sum(dim=0)
    dropped = 0
    n, c = probs.shape
    for i in range(n):
        for j in range(c):
            if M[i, j] <= 0.5:
                continue
            given = int(round(float(targets[i, j])))
            p = float(probs[i, j])
            if given == 1 and p < NOISE_FLAG_LOW:
                if pos_per_tag[j] > MIN_POSITIVES_FLOOR:
                    M[i, j] = 0.0
                    pos_per_tag[j] -= 1
                    dropped += 1
            elif given == 0 and p > NOISE_FLAG_HIGH:
                M[i, j] = 0.0
                dropped += 1
    return M, dropped


def _save_suspects(directory, suspects):
    sp = Path(directory) / SIMPLEPARTY_DIR
    sp.mkdir(exist_ok=True)
    path = sp / SUSPECT_FILENAME
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=str(sp), prefix='.suspect-', suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        json.dump(suspects, f, indent=2, ensure_ascii=False)
    os.replace(tmp, str(path))


def load_suspects(directory):
    """Saved suspect-label map for a directory, or {}."""
    path = Path(directory) / SIMPLEPARTY_DIR / SUSPECT_FILENAME
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


# --- Build embeddings for the training set ---

def build_training_embeddings(directory, tags_data, max_frames=DEFAULT_EMBED_FRAMES, progress=None):
    """Embed every training video (cache hits are instant). Returns manifest of
    (video_name, embedding_np, tags, rejected). Videos with no usable frames are
    skipped."""
    t0 = time.monotonic()
    entries = training_entries(tags_data)
    items = [(name, e) for name, e in entries.items()
             if (Path(directory) / name).exists()
             and (e.get('tags') or e.get('rejected_tags'))]

    if progress is not None:
        progress['phase'] = 'embedding videos'
        progress['total'] = len(items)
        progress['done'] = 0

    manifest = []
    for i, (name, entry) in enumerate(items):
        if progress is not None:
            progress['done'] = i
            progress['current'] = name
        emb = get_video_embedding(directory, name, max_frames=max_frames, progress=progress)
        if emb is None:
            continue
        manifest.append((name, emb, entry.get('tags', []), entry.get('rejected_tags', [])))

    if progress is not None:
        progress['done'] = len(items)
    logger.debug('built %d/%d training embeddings (%.1fs)',
                 len(manifest), len(items), time.monotonic() - t0)
    return manifest


# --- Training ---

def train(directory, max_frames=DEFAULT_EMBED_FRAMES, min_tag_count=5, progress=None):
    """Train a linear-probe tagger from confirmed tags in a directory."""
    if progress is None:
        progress = {}
    try:
        _train_inner(directory, max_frames, min_tag_count, progress)
    except Exception as e:
        logger.exception('training failed')
        progress['error'] = str(e)
        progress['running'] = False


def _train_inner(directory, max_frames, min_tag_count, progress):
    import numpy as np
    progress['phase'] = 'loading'
    torch, _ = _require_torch()

    tags_data = load_tags(directory)
    vocab = build_vocabulary(tags_data, min_count=min_tag_count)
    if len(vocab) < 2:
        progress['error'] = f'Need at least 2 tags with >= {min_tag_count} occurrences'
        progress['running'] = False
        return
    tag_to_idx = {tag: i for i, tag in enumerate(vocab)}
    progress['vocab_size'] = len(vocab)

    prune_stale_embeddings(directory)
    manifest = build_training_embeddings(directory, tags_data, max_frames=max_frames, progress=progress)
    if len(manifest) < 8:
        progress['error'] = f'Only {len(manifest)} videos embedded, need at least 8'
        progress['running'] = False
        return

    progress['phase'] = 'fitting head'
    names = [m[0] for m in manifest]
    dim = int(manifest[0][1].shape[0])
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    X = torch.from_numpy(np.stack([m[1] for m in manifest])).float().to(device)
    Y = torch.zeros(len(manifest), len(vocab), device=device)
    M = torch.zeros(len(manifest), len(vocab), device=device)
    for i, (_, _, tags, rejected) in enumerate(manifest):
        target, mask = _encode_tags(tags, vocab, tag_to_idx, rejected)
        Y[i] = target.to(device)
        M[i] = mask.to(device)

    pos_weight = _pos_weights_from_tensors(Y, M).to(device)

    # Leak-free OOF predictions drive both noise detection and thresholds.
    progress['phase'] = 'detecting label noise'
    oof = _kfold_oof_probs(X, Y, M, dim, len(vocab), pos_weight)
    oof_np = oof.cpu().numpy()
    Y_np, M_np = Y.cpu().numpy(), M.cpu().numpy()

    suspects = score_label_noise(oof_np, Y_np, M_np, vocab, names)
    _save_suspects(directory, suspects)
    progress['suspect_count'] = sum(len(v) for v in suspects.values())

    # Auto-clean the most egregious labels out of the final fit.
    M_clean, dropped = _auto_clean_mask(M.clone(), oof, Y, vocab)
    progress['cleaned_count'] = dropped

    # Final head on all (cleaned) data, with a per-video holdout for early stop.
    progress['phase'] = 'fitting head'
    train_idx, val_idx = _per_video_split(names)
    ti = torch.tensor(train_idx, device=device)
    vi = torch.tensor(val_idx, device=device)
    val = (X[vi], Y[vi], M_clean[vi]) if len(val_idx) else None
    head = _fit_head(X[ti] if val else X, Y[ti] if val else Y,
                     M_clean[ti] if val else M_clean,
                     dim, len(vocab), pos_weight, val=val)

    progress['phase'] = 'tuning thresholds'
    thresholds = _thresholds_from_probs(oof_np, Y_np, M_np, vocab)

    sp_dir = Path(directory) / SIMPLEPARTY_DIR
    sp_dir.mkdir(exist_ok=True)
    save_path = sp_dir / MODEL_FILENAME
    save_model(str(save_path), head.cpu(), vocab, thresholds, dim)

    global _loaded_model
    _loaded_model = None  # invalidate inference cache

    progress['phase'] = 'done'
    progress['running'] = False
    progress['model_path'] = str(save_path)
    progress['current'] = (
        f'Saved model with {len(vocab)} tags; '
        f'{progress.get("suspect_count", 0)} suspect labels, {dropped} auto-cleaned')


# --- Inference ---

def suggest_for_video(video_path, model_path, max_tags=10):
    """Suggest tags for a single video. Returns list of (tag, confidence)."""
    import torch
    info = load_model(model_path)
    directory = str(Path(video_path).parent)
    name = Path(video_path).name
    emb = get_video_embedding(directory, name)
    if emb is None:
        return []
    x = torch.from_numpy(emb).float().unsqueeze(0)
    with torch.no_grad():
        probs = torch.sigmoid(info['head'](x))[0]
    results = []
    for i, (prob, thresh) in enumerate(zip(probs.tolist(), info['thresholds'])):
        if prob >= thresh:
            results.append((info['vocab'][i], prob))
    return sorted(results, key=lambda x: -x[1])[:max_tags]


def suggest_for_directory(directory, model_path, progress=None, max_tags=10):
    """Suggest tags for all untagged videos in a directory."""
    if progress is None:
        progress = {}
    try:
        _suggest_inner(directory, model_path, progress, max_tags)
    except Exception as e:
        logger.exception('suggestion failed')
        progress['error'] = str(e)
        progress['running'] = False


_SUGGEST_FLUSH_COUNT = 10
_SUGGEST_FLUSH_SECONDS = 60


def _merge_suggestions(pending):
    """Transform that writes only its own suggestion entries; never clobbers a
    video the user confirmed while the job ran."""
    def _apply(tags):
        for name, fields in pending.items():
            current = tags.get(name, {})
            if current.get('status') == 'confirmed' and current.get('tags'):
                continue
            tags[name] = {**current, **fields}
        return tags
    return _apply


def _suggest_inner(directory, model_path, progress, max_tags):
    def suggest_fn(video_path):
        return suggest_for_video(video_path, model_path, max_tags=max_tags)
    _run_suggest_pass(directory, suggest_fn, progress)


def _run_suggest_pass(directory, suggest_fn, progress):
    """Tag every untagged video via suggest_fn(video_path)->[(tag,conf)], writing
    status='suggested' entries in batches. Shared by supervised and zero-shot."""
    from simpleparty.tagger import untagged_videos, update_tags

    tags_data = load_tags(directory)
    videos = untagged_videos(directory, tags_data)

    progress['phase'] = 'suggesting'
    progress['total'] = len(videos)
    progress['done'] = 0
    progress['running'] = True

    pending = {}
    last_flush = time.monotonic()

    def flush():
        nonlocal pending, last_flush
        if pending:
            update_tags(directory, _merge_suggestions(pending))
            pending = {}
        last_flush = time.monotonic()

    try:
        for video_name in videos:
            progress['current'] = video_name
            video_path = Path(directory) / video_name
            results = suggest_fn(str(video_path))
            if results:
                avg_conf = sum(c for _, c in results) / len(results)
                pending[video_name] = {
                    'tags': [tag for tag, _ in results],
                    'status': 'suggested',
                    'confidence': round(avg_conf, 3),
                }
            if len(pending) >= _SUGGEST_FLUSH_COUNT or time.monotonic() - last_flush > _SUGGEST_FLUSH_SECONDS:
                flush()
            progress['done'] += 1
    finally:
        flush()

    progress['running'] = False
    progress['current'] = ''


# --- Zero-shot (no trained head) ---

def candidate_vocabulary(tags_data):
    """All confirmed tags (any count) — the label space for zero-shot."""
    return build_vocabulary(tags_data, min_count=1)


def _zero_shot_rank(image_emb, text_embs, vocab, max_tags=10, min_sim=ZERO_SHOT_MIN_SIM):
    """Rank vocab tags by cosine similarity of their text prompt to the video
    embedding. Inputs are L2-normalized; cosine = dot product."""
    import numpy as np
    sims = np.asarray(text_embs) @ np.asarray(image_emb)
    out = []
    for idx in np.argsort(-sims):
        s = float(sims[idx])
        if s < min_sim:
            break
        out.append((vocab[idx], s))
        if len(out) >= max_tags:
            break
    return out


def zero_shot_text_matrix(vocab, templates=ZERO_SHOT_TEMPLATES, progress=None):
    """(C, D) matrix: per tag, mean of its template prompt embeddings, normalized."""
    import numpy as np
    rows = []
    for tag in vocab:
        embs = embed_texts([t.format(tag) for t in templates], progress=progress)
        v = embs.mean(0)
        v = v / (np.linalg.norm(v) + 1e-8)
        rows.append(v)
    return np.stack(rows).astype('float32')


def zero_shot_suggest_for_video(video_path, vocab=None, max_tags=10, min_sim=ZERO_SHOT_MIN_SIM):
    """Suggest tags for a single video with no trained head."""
    directory = str(Path(video_path).parent)
    name = Path(video_path).name
    if vocab is None:
        vocab = candidate_vocabulary(load_tags(directory))
    if not vocab:
        return []
    emb = get_video_embedding(directory, name)
    if emb is None:
        return []
    text = zero_shot_text_matrix(vocab)
    return _zero_shot_rank(emb, text, vocab, max_tags=max_tags, min_sim=min_sim)


def zero_shot_suggest_for_directory(directory, progress=None, max_tags=10, min_sim=ZERO_SHOT_MIN_SIM):
    """Cold-start: zero-shot tag every untagged video using the directory's
    confirmed-tag vocabulary. No trained model needed."""
    if progress is None:
        progress = {}
    try:
        import numpy as np
        vocab = candidate_vocabulary(load_tags(directory))
        if len(vocab) < 1:
            progress['error'] = 'No confirmed tags to use as zero-shot labels'
            progress['running'] = False
            return
        progress['phase'] = 'embedding tag prompts'
        text = zero_shot_text_matrix(vocab, progress=progress)

        def suggest_fn(video_path):
            d = str(Path(video_path).parent)
            n = Path(video_path).name
            emb = get_video_embedding(d, n)
            if emb is None:
                return []
            return _zero_shot_rank(emb, text, vocab, max_tags=max_tags, min_sim=min_sim)

        _run_suggest_pass(directory, suggest_fn, progress)
    except Exception as e:
        logger.exception('zero-shot suggestion failed')
        progress['error'] = str(e)
        progress['running'] = False
