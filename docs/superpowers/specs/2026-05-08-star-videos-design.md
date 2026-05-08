# Star videos — design

Status: approved 2026-05-08

## Goal

Allow the user to star (favourite) individual videos and filter the browse view to show only starred videos.

## Data model

Each entry in `.simpleparty/tags.json` gains an optional boolean field:

```json
{ "video.mp4": { "tags": [...], "status": "confirmed", "starred": true } }
```

- Field absent or falsy ⇒ not starred. No migration needed.
- A video may be starred without any tags or status; the entry can be as small as `{"starred": true}`.
- Unstarring removes the `starred` key. If the resulting entry has no other meaningful state (no `tags`, no `rejected_tags`, no `status` other than default), the entry is dropped from the map to keep the file tidy.

## Server

- Helper in `tagger.py`: `is_starred(entry)` — `bool(entry) and bool(entry.get('starred'))`.
- New endpoint `POST /star-update` taking form fields `dir`, `name`, `starred` (`1`/`0`). Mirrors the existing `/tag-update` pattern in `server.py`. Loads tags via `load_tags`, mutates the entry, saves atomically via `save_tags`. Returns 204 on success, 400 on bad input, 403 if `allow_tag` is disabled, 404 if the directory or video does not exist.
- Browse handler: parse `starred=1` from query string. After the existing `filter_videos_by_tags`, additionally filter to entries where `is_starred(tags_map.get(name, {}))` if the flag is set.
- Preserve `starred=1` on shuffle/play/next/prev links the same way `tags` is preserved today (extend `url_for_browse` and `url_for_play`).
- Like the rest of the tag plumbing, `/star-update` is gated by `--no-tag`; starring rides on the same on/off switch since the data lives in `tags.json`. README updated to mention this.

## UI — player page

- A `★` button in the existing video meta/controls row, near the tag pill area. Filled when starred, outline when not.
- Clicking issues `POST /star-update` via fetch and toggles the visual state in place — no page reload, matching how tag adds/removes already work.
- No keyboard shortcut. No grid-tile overlay.

## UI — browse page filter

- A `★ Starred only` pill rendered in `render_tag_filter` next to the existing tag filter pills.
- Inactive: faint outline pill linking to current URL with `starred=1` added.
- Active: filled purple pill (matches `.tag-pill.active`) with an `×` link back to the URL with `starred` removed.
- Composes with tag filters via AND.
- The pill is hidden entirely if there are no starred videos in the current directory (parallels how the tag filter hides itself when there are no tags).
- The active state is preserved across navigation (next/prev/shuffle) the same way `tags` is.

## Persistence behaviour

Star state lives per-directory in that directory's `tags.json`. Moving a file across directories drops its starred state — same semantics as tags today.

## Testing

Unit tests (`tests/test_tagger.py`):

- `is_starred` helper.
- Star/unstar round-trip through `load_tags`/`save_tags`.
- Entry pruning when removing the last field.

Integration tests (`tests/test_server.py`, new file if needed):

- `POST /star-update` happy path.
- `POST /star-update` rejected when `--no-tag`.
- Browse handler with `starred=1` alone.
- Browse handler with `starred=1` combined with `tags=`.
- Filter pill hidden when zero starred videos.

## Out of scope

- Grid-tile star toggle.
- Keyboard shortcut.
- Cross-directory "All starred" view.
- Bulk star operations.
- Star as a sort key.
