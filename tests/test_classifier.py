"""Tests for the CLIP linear-probe classifier core.

These cover the parts that need neither a GPU nor CLIP weights: checkpoint
v2 save/load (tiny head, CPU tensors), back-compat detection, the per-video
train/val split, and the pure label-noise scoring.
"""

import numpy as np
import pytest

from simpleparty.classifier import (
    RetrainRequired,
    _build_head,
    save_model,
    load_model,
    _per_video_split,
    score_label_noise,
    _zero_shot_rank,
)
from simpleparty.embeddings import CLIP_MODEL_ID
from simpleparty.tagger import SIMPLEPARTY_DIR, MODEL_FILENAME


# --- checkpoint v2 ---

def test_save_load_roundtrip(tmp_path):
    import torch
    head = _build_head(8, 3)
    vocab = ['a', 'b', 'c']
    thresholds = [0.4, 0.5, 0.6]
    path = tmp_path / SIMPLEPARTY_DIR / MODEL_FILENAME
    path.parent.mkdir(parents=True)
    save_model(str(path), head, vocab, thresholds, embed_dim=8)

    info = load_model(str(path))
    assert info['vocab'] == vocab
    assert info['thresholds'] == thresholds
    assert info['clip_model_id'] == CLIP_MODEL_ID
    assert info['embed_dim'] == 8

    x = torch.randn(2, 8)
    head.eval()  # disable dropout so the comparison is deterministic
    with torch.no_grad():
        expected = head(x)
        got = info['head'](x)
    assert torch.allclose(expected, got)


def test_load_legacy_efficientnet_checkpoint_raises(tmp_path):
    import torch
    path = tmp_path / 'old.pt'
    # Old format: full backbone state, no clip_model_id.
    torch.save({
        'model_state_dict': {'features.0.weight': torch.zeros(1)},
        'vocab': ['x', 'y'],
        'thresholds': [0.5, 0.5],
        'num_classes': 2,
    }, str(path))
    with pytest.raises(RetrainRequired):
        load_model(str(path))


def test_load_mismatched_model_id_raises(tmp_path):
    import torch
    head = _build_head(4, 2)
    path = tmp_path / 'm.pt'
    torch.save({
        'head_state_dict': head.state_dict(),
        'vocab': ['x', 'y'],
        'thresholds': [0.5, 0.5],
        'clip_model_id': 'SomeOtherModel__pretrain',
        'embed_dim': 4,
        'num_classes': 2,
        'format_version': 2,
    }, str(path))
    with pytest.raises(RetrainRequired):
        load_model(str(path))


# --- per-video split ---

def test_per_video_split_is_deterministic():
    names = [f'v{i}.mp4' for i in range(20)]
    a = _per_video_split(names, frac=0.8, seed=42)
    b = _per_video_split(names, frac=0.8, seed=42)
    assert a == b


def test_per_video_split_disjoint_and_proportioned():
    names = [f'v{i}.mp4' for i in range(20)]
    train_idx, val_idx = _per_video_split(names, frac=0.8, seed=42)
    assert set(train_idx).isdisjoint(val_idx)
    assert sorted(train_idx + val_idx) == list(range(20))
    assert len(train_idx) == 16


# --- label-noise scoring ---

def test_score_label_noise_flags_confident_disagreement():
    names = ['a.mp4', 'b.mp4']
    vocab = ['cat', 'dog']
    # a: 'cat' given-positive but model says ~0 -> suspect. 'dog' unknown.
    # b: 'dog' given-negative (rejected) but model says ~1 -> suspect.
    targets = np.array([[1.0, 0.0], [0.0, 0.0]])
    masks = np.array([[1.0, 0.0], [0.0, 1.0]])
    probs = np.array([[0.02, 0.5], [0.5, 0.98]])

    suspects = score_label_noise(probs, targets, masks, vocab, names, low=0.1, high=0.9)
    assert suspects['a.mp4'] == [{'tag': 'cat', 'given': 1, 'prob': pytest.approx(0.02)}]
    assert suspects['b.mp4'] == [{'tag': 'dog', 'given': 0, 'prob': pytest.approx(0.98)}]


def test_zero_shot_rank_orders_and_filters_by_similarity():
    image = np.array([1.0, 0.0, 0.0])
    c = np.array([0.8, 0.6, 0.0]); c = c / np.linalg.norm(c)
    text = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], c])  # sims: 1.0, 0.0, 0.8
    vocab = ['a', 'b', 'c']
    ranked = _zero_shot_rank(image, text, vocab, max_tags=10, min_sim=0.5)
    assert [t for t, _ in ranked] == ['a', 'c']  # 'b' below min_sim, sorted desc


def test_zero_shot_rank_respects_max_tags():
    image = np.array([1.0, 0.0])
    text = np.array([[1.0, 0.0], [0.9, 0.1]])
    ranked = _zero_shot_rank(image, text, ['a', 'b'], max_tags=1, min_sim=0.0)
    assert len(ranked) == 1 and ranked[0][0] == 'a'


def test_score_label_noise_ignores_unmasked_and_agreeing():
    names = ['a.mp4']
    vocab = ['cat', 'dog']
    targets = np.array([[1.0, 0.0]])
    masks = np.array([[1.0, 1.0]])
    probs = np.array([[0.95, 0.02]])  # both agree with given labels
    suspects = score_label_noise(probs, targets, masks, vocab, names, low=0.1, high=0.9)
    assert suspects == {}


# --- auto-clean ---

def test_auto_clean_drops_confident_disagreements_with_floor():
    import torch
    from simpleparty.classifier import _auto_clean_mask, MIN_POSITIVES_FLOOR
    vocab = ['cat', 'dog']
    # cat has many positives (rows 0..4 given=1); row0 is confidently wrong.
    # dog: row0 given-negative but model says present -> dropped, no floor.
    n = 6
    targets = torch.zeros(n, 2)
    masks = torch.ones(n, 2)
    targets[:5, 0] = 1.0          # 5 cat positives
    probs = torch.full((n, 2), 0.5)
    probs[0, 0] = 0.001           # cat positive, model sure it's absent -> drop
    probs[0, 1] = 0.999           # dog negative, model sure it's present -> drop

    M, dropped = _auto_clean_mask(masks.clone(), probs, targets, vocab)
    assert M[0, 0] == 0.0   # bad cat positive dropped (5 > floor)
    assert M[0, 1] == 0.0   # bad dog negative dropped
    assert dropped == 2


def test_auto_clean_protects_rare_positive_floor():
    import torch
    from simpleparty.classifier import _auto_clean_mask, MIN_POSITIVES_FLOOR
    vocab = ['rare']
    n = MIN_POSITIVES_FLOOR  # exactly at the floor
    targets = torch.ones(n, 1)
    masks = torch.ones(n, 1)
    probs = torch.zeros(n, 1)   # every positive looks wrong, but floor protects
    M, dropped = _auto_clean_mask(masks.clone(), probs, targets, vocab)
    assert dropped == 0
    assert M.sum() == n


# --- suspect badge rendering ---

def test_train_flags_and_cleans_a_mislabeled_video(tmp_path, monkeypatch):
    """End-to-end training over crafted separable embeddings: a video whose
    given label contradicts its (clearly separable) content is flagged and
    auto-cleaned. CLIP is monkeypatched out so this needs no GPU."""
    import simpleparty.classifier as clf
    from simpleparty.tagger import save_tags

    rng = np.random.default_rng(0)
    # Two clearly-separable directions, scaled to the kind of margin real CLIP
    # features produce for genuinely distinct concepts.
    RED = np.eye(32, dtype='float32')[0] * 5
    BLUE = np.eye(32, dtype='float32')[1] * 5

    def jitter(v):
        return (v + rng.normal(0, 0.05, 32)).astype('float32')

    tags = {}
    vectors = {}
    for i in range(8):
        name = f'red_{i}.mp4'
        (tmp_path / name).write_bytes(b'')
        tags[name] = {'tags': ['red'], 'status': 'confirmed'}
        vectors[name] = jitter(RED)
    for i in range(8):
        name = f'blue_{i}.mp4'
        (tmp_path / name).write_bytes(b'')
        tags[name] = {'tags': ['blue'], 'status': 'confirmed'}
        vectors[name] = jitter(BLUE)
    # Red content, mislabeled 'blue'.
    (tmp_path / 'wrong.mp4').write_bytes(b'')
    tags['wrong.mp4'] = {'tags': ['blue'], 'status': 'confirmed'}
    vectors['wrong.mp4'] = jitter(RED)

    save_tags(str(tmp_path), tags)

    def fake_embed(directory, name, max_frames=8, progress=None):
        return vectors[name].copy()

    monkeypatch.setattr(clf, 'get_video_embedding', fake_embed)
    monkeypatch.setattr(clf, 'prune_stale_embeddings', lambda d: None)

    progress = {'running': True}
    clf.train(str(tmp_path), min_tag_count=5, progress=progress)

    assert progress.get('error') is None
    suspects = clf.load_suspects(str(tmp_path))
    assert 'wrong.mp4' in suspects
    assert any(s['tag'] == 'blue' for s in suspects['wrong.mp4'])
    assert progress['cleaned_count'] >= 1


def test_render_marks_suspect_confirmed_tags():
    from simpleparty.render import render_video_tags_inline
    html = render_video_tags_inline('dir', 'v.mp4', ['cat', 'dog'],
                                    status='confirmed', suspect_tags=['cat'])
    assert 'suspect' in html        # cat pill flagged
    assert 'suspect-badge' in html
    # dog pill is not flagged
    assert html.count('video-tag-pill suspect') == 1


def test_render_suggested_shows_source_and_scores():
    from simpleparty.render import render_video_tags_inline
    html = render_video_tags_inline(
        'dir', 'v.mp4', ['cat', 'dog'], status='suggested',
        scores={'cat': 0.91, 'dog': 0.42}, source='model')
    assert '0.91' in html and '0.42' in html   # per-pill scores
    assert 'model' in html                      # source surfaced once


def test_render_suggested_marks_zero_shot_source():
    from simpleparty.render import render_video_tags_inline
    html = render_video_tags_inline(
        'dir', 'v.mp4', ['cat'], status='suggested',
        scores={'cat': 0.22}, source='zero-shot')
    assert 'zero-shot' in html
    assert '0.22' in html


def test_render_suggested_without_scores_still_renders():
    # Back-compat: an older suggested entry with no stored scores/source.
    from simpleparty.render import render_video_tags_inline
    html = render_video_tags_inline('dir', 'v.mp4', ['cat'], status='suggested')
    assert 'cat' in html
    assert 'btn-confirm' in html  # Accept button still present
