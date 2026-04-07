"""Train and run a multi-label image classifier for video tagging.

Trains an EfficientNet-B0 on confirmed tags, then suggests tags for
untagged videos. Generic — works with any tag vocabulary.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from simpleparty.tagger import (
    SIMPLEPARTY_DIR, MODEL_FILENAME, VIDEO_EXTENSIONS,
    _get_duration, _is_dark_frame,
    confirmed_entries, load_tags, model_path, save_tags,
)

FRAMES_DIR = SIMPLEPARTY_DIR + '/frames'


def _require_torch():
    """Import and return torch + torchvision, raising a clear error if missing."""
    try:
        import torch
        import torchvision
        return torch, torchvision
    except ImportError:
        raise RuntimeError(
            'torch and torchvision are required for classifier features. '
            'Install with: uv add torch torchvision'
        )


# --- Vocabulary ---

def build_vocabulary(tags_data, min_count=5):
    """Build tag vocabulary from confirmed tag entries.

    Returns sorted list of tags appearing >= min_count times.
    """
    from collections import Counter
    counts = Counter()
    for entry in tags_data.values():
        if entry.get('status', 'confirmed') == 'suggested':
            continue
        for tag in entry.get('tags', []):
            key = tag.lower().strip()
            if key:
                counts[key] += 1

    vocab = sorted(tag for tag, count in counts.items() if count >= min_count)
    return vocab


def _encode_tags(tags, vocab, tag_to_idx):
    """Encode a tag list as a binary vector."""
    import torch
    vec = torch.zeros(len(vocab))
    for tag in tags:
        key = tag.lower().strip()
        if key in tag_to_idx:
            vec[tag_to_idx[key]] = 1.0
    return vec


# --- Frame extraction ---

def _extract_single_frame(video_path, position, out_path):
    """Extract a single frame at a given position (seconds)."""
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


def extract_training_frames(directory, tags_data, max_frames=1, progress=None):
    """Extract frames for all confirmed videos. Returns manifest list.

    Each manifest entry: (frame_path, tags_list).
    Frames saved to {directory}/.simpleparty-frames/.
    """
    frames_dir = Path(directory) / FRAMES_DIR
    frames_dir.mkdir(exist_ok=True)

    confirmed = confirmed_entries(tags_data)
    manifest = []
    total = len(confirmed)

    for i, (video_name, entry) in enumerate(confirmed.items()):
        if progress:
            progress['phase'] = 'extracting frames'
            progress['done'] = i
            progress['total'] = total
            progress['current'] = video_name

        video_path = Path(directory) / video_name
        if not video_path.exists():
            continue

        tags = entry.get('tags', [])
        if not tags:
            continue

        duration = _get_duration(video_path)
        if duration <= 0:
            continue

        positions = [duration * (j + 1) / (max_frames + 1) for j in range(max_frames)]

        for frame_idx, pos in enumerate(positions):
            frame_name = f'{video_name}.f{frame_idx}.jpg'
            frame_path = frames_dir / frame_name

            if not frame_path.exists():
                ok = _extract_single_frame(video_path, pos, frame_path)
                if not ok:
                    continue

            if frame_path.exists():
                manifest.append((str(frame_path), tags))

    if progress:
        progress['done'] = total

    return manifest


# --- Dataset ---

def _build_dataset(manifest, vocab, tag_to_idx, train=True):
    """Build a PyTorch Dataset from the manifest."""
    torch, torchvision = _require_torch()
    from torchvision import transforms

    if train:
        transform = transforms.Compose([
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ColorJitter(0.2, 0.2, 0.2, 0.1),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])
    else:
        transform = transforms.Compose([
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ])

    class FrameDataset(torch.utils.data.Dataset):
        def __init__(self, items, tf):
            self.items = items
            self.tf = tf

        def __len__(self):
            return len(self.items)

        def __getitem__(self, idx):
            from PIL import Image
            path, tags = self.items[idx]
            img = Image.open(path).convert('RGB')
            img = self.tf(img)
            label = _encode_tags(tags, vocab, tag_to_idx)
            return img, label

    return FrameDataset(manifest, transform)


# --- Model ---

def _build_model(num_classes, freeze_backbone=True):
    """Build EfficientNet-B0 with a multi-label head."""
    torch, torchvision = _require_torch()
    from torchvision.models import efficientnet_b0, EfficientNet_B0_Weights

    model = efficientnet_b0(weights=EfficientNet_B0_Weights.IMAGENET1K_V1)
    if freeze_backbone:
        for param in model.features.parameters():
            param.requires_grad = False

    in_features = model.classifier[1].in_features
    model.classifier = torch.nn.Sequential(
        torch.nn.Dropout(0.3),
        torch.nn.Linear(in_features, num_classes),
    )
    return model


def _compute_pos_weights(manifest, vocab, tag_to_idx, cap=10.0):
    """Compute positive class weights for BCEWithLogitsLoss."""
    torch, _ = _require_torch()
    pos_counts = torch.zeros(len(vocab))
    total = len(manifest)
    for _, tags in manifest:
        for tag in tags:
            key = tag.lower().strip()
            if key in tag_to_idx:
                pos_counts[tag_to_idx[key]] += 1
    neg_counts = total - pos_counts
    weights = neg_counts / pos_counts.clamp(min=1)
    return weights.clamp(max=cap)


def _find_thresholds(model, val_loader, vocab, device):
    """Find per-tag optimal thresholds on validation set."""
    torch, _ = _require_torch()
    model.eval()
    all_probs = []
    all_labels = []
    with torch.no_grad():
        for imgs, labels in val_loader:
            imgs = imgs.to(device)
            logits = model(imgs)
            probs = torch.sigmoid(logits).cpu()
            all_probs.append(probs)
            all_labels.append(labels)

    all_probs = torch.cat(all_probs)
    all_labels = torch.cat(all_labels)

    thresholds = []
    for i in range(len(vocab)):
        best_f1, best_t = 0.0, 0.5
        for t in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
            pred = (all_probs[:, i] >= t).float()
            tp = (pred * all_labels[:, i]).sum()
            fp = (pred * (1 - all_labels[:, i])).sum()
            fn = ((1 - pred) * all_labels[:, i]).sum()
            precision = tp / (tp + fp + 1e-8)
            recall = tp / (tp + fn + 1e-8)
            f1 = 2 * precision * recall / (precision + recall + 1e-8)
            if f1 > best_f1:
                best_f1, best_t = f1.item(), t
        thresholds.append(best_t)

    return thresholds


# --- Training ---

def train(directory, max_frames=1, min_tag_count=5, progress=None):
    """Train a classifier from confirmed tags in a directory.

    Saves model to {directory}/.simpleparty-model.pt.
    Progress dict is mutated with phase/done/total/current updates.
    """
    if progress is None:
        progress = {}

    progress['phase'] = 'loading PyTorch'
    torch, _ = _require_torch()

    tags_data = load_tags(directory)
    vocab = build_vocabulary(tags_data, min_count=min_tag_count)
    if len(vocab) < 2:
        progress['error'] = f'Need at least 2 tags with >= {min_tag_count} occurrences'
        progress['running'] = False
        return

    tag_to_idx = {tag: i for i, tag in enumerate(vocab)}
    progress['phase'] = 'building vocabulary'
    progress['vocab_size'] = len(vocab)

    # Extract frames
    manifest = extract_training_frames(directory, tags_data, max_frames=max_frames, progress=progress)
    if len(manifest) < 10:
        progress['error'] = f'Only {len(manifest)} frames extracted, need at least 10'
        progress['running'] = False
        return

    # Train/val split (80/20 by video)
    import random
    random.seed(42)
    shuffled = list(manifest)
    random.shuffle(shuffled)
    split = int(len(shuffled) * 0.8)
    train_items, val_items = shuffled[:split], shuffled[split:]

    train_ds = _build_dataset(train_items, vocab, tag_to_idx, train=True)
    val_ds = _build_dataset(val_items, vocab, tag_to_idx, train=False)
    train_loader = torch.utils.data.DataLoader(train_ds, batch_size=64, shuffle=True, num_workers=2)
    val_loader = torch.utils.data.DataLoader(val_ds, batch_size=64, shuffle=False, num_workers=2)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    pos_weights = _compute_pos_weights(train_items, vocab, tag_to_idx).to(device)
    criterion = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weights)

    # Phase 1: frozen backbone
    model = _build_model(len(vocab), freeze_backbone=True).to(device)
    optimizer = torch.optim.Adam(
        (p for p in model.parameters() if p.requires_grad), lr=1e-3,
    )

    progress['phase'] = 'training (frozen backbone)'
    for epoch in range(10):
        model.train()
        total_loss = 0
        for imgs, labels in train_loader:
            imgs, labels = imgs.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = criterion(model(imgs), labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()
        progress['done'] = epoch + 1
        progress['total'] = 20
        progress['current'] = f'phase1 epoch {epoch+1}/10 loss={total_loss/len(train_loader):.4f}'

    # Phase 2: unfreeze last 2 blocks
    progress['phase'] = 'training (fine-tuning)'
    for param in model.features[-2:].parameters():
        param.requires_grad = True
    optimizer = torch.optim.Adam(
        (p for p in model.parameters() if p.requires_grad), lr=1e-4,
    )
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=3)

    best_val_loss = float('inf')
    patience_counter = 0

    for epoch in range(10):
        model.train()
        total_loss = 0
        for imgs, labels in train_loader:
            imgs, labels = imgs.to(device), labels.to(device)
            optimizer.zero_grad()
            loss = criterion(model(imgs), labels)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        # Validate
        model.eval()
        val_loss = 0
        with torch.no_grad():
            for imgs, labels in val_loader:
                imgs, labels = imgs.to(device), labels.to(device)
                val_loss += criterion(model(imgs), labels).item()
        val_loss /= max(len(val_loader), 1)
        scheduler.step(val_loss)

        progress['done'] = 10 + epoch + 1
        progress['current'] = f'phase2 epoch {epoch+1}/10 val_loss={val_loss:.4f}'

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            patience_counter = 0
        else:
            patience_counter += 1
            if patience_counter >= 5:
                break

    # Find per-tag thresholds
    progress['phase'] = 'tuning thresholds'
    thresholds = _find_thresholds(model, val_loader, vocab, device)

    # Save model
    sp_dir = Path(directory) / SIMPLEPARTY_DIR
    sp_dir.mkdir(exist_ok=True)
    save_path = sp_dir / MODEL_FILENAME
    torch.save({
        'model_state_dict': model.cpu().state_dict(),
        'vocab': vocab,
        'thresholds': thresholds,
        'num_classes': len(vocab),
    }, str(save_path))

    progress['phase'] = 'done'
    progress['running'] = False
    progress['model_path'] = str(save_path)
    progress['current'] = f'Saved model with {len(vocab)} tags'


# --- Inference ---

_loaded_model = None


def load_model(model_path):
    """Load a trained model for inference. Cached as singleton."""
    global _loaded_model
    torch, torchvision = _require_torch()
    from torchvision import transforms

    if _loaded_model and _loaded_model.get('path') == model_path:
        return _loaded_model

    checkpoint = torch.load(model_path, map_location='cpu', weights_only=False)
    vocab = checkpoint['vocab']
    model = _build_model(len(vocab), freeze_backbone=False)
    model.load_state_dict(checkpoint['model_state_dict'])
    model.eval()

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    transform = transforms.Compose([
        transforms.Resize(256),
        transforms.CenterCrop(224),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ])

    _loaded_model = {
        'path': model_path,
        'model': model,
        'vocab': vocab,
        'thresholds': checkpoint['thresholds'],
        'transform': transform,
        'device': device,
    }
    return _loaded_model


def classify_frame(image_path, model_info):
    """Classify a single frame. Returns list of (tag, confidence) tuples."""
    torch, _ = _require_torch()
    from PIL import Image

    img = Image.open(image_path).convert('RGB')
    tensor = model_info['transform'](img).unsqueeze(0).to(model_info['device'])

    with torch.no_grad():
        logits = model_info['model'](tensor)[0]
        probs = torch.sigmoid(logits)

    results = []
    for i, (prob, thresh) in enumerate(zip(probs, model_info['thresholds'])):
        if prob.item() >= thresh:
            results.append((model_info['vocab'][i], prob.item()))

    return sorted(results, key=lambda x: -x[1])


def suggest_for_video(video_path, model_path, max_frames=1):
    """Suggest tags for a single video. Returns list of (tag, confidence)."""
    from simpleparty.tagger import extract_keyframes

    model_info = load_model(model_path)
    frames = extract_keyframes(video_path, max_frames=max_frames)
    if not frames:
        return []

    tmpdir = frames[0].parent

    try:
        all_results = {}
        for frame in frames:
            for tag, conf in classify_frame(str(frame), model_info):
                if tag not in all_results or conf > all_results[tag]:
                    all_results[tag] = conf
        return sorted(all_results.items(), key=lambda x: -x[1])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def suggest_for_directory(directory, model_path, progress=None, max_frames=1):
    """Suggest tags for all untagged videos in a directory.

    Saves suggestions with status='suggested' to the tags file.
    """
    from simpleparty.tagger import untagged_videos

    if progress is None:
        progress = {}

    tags_data = load_tags(directory)
    videos = untagged_videos(directory, tags_data)

    progress['phase'] = 'suggesting'
    progress['total'] = len(videos)
    progress['done'] = 0
    progress['running'] = True

    for video_name in videos:
        progress['current'] = video_name
        video_path = Path(directory) / video_name

        results = suggest_for_video(str(video_path), model_path, max_frames=max_frames)

        if results:
            avg_conf = sum(c for _, c in results) / len(results)
            tags_data[video_name] = {
                'tags': [tag for tag, _ in results],
                'status': 'suggested',
                'confidence': round(avg_conf, 3),
            }
            save_tags(directory, tags_data)

        progress['done'] += 1

    progress['running'] = False
    progress['current'] = ''
