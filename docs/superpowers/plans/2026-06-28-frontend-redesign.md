# SimpleParty Frontend Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace SimpleParty's ad-hoc CSS/markup with a tokenized modern-dark design system, an inline-SVG icon set, role-encoded components, and decluttered browse/player layouts — without changing any backend behavior, route, URL, or feature.

**Architecture:** A Claude Design mockup gate establishes the exact visual look for the two hero pages; then we port that look into `render.py` (markup) and `static/style.css` (styles) behind a token layer, preserving every test-load-bearing class/string and every `play.js` element id. Pure presentation.

**Tech Stack:** Python stdlib HTTP server, string-built HTML in `render.py`, htmx 2.0.4, one hand-authored `style.css`, vanilla JS in `play.js`. Claude Design MCP for mockups only.

## Global Constraints

- **No backend/route/URL/feature changes.** `routes.py`, `server.py`, `library.py`, etc. are off-limits. Only `render.py`, `static/style.css`, `static/play.js`, a new `icons.py`, README, Makefile, and tests may change.
- **Runtime stays zero-dependency.** `pyproject.toml` `dependencies = []` must remain empty. The shipped artifact is one `static/style.css` plus `static/play.js`. No CDN beyond the already-present htmx `<script>`.
- **Committed palette (exact hex):** `--bg:#0a0c10 --surface:#12151c --surface-2:#1a1f29 --surface-hover:#222836 --border:#262c38 --text:#e6e9ef --text-muted:#9aa4b2 --text-faint:#7a8494 --accent:#6366f1 --accent-hover:#818cf8 --on-accent:#ffffff --danger:#ef4444 --warn:#f59e0b --success:#34d399 --star:#facc15`. `--accent-weak:rgba(99,102,241,0.14)`, `--danger-weak:rgba(239,68,68,0.16)`.
- **Type ramp (only these sizes for text):** xs 12 / sm 13 / base 15 / lg 18 / xl 22 / display 28 px. No sub-12px text.
- **Spacing/radius:** 4/8/12/16/24/32; radius 6/8/12/pill; one shadow `0 4px 16px rgba(0,0,0,0.4)`.
- **Carry forward every fix** in `docs/ui-audit-mobile-android-2026-06-05.md` (focus-visible rings, ≥44px touch targets on ≤640px, AA contrast for body text, reduced-motion guard, accessible names on icon-only controls, `aria-pressed` on toggles, landscape control handling, keyboard-operable tag combobox, `role=status/alert` regions).
- **LOAD-BEARING strings/classes/ids — preserve verbatim, or update the named test/JS in the SAME task.** Only `tests/test_http_smoke.py` and `static/play.js` couple to rendered output (`tests/test_server.py` is logic-only).
  - Classes counted/asserted in tests: `item item-video` (counted), `playlist-item` (counted as `<a class="playlist-item`), `video-tag-pill suspect`, `suspect-badge`, `embed-check`, `btn-train`, `tag-progress`, `load-more`, `file-list` (CSS rule `#file-list` asserted present).
  - Strings asserted in tests: `video-title">` + name, `name="video" value="a.mp4"`, `Embed all missing (2)`, `Embed all missing`, `embedded`, `Suggest (model)`, `zero-shot`, `model)`, `Suggest`, `Embed this video`, `Playlist`, and absence of `hx-post="/suggest"`, `/confirm-all`, `Confirm all`.
  - `play.js` element ids: `video`, `video-overlay`, `speed-select`, `btn-autoplay`, `btn-repeat`, `btn-star`, `delete-form`; selectors `.star-icon`, `#delete-form button`. The btn-autoplay/btn-repeat **textContent** ("Autoplay: On/Off", "Repeat: Off/All/One") is set by JS — keep those buttons text-labelled, not icon-only.
- **Run after every task:** `uv run pytest -q` must stay green (155-ish assertions). Never weaken a test to make it pass; if the redesign legitimately changes a load-bearing string, update the assertion in the same commit and note why.

---

## File map

- `src/simpleparty/icons.py` — **new.** Inline-SVG icon set + `icon()` helper.
- `src/simpleparty/render.py` — **modify.** Swap emoji → `icon()`; restructure browse toolbar + player control tiers + tagging panel; restyle download/unlock/error. Markup only.
- `src/simpleparty/static/style.css` — **rewrite.** Token layer → base/type → components → page layouts → carried-forward a11y/mobile block.
- `src/simpleparty/static/play.js` — **modify.** Star toggle no longer rewrites glyph text (CSS drives filled/outline via `.active`).
- `tests/test_http_smoke.py` — **modify only where a load-bearing string legitimately changes**, plus one new guard test (no emoji) and one CSS-token guard test.
- `tests/test_render_icons.py` — **new.** Unit tests for `icon()`.
- `README.md` — **modify** only if Task 2 selects Tailwind (soften "zero dependencies / no build step").

---

## Task 1: Claude Design mockup gate (no code; approval checkpoint)

**Files:** none in-repo (Claude Design MCP artifacts only).

**Deliverable:** Two approved, in-browser-rendered mockups (Browse + Player) using the committed palette, type ramp, spacing, SVG icon style, and the Section-2 restructures (browse toolbar view/library zones; player transport/secondary/toggle/danger tiers). Smaller pages are *derived* from the same system, not separately mocked.

- [ ] **Step 1: Build the Browse + Player mockups in Claude Design** using the exact tokens in Global Constraints. Include realistic content: breadcrumb, mixed dirs+videos grid with thumbnails, star/selected-tag chips + sort segmented control, the embed/train/download library zone, an active progress strip; and for Player: hero video, tiered controls, the tag accept/reject/suggest panel, related + playlist.
- [ ] **Step 2: Render previews** (`render_preview` → `serve_url`) and present both to the user. Iterate until the user explicitly approves the look. Capture the final SVG icon shapes here (they become `icons.py` source in Task 3).
- [ ] **Step 3: Record the approved values** — extract the precise per-component CSS (colors mapped to tokens, paddings, radii, font sizes from the ramp) into a short notes block appended to the design spec. These notes are the source of truth for the porting tasks' exact values.

**Gate:** Do not start Task 2 until the user approves the mockups.

---

## Task 2: Resolve CSS approach + scaffold token layer

**Files:**
- Modify: `src/simpleparty/static/style.css` (top of file)
- Test: `tests/test_http_smoke.py` (extend `test_static_css`)

**Decision rule (resolve the spec's deferred choice):** Default to **hand-written CSS custom properties** — the tests require semantic class names (`item-video`, `btn-train`, …) so Tailwind utilities alone can't replace them, the project ethos is zero-build, and it's a single 300-line stylesheet. Choose Tailwind-compiled **only if** the approved mockup needs rapid utility iteration the user explicitly wants; if so, add a `tailwind` Makefile target + standalone-CLI config that *scans `render.py`* and **emits the committed token names as CSS vars**, keep all load-bearing semantic classes as `@layer components`, and soften the README claim to "zero **runtime** dependencies." The rest of this plan assumes the hand-written branch (the default).

**Interfaces:**
- Produces: a `:root` token block consumed by every later CSS task; class `#file-list` still present (keeps `test_static_css`).

- [ ] **Step 1: Write the failing test** — extend the existing CSS smoke test to assert the token layer exists.

```python
# in tests/test_http_smoke.py, replace test_static_css body's asserts with these added lines:
def test_static_css(srv):
    status, headers, body = request(srv, 'GET', '/static/style.css')
    text = body.decode()
    assert status == 200
    assert headers['Content-Type'].startswith('text/css')
    assert headers['Cache-Control'] == 'public, max-age=3600'
    assert '#file-list' in text
    assert ':root' in text and '--accent:#6366f1' in text.replace(' ', '')
    assert '--bg:#0a0c10' in text.replace(' ', '')
```

- [ ] **Step 2: Run it, expect FAIL** — `uv run pytest tests/test_http_smoke.py::test_static_css -q` → fails on the `--accent` assertion (token block absent).
- [ ] **Step 3: Prepend the token block** to `style.css` (above the existing reset):

```css
:root{
  --bg:#0a0c10;--surface:#12151c;--surface-2:#1a1f29;--surface-hover:#222836;--border:#262c38;
  --text:#e6e9ef;--text-muted:#9aa4b2;--text-faint:#7a8494;
  --accent:#6366f1;--accent-hover:#818cf8;--accent-weak:rgba(99,102,241,0.14);--on-accent:#fff;
  --danger:#ef4444;--danger-weak:rgba(239,68,68,0.16);--warn:#f59e0b;--success:#34d399;--star:#facc15;
  --text-xs:12px;--text-sm:13px;--text-base:15px;--text-lg:18px;--text-xl:22px;--text-display:28px;
  --lh-tight:1.2;--lh:1.45;
  --space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;--space-6:32px;
  --radius-sm:6px;--radius-md:8px;--radius-lg:12px;--radius-pill:999px;
  --shadow:0 4px 16px rgba(0,0,0,0.4);
}
```

- [ ] **Step 4: Run it, expect PASS** — `uv run pytest tests/test_http_smoke.py::test_static_css -q`.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Add CSS design-token layer (:root)"`.

---

## Task 3: Inline-SVG icon set + helper

**Files:**
- Create: `src/simpleparty/icons.py`
- Create: `tests/test_render_icons.py`

**Interfaces:**
- Produces: `icon(name: str, *, label: str | None = None, cls: str = 'icon') -> str`. Returns an inline `<svg>` string. When `label` is None the svg carries `aria-hidden="true"` (decorative, inside a labelled control); when `label` is given it carries `role="img"` + `aria-label="<label>"`. Uses `currentColor`, `width/height=1em`, `viewBox="0 0 24 24"`, class `cls`.
- Names required (from emoji audit): `download trash star star-outline embed tag wand gear play prev next shuffle folder lock lock-open check x warning clock film film`. (Exact path `d=` strings come from the Task 1 approved icons.)

- [ ] **Step 1: Write failing tests** in `tests/test_render_icons.py`:

```python
import re
from simpleparty.icons import icon, ICONS

def test_known_names_render_svg():
    for name in ['download','trash','star','star-outline','embed','tag','wand','gear',
                 'play','prev','next','shuffle','folder','lock','lock-open','check','x','warning','clock','film']:
        svg = icon(name)
        assert svg.startswith('<svg') and svg.rstrip().endswith('</svg>')
        assert 'currentColor' in svg

def test_decorative_icon_is_aria_hidden():
    assert 'aria-hidden="true"' in icon('trash')
    assert 'aria-label' not in icon('trash')

def test_labelled_icon_exposes_name():
    svg = icon('trash', label='Delete')
    assert 'role="img"' in svg and 'aria-label="Delete"' in svg
    assert 'aria-hidden' not in svg

def test_unknown_name_raises():
    import pytest
    with pytest.raises(KeyError):
        icon('not-an-icon')

def test_custom_class_applied():
    assert 'class="star-icon"' in icon('star', cls='star-icon')
```

- [ ] **Step 2: Run, expect FAIL** — `uv run pytest tests/test_render_icons.py -q` → ModuleNotFoundError.
- [ ] **Step 3: Implement `icons.py`** — a dict of name→inner-SVG markup (paths captured from the approved Task 1 icons) and the helper:

```python
"""Inline-SVG icon set. One flat stroke style, currentColor, zero deps."""
from html import escape as _esc

# Each value is the inner markup of a 24x24 viewBox svg (paths captured from the
# approved Claude Design mockup). Stroke style: fill=none, stroke=currentColor,
# stroke-width=2, round caps/joins — except `star`/`film` which fill currentColor.
ICONS = {
    'download': '<path d="..."/>',
    'trash': '<path d="..."/>',
    'star': '<path d="..." fill="currentColor" stroke="none"/>',
    'star-outline': '<path d="..."/>',
    'embed': '<path d="..."/>',
    'tag': '<path d="..."/>',
    'wand': '<path d="..."/>',
    'gear': '<path d="..."/>',
    'play': '<path d="..."/>',
    'prev': '<path d="..."/>',
    'next': '<path d="..."/>',
    'shuffle': '<path d="..."/>',
    'folder': '<path d="..."/>',
    'lock': '<path d="..."/>',
    'lock-open': '<path d="..."/>',
    'check': '<path d="..."/>',
    'x': '<path d="..."/>',
    'warning': '<path d="..."/>',
    'clock': '<path d="..."/>',
    'film': '<path d="..." fill="currentColor" stroke="none"/>',
}

def icon(name, *, label=None, cls='icon'):
    inner = ICONS[name]  # KeyError on unknown name is intentional
    a11y = (f'role="img" aria-label="{_esc(label)}"' if label
            else 'aria-hidden="true"')
    return (
        f'<svg class="{_esc(cls)}" width="1em" height="1em" viewBox="0 0 24 24" '
        f'fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" {a11y}>{inner}</svg>'
    )
```

(Replace every `d="..."` with the real path data from the approved mockup before running.)

- [ ] **Step 4: Run, expect PASS** — `uv run pytest tests/test_render_icons.py -q`.
- [ ] **Step 5: Add `.icon` base style** to `style.css` components area: `.icon{display:inline-block;vertical-align:-0.125em;flex-shrink:0}`.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "Add inline-SVG icon set + icon() helper"`.

---

## Task 4: Base, typography & component CSS (buttons, pills, inputs, cards)

**Files:**
- Modify: `src/simpleparty/static/style.css` (everything below `:root`, above page-specific rules)

**Interfaces:**
- Produces CSS classes the port tasks rely on: `.btn` (secondary/ghost), `.btn-primary`, `.btn-toggle`, `.btn-danger`, `.btn-lock` (alias of danger), `.sort-pills`/`.sort-pill` (segmented), `.tag-pill`/`.tag-pill.active`, `.tag-pill.star-pill`, input field style, `.card` surface recipe. All reference only `var(--…)` tokens.

- [ ] **Step 1: Rewrite the base + component layer** using only tokens. Replace literal hex with `var(--…)`. Map roles: `.btn` neutral on `--surface-2`/`--border`; `.btn-primary` filled `--accent`/`--on-accent`; `.btn.active`/`.btn-toggle[aria-pressed=true]` accent fill; `.btn-danger`,`.btn-lock` use `--danger`/`--danger-weak`; `.btn-star.active` uses `--star`. Sort pills become a segmented control (joined, shared border, active = `--accent`). Tag chips stay pill-rounded on `--surface-2`. Inputs: `--surface-2` bg, `--border`, focus ring `--accent-weak`. Body uses `--bg`/`--text` and the type tokens. **Exact paddings/sizes come from the approved Task 1 mockup; sizes restricted to the ramp.**
- [ ] **Step 2: Keep these carried-forward rules** verbatim from the current file's audit block: `.visually-hidden`, `:focus-visible` ring, `@media(prefers-reduced-motion:reduce)`, the `@media(max-width:640px)` ≥44px target block, the landscape `@media(orientation:landscape) and (max-height:520px)` block, 2-line clamp on list names. Re-express their colors via tokens.
- [ ] **Step 3: Run the suite** — `uv run pytest -q`. Expected: PASS (no markup changed yet; CSS-only). If `test_static_css` token asserts still pass, good.
- [ ] **Step 4: Visual check** — start the app on a fixtures dir (`uv run simpleparty <fixtures> -p 8731 -b 127.0.0.1`), screenshot Browse + Player with Playwright; confirm nothing is unstyled/broken even before markup port (classes still match). 
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Tokenize base + component CSS; role-encoded buttons & segmented sort"`.

---

## Task 5: Port nav + browse toolbar (view/library zones) + sort/filter

**Files:**
- Modify: `src/simpleparty/render.py` — `render_nav`, `render_sort_pills`, `render_tag_filter`, `render_file_list` (action-bar assembly + dir rows), `render_coverage_controls`, `_render_train_btn`.
- Modify: `src/simpleparty/static/style.css` — `nav`, `.action-bar`/new toolbar zone classes, `.tag-filter`.
- Test: `tests/test_http_smoke.py` (only if a load-bearing string moves).

**Interfaces:**
- Consumes: `icon()` from Task 3.
- Produces: toolbar markup grouped into a **view zone** (Shuffle, sort segmented control, tag filter) and a **library zone** (coverage badge + Embed/Train, Download/Manage), plus a slim status strip. Must keep verbatim: `btn-train`, `Embed all missing ({missing})`, the coverage badge containing `embedded`, `embed-check`, `tag-progress`, `#file-list`, `item item-video` (dir rows stay `class="item"`).

- [ ] **Step 1: Confirm the guarded strings** — `grep -nE "Embed all missing|btn-train|embedded|tag-progress|item-video" tests/test_http_smoke.py` and keep each output token unchanged in the new markup.
- [ ] **Step 2: Replace emoji in these functions** with `icon()`: Downloads `⬇`→`icon('download')`, Shuffle `⇅`→`icon('shuffle')`, coverage/embed/train `🧠`→`icon('embed')`, Download URL/Queue `⬇`→`icon('download')`, folder `📁`→`icon('folder')`, lock `🔒`/`🔓`→`icon('lock')`/`icon('lock-open')`, clock `⏳`→`icon('clock')`, suggested-card prefix `❓`→a `<span class="badge-suggested">` (see Task 7). Lock button keeps `aria-label`.
- [ ] **Step 3: Restructure the action bar** into `<div class="toolbar"><div class="toolbar-view">…</div><div class="toolbar-lib">…</div></div>`; move sort segmented control + shuffle + (tag filter rendered just above stays) into view zone, embed/train/download into library zone. Keep the `tag-progress` and `download-progress` polling divs as the status strip. Sort pills already emit `class="sort-pill"`; the segmented look is pure CSS from Task 4.
- [ ] **Step 4: Run the suite** — `uv run pytest -q`. Fix any assertion that broke *because a guarded string moved*; if a string is genuinely unchanged the tests stay green. Expected: PASS.
- [ ] **Step 5: Visual check** — screenshot Browse at 1280, 393 (portrait), confirm toolbar zones read clearly and no horizontal overflow.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "Restructure browse toolbar into view/library zones; SVG icons"`.

---

## Task 6: Port video cards, dir rows & suggested badge

**Files:**
- Modify: `src/simpleparty/render.py` — `render_video_item`, `render_video_items`, dir-row block in `render_file_list`, `render_related_videos`, `render_playlist_item`.
- Modify: `src/simpleparty/static/style.css` — `.item-video`, `.item-thumb*`, `.item-name/size/tags`, `.btn-del`, `.badge-suggested`, `.playlist-*`, `.related-*`.

**Interfaces:**
- Consumes: `icon()`.
- Produces: must keep verbatim `item item-video` (counted by `test_browse_renders_first_page_plus_sentinel`), `<a class="playlist-item`, `embed-check`, `name="video" value="…"`, `item-tags`/`item-tags suggested`, thumb placeholder element. Delete uses `icon('trash', label='Delete '+name)`. Thumb placeholder `🎬`→`icon('film', cls='item-thumb-placeholder-icon')`. Suggested tags prefix `❓`→`<span class="badge-suggested">Suggested</span>` (real text + color, not opacity — keeps audit A9).

- [ ] **Step 1: Confirm guarded strings** — `grep -nE "item item-video|playlist-item|embed-check|item-tags" tests/test_http_smoke.py`.
- [ ] **Step 2: Swap emoji → icons** in the listed functions; replace the suggested-tags opacity treatment with the `.badge-suggested` chip + an AA-contrast color (`--accent`), keeping the existing `visually-hidden` "Suggested tags:" prefix for AT.
- [ ] **Step 3: Restyle cards** in CSS via tokens (thumbnail-first, tightened name/size/tags using ramp sizes; hover `--surface-hover`; `.item.playing` border `--accent`). Keep `flex-wrap:nowrap` (audit L1) and `min-width:0` rules.
- [ ] **Step 4: Run the suite** — `uv run pytest -q`. Expected: PASS (pagination counts unchanged, `item item-video` intact).
- [ ] **Step 5: Visual check** — screenshot a populated grid + the 250-item lazy grid (scroll to trigger `load-more`) at 1280 and 393; confirm uniform card heights.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "Restyle video cards, dir rows, playlist & suggested badge"`.

---

## Task 7: Port player control tiers + star icon (CSS-driven)

**Files:**
- Modify: `src/simpleparty/render.py` — `render_play_page` (controls block, transcode notice, video-title/area).
- Modify: `src/simpleparty/static/play.js` — star toggle stops rewriting glyph text.
- Modify: `src/simpleparty/static/style.css` — `#player-area`, `#controls` tiers, `.btn-skip`, `#now-playing`, `.btn-star`, `#video-overlay`.

**Interfaces:**
- Consumes: `icon()`.
- Produces: must keep ids `video`, `video-overlay`, `video-title`, `speed-select`, `btn-autoplay`, `btn-repeat`, `btn-star`, `delete-form`, and `.star-icon` on the star button. `btn-autoplay`/`btn-repeat` keep text labels (JS sets textContent). Star button renders **both** states via CSS: `.btn-star .star-icon` shows a star whose fill is `none` by default and `var(--star)` when `.btn-star.active` — so JS only toggles the class, not the glyph.

- [ ] **Step 1: Confirm guarded ids/strings** — `grep -nE "btn-autoplay|btn-repeat|btn-star|star-icon|delete-form|video-title|speed-select" static/play.js tests/test_http_smoke.py`.
- [ ] **Step 2: Rebuild the controls markup** into tiers: transport (`Prev`/`Next` with `icon('prev')`/`icon('next')`, prominent), secondary (`.skip-group` + `speed-select`), toggles (`Shuffle`/`Autoplay`/`Repeat` as `.btn-toggle`), danger (`delete-form` button `.btn-danger` with `icon('trash')`), star (`.btn-star` with `<span class="star-icon">`+`icon('star')`). Transcode `⚙`→`icon('gear')`. Keep all `title`/`aria-label`/`aria-pressed`/`onclick`/`href` attrs exactly.
- [ ] **Step 3: Edit `play.js`** — in the star click handler, **remove** the line `btnStar.querySelector(".star-icon").textContent=next?"★":"☆";` (CSS now drives appearance via `.active`). Leave class/aria toggling and `flash()` intact. Initial state: render emits `.active` when starred, so CSS shows the filled star with no JS needed.

```js
// before:
btnStar.classList.toggle("active",next);
btnStar.setAttribute("aria-pressed",next?"true":"false");
btnStar.querySelector(".star-icon").textContent=next?"★":"☆";   // <-- delete this line
flash(next?"Starred":"Unstarred");
// after: the textContent line is gone; CSS .btn-star.active .star-icon{fill:var(--star)} handles it.
```

- [ ] **Step 4: CSS for tiers + star** — group controls with `gap` and flex wrapping that keeps tiers visually distinct; `.btn-star .star-icon{...}` default `fill:none;stroke:var(--text-muted)`, `.btn-star.active .star-icon{fill:var(--star);stroke:var(--star)}`. Keep `#now-playing{flex:0 1 auto}` (audit P5) and landscape rules.
- [ ] **Step 5: Run the suite** — `uv run pytest -q`. Expected: PASS (`test_play_page`, suspect/suggest/embed play tests unaffected — they assert tag panel + title, not control glyphs).
- [ ] **Step 6: Manual + visual check** — `uv run simpleparty <fixtures>`; open a play page, verify: star toggles visually (filled⇄outline) and persists across reload, autoplay/repeat labels still flip, `d`/`a`/`r` keyboard shortcuts work, swipe/skip still work. Screenshot desktop + 800×393 landscape (controls must be reachable).
- [ ] **Step 7: Commit** — `git add -A && git commit -m "Tier player controls; CSS-driven star icon; SVG transport icons"`.

---

## Task 8: Port tagging panel (accept/reject/suggest) + coverage controls

**Files:**
- Modify: `src/simpleparty/render.py` — `render_video_tags_inline`, the per-video embed/suggest block in `render_play_page`, `render_coverage_controls`.
- Modify: `src/simpleparty/static/style.css` — `.video-meta`, `.video-tag-pill*`, `.suspect-badge`, `.btn-confirm/.btn-reject`, `.suggest-source`, `.video-tag-remove`, `.coverage-badge`.

**Interfaces:**
- Consumes: `icon()`.
- Produces: must keep verbatim `video-tag-pill suspect`, `suspect-badge`, `Suggest (model)`, `Suggest (zero-shot)` (i.e. `zero-shot`/`model)`), `Embed this video`, `video-meta` id. Accept `✔`→`icon('check')`, Reject `✘`→`icon('x')`, suspect `⚠`→`icon('warning', cls='suspect-badge')` (keep the `suspect-badge` class on it), source `🏷`/`🔮`→`icon('tag')`/`icon('wand')`, embed/suggest buttons `🧠`/`🏷`→`icon('embed')`/`icon('tag')`. Tag remove `×` may stay a glyph (not emoji) or become `icon('x')` at small size — keep its 24px hit-area.

- [ ] **Step 1: Confirm guarded strings** — `grep -nE "video-tag-pill suspect|suspect-badge|Suggest \(|Embed this video|video-meta|zero-shot" tests/test_http_smoke.py`.
- [ ] **Step 2: Swap emoji → icons** in the three functions; ensure the `btn-confirm`/`btn-reject` get the new `.btn-primary`(success-tinted)/`.btn-danger` roles, and suspect pills keep the `suspect-badge` class on the warning icon.
- [ ] **Step 3: Restyle the panel** via tokens — calmer spacing, confirmed vs suggested (dashed border + `--accent`) vs suspect (`--warn`) differentiated by real style, never opacity (audit A9). Keep `tag-score` legible (≥12px).
- [ ] **Step 4: Run the suite** — `uv run pytest -q`. Expected: PASS (`test_play_page_flags_suspect_tags`, `test_suggest_button_labels_*`, `test_play_page_offers_embed_when_unembedded` all rely on strings kept above).
- [ ] **Step 5: Visual check** — fixtures with a suggested-tag video, a suspect-tag video, an unembedded video; screenshot each panel state.
- [ ] **Step 6: Commit** — `git add -A && git commit -m "Restyle tagging panel & coverage controls with SVG icons"`.

---

## Task 9: Port download, unlock & error pages

**Files:**
- Modify: `src/simpleparty/render.py` — `render_download_form`, `_render_download_job_card`, `render_download_status`, `render_download_page`, `render_locked_page`, `render_error_page`.
- Modify: `src/simpleparty/static/style.css` — `.download-*`, `.unlock-box`, `.error-page`, `.notice`, `#transcode-notice`, progress-bar classes.

**Interfaces:**
- Consumes: `icon()`.
- Produces: keeps `download-board`, `download-progress`, progress-bar classes, `role=status/alert` regions, the unlock password `font-size:16px` (no-zoom), error `role="alert"`. Replace remaining emoji: download/queue `⬇`, play `▶`→`icon('play')`, folder `📁`, done `✅`→`icon('check')` (success color), error `❌`→`icon('x')` (danger), now `▶ Now`→`icon('play')`+"Now".

- [ ] **Step 1: Confirm guarded strings** — `grep -nE "download-board|download-progress|tag-progress-bar|No downloads yet" tests/test_http_smoke.py` (only `test_static_css` + general smoke touch these; download routes are disabled in the smoke fixture so most assert nothing here — verify before editing).
- [ ] **Step 2: Swap emoji → icons** and apply card/input/button system classes; inline `style="…"` blocks in these renderers (e.g. the download hint, error `<p>` color) move to token-based classes.
- [ ] **Step 3: Run the suite** — `uv run pytest -q`. Expected: PASS.
- [ ] **Step 4: Visual check** — run with `--no-download` off isn't possible in the smoke fixture; manually `uv run simpleparty <fixtures>` (downloads enabled by default), visit `/download`, screenshot the board (queue a fake/失败 URL to see error card), the unlock page (point at an fscrypt dir if available, else skip), and trigger an error page.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Restyle download, unlock & error pages"`.

---

## Task 10: Guard tests + full verification + README

**Files:**
- Modify: `tests/test_http_smoke.py` (add two guard tests)
- Modify: `README.md` (only if Task 2 chose Tailwind)

- [ ] **Step 1: Add a no-emoji guard test** asserting the redesigned pages contain no emoji from the replaced set:

```python
def test_pages_have_no_emoji(srv):
    sp_server._config['has_ffmpeg'] = True
    banned = ['⬇','\U0001F5D1','\U0001F9E0','\U0001F3F7','\U0001F52E','⚙',
              '⇅','\U0001F4C1','\U0001F512','\U0001F513','\U0001F3AC','✅',
              '❌','✔','✘','❓','⏳']
    for url in ['/', '/browse?path=sub', '/play?path=&idx=0&sort=name&dir=asc']:
        text = request(srv, 'GET', url)[2].decode()
        for ch in banned:
            assert ch not in text, f'emoji {ch!r} still in {url}'
```

- [ ] **Step 2: Add a token-discipline guard** for the stylesheet (no stray component hex outside `:root`):

```python
import re
def test_css_uses_tokens_not_raw_hex(srv):
    text = request(srv, 'GET', '/static/style.css')[2].decode()
    body = text.split('}', 1)[1] if ':root' in text else text  # drop the :root block
    # allow #fff/#000 shorthands; flag 6-digit hex in component rules
    leaks = re.findall(r'#[0-9a-fA-F]{6}', body)
    assert not leaks, f'raw hex outside tokens: {set(leaks)}'
```

- [ ] **Step 3: Run, expect FAIL if any leak/emoji remains**; fix the offending renderer/CSS (move hex into a token or `var()`, replace stray emoji). Iterate until both guards pass.
- [ ] **Step 4: Full suite** — `uv run pytest -q`. Expected: ALL PASS.
- [ ] **Step 5: Playwright a11y/regression sweep** — screenshot Browse + Player + Download at 1280, 360, 393, 412 portrait and 800×393 landscape. Verify against `docs/ui-audit-mobile-android-2026-06-05.md`: focus-visible ring on tab; every control ≥44px on phones; no horizontal overflow; suggested/suspect tags legible (AA); reduced-motion honored; toggle pressed-state visible. Save shots under `/tmp/sp-redesign/`.
- [ ] **Step 6: README** — if Task 2 chose hand-written CSS (default), leave the "Zero dependencies" claim as-is (still true). If Tailwind, change "Zero dependencies - pure Python standard library, nothing to install" and the "no build step" line to "Zero **runtime** dependencies" and document the `make tailwind` step.
- [ ] **Step 7: Bump version + commit** — bump `pyproject.toml` version (e.g. 0.10.0→0.11.0) and `__init__.py` if it carries `__version__` (the CSS/JS cache-bust `?v=` rides on it). `git add -A && git commit -m "Add redesign guard tests; verify a11y; bump version"`.

---

## Self-review notes

- **Spec coverage:** §1 design system → Tasks 2–4; §1 icons → Task 3; §2 browse restructure → Tasks 5–6; §2 player restructure → Tasks 7–8; §2 download/unlock/error → Task 9; acceptance criteria 1–2 → Task 10 token/type guards; criterion 4 (no emoji) → Task 10 guard; criteria 3,5,6 → Tasks 4–9; criterion 7 (features + a11y intact) → every task's `pytest` gate + Task 10 sweep. Carried-forward audit fixes → Task 4 Step 2 + Task 10 Step 5.
- **Mockup-derived values:** exact paddings/sizes/icon paths are intentionally sourced from the Task 1 approved mockup rather than guessed here; the token names, class contracts, load-bearing strings, and test code are fully specified.
- **Type consistency:** `icon(name, *, label=None, cls='icon')` is used with the same signature in all port tasks; class contracts (`item item-video`, `btn-train`, `video-tag-pill suspect`, `tag-progress`, ids for `play.js`) are listed once in Global Constraints and referenced per task.
