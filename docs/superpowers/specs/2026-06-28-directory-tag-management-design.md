# Directory Tag Management (Design Spec)

**Date:** 2026-06-28 · **Scope:** rename/merge/remove tags across all videos in a
directory, from the browse page. Server-rendered + htmx, zero new runtime deps.

## Goal

A directory's tags accumulate near-duplicates ("scifi" / "sci-fi" / "science fiction")
and over-generic labels worth dropping. Today tags can only be edited one video at a time
on the play page. This adds **directory-level** tag editing: rename a tag (renaming onto an
existing tag merges them) and remove a tag from every video — **keeping the videos**. This
is deliberately distinct from the existing `/delete-by-tag`, which deletes the videos.

User-approved decisions:
- Operations: **rename + remove**; rename-onto-an-existing-tag **is** the merge.
- Scope: affects **all** tags — confirmed *and* AI-suggested entries (and their
  `suggest_scores`).

## Data model (existing, unchanged)

`.simpleparty/tags.json`: `{ video_name: { tags: [str, …], status: 'confirmed'|'suggested',
starred?: bool, suggest_scores?: {tag: float}, suggest_source?: str } }`. Writes go through
the existing `tagger.update_tags(directory, transform)` — an atomic, per-directory-serialized
read-modify-write. The existing tag *filter* already aggregates counts by a tag's
**lowercased** form, so a "tag" for management is identified by its lowercased key and an
operation hits every case variant.

## Architecture — a pure rewrite function

The core is one pure, unit-testable function in `tagger.py`:

```
rewrite_tags(tags_map: dict, mapping: dict[str, str | None]) -> dict
```

`mapping` maps a **lowercased source tag** to either a target string (rename/merge) or
`None` (remove). For each video entry it produces a new entry where the `tags` list is
rewritten:
- each tag whose lowercased form is a key in `mapping` is replaced by the target (or dropped
  if `None`);
- **order preserved**, **duplicates removed** (so merging two tags a video both had yields
  one);
- `suggest_scores` keys are rewritten the same way; on a collision (both source and target
  scored) keep the **higher** score; dropped tags' scores are removed;
- `status` and `starred` are preserved untouched;
- entries are kept even if their `tags` list becomes empty (a `starred` flag may still
  matter); empty-entry cleanup is explicitly out of scope.

Both UI operations compile to a one-key `mapping`:
- **Rename `old → new`**: `{old.lower(): new}` (the entered casing of `new` is used).
- **Remove `tag`**: `{tag.lower(): None}`.

Keeping the rewrite pure and separate from I/O means the merge/dedup/score logic is tested
in isolation, and the route handlers are thin wrappers over `update_tags`.

## Routes & data flow

Two POST routes in `routes.py`, both gated on `_config['allow_tag']`, path-safety-checked
via the existing `is_safe_rel_path`/`resolve_path`, returning the re-rendered manage panel
fragment (htmx swap):

- **`/rename-tag`** — form `path, old, new`. Validates: `new` non-empty after strip; if
  `old.lower() == new.lower()` and same casing → no-op; applies
  `update_tags(dir, lambda t: rewrite_tags(t, {old.lower(): new.strip()}))`.
- **`/remove-tag`** — form `path, tag`. Applies
  `update_tags(dir, lambda t: rewrite_tags(t, {tag.lower(): None}))`.

Unknown tag (not present in the directory) → no-op (the rewrite simply changes nothing).
Both register in `POST_ROUTES`.

## UI surface (`render.py`)

A **"Manage tags"** disclosure in the browse toolbar's library zone, mirroring the existing
"Download URL" `<details>` pattern. A new `render_tag_manager(rel_path, tags_map)` renders a
panel listing every tag in the directory (by lowercased key) with its video count, each row
offering:
- **Rename** — a tiny inline form (`<input name="new">` + submit) posting `/rename-tag` with
  hidden `path` + `old`; `hx-target` the panel, `hx-swap="outerHTML"`. Submitting a name that
  matches another tag merges them.
- **Remove** — a button posting `/remove-tag`, with
  `hx-confirm="Remove tag 'X' from N videos? The videos are kept."` — wording chosen to make
  the keep-the-videos semantics unmistakable vs. the delete-by-tag action.

After either op the route returns the refreshed panel (updated counts / vanished rows). The
browse grid and filter reflect the change on the next navigation (they re-read tags.json).
A one-line hint notes that big edits may warrant re-Training the classifier. SVG icons via
the existing `icon()` helper; no new JavaScript.

## Edge cases / notes

- **Case-insensitive** match; rename normalizes to the entered casing across all variants.
- Rename to empty / to itself / unknown tag → no-op (validated or naturally inert).
- The trained classifier and `suspect_tags.json` reference old labels, but stale references
  simply stop matching (the play page only renders suspect badges for tags a video still
  has), so **no cleanup is needed**; a retrain after large edits is advisory only.
- Concurrency: `update_tags` is serialized per directory, so concurrent edits can't clobber.

## Testing

- **Unit (`rewrite_tags`)**: rename; merge-with-dedup (video has both source & target);
  remove; case variants ("Scifi"/"scifi" both rewritten); `suggest_scores` rename + collision
  (keep higher) + drop; `status`/`starred` preserved; empty-`tags` entry kept; unknown key →
  unchanged.
- **Route smoke (`test_http_smoke.py`)**: `/rename-tag` and `/remove-tag` persist the expected
  `tags.json`; `allow_tag=False` → 403; path traversal blocked; merge dedups.

## Scope, non-goals & sequencing

- **In:** `rewrite_tags`, the two routes, the manage panel, tests.
- **Out:** empty-entry cleanup, classifier retrain triggering, recursive (sub-directory) tag
  edits — this is strictly per-directory, matching the per-directory `tags.json` model.
- **Sequencing:** the manage panel adds UI to the browse toolbar, which the in-flight
  **frontend redesign** (`frontend-redesign` branch) is also restructuring. To avoid
  `render.py` conflicts, land this either before resuming the redesign or after it merges;
  decide at planning time. The `rewrite_tags` + routes layer is independent of the redesign.
