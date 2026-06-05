# SimpleParty — Mobile (Android) UI Audit

**Date:** 2026-06-05 · **Version audited:** 0.9.6 · **Scope:** whole UI, with emphasis on Android phones.

## Context & method

All of SimpleParty's HTML, CSS and JS is generated inline in `src/simpleparty/server.py`
(CSS block `433–651`; page renderers from `654` on). There is no template engine and no build step,
so every fix below is a one-file CSS/markup edit.

The app was run locally against throwaway fixtures (12 videos with varied durations/sizes, long
filenames, a subfolder, a deep nested path, an empty folder, a 14-tag video, 3 starred, 1 AI-suggested)
on `127.0.0.1:8731` and driven with **Playwright** emulating a Pixel-class Android device at
**360 / 393 / 412 px** portrait + **800×393** landscape. For each page we captured full-page screenshots
**and** programmatic measurements: horizontal overflow, every interactive element's rendered size, the
font-size histogram, named-element rects, plus computed **WCAG contrast** ratios. Claims were
cross-checked against the source and adversarially re-verified (several first-pass hypotheses were
corrected — see *Corrected claims*).

Artifacts: screenshots in `/tmp/sp-audit/shots/`, raw metrics in `/tmp/sp-audit/measurements.json`.

## What's already good

- `<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">` is correct.
- **No horizontal page scroll** on normal content (`body{overflow-x:hidden;max-width:100vw}`).
- Responsive grid: 5 cols (>1024) / 3 (≤1024) / 2 (≤640 phones).
- **All three delete actions have `hx-confirm`** (grid `:826`, bulk-by-tag `:1038`, player `:1255`).
- Unlock password input is `font-size:16px` (avoids iOS auto-zoom).
- System font stack, native `<video>` controls, native `<select>` for speed (good on touch), dark theme.

## Severity legend
**Critical** = real layout breakage · **High** = blocks/erodes core mobile use or WCAG-A/AA ·
**Medium** = notable friction/polish · **Low/Nit** = minor.

---

## 1. Layout & responsiveness

### L1 · **Critical** — Long filenames blow out cards → double-height, ragged 2-col grid
A flex `min-width` omission lets an unbreakable long name/tag string force a card far wider than its
50% column, which `#file-list{overflow:hidden}` then clips — producing **double-height holes** in the grid.
*Evidence:* at 393 px the two longest-named cards measured **~600 px wide / 394 px tall** vs ~180 px for
every other card (distinct card heights `[178,181,193,194,204,394]`); visible in `browse-root-360-full.png`
row 2. This is the **only true horizontal-overflow source** in the app, and it also causes the clipped
size label ("59.1" with no "KB") and the "bare trash-circle over emptiness" cards — both downstream symptoms.
*Location:* `.item-video` (`server.py:515`) — it carries class `item item-video`, so it inherits `flex-wrap:wrap` from `.item` (`:509`) and never resets it.
*Root cause (empirically verified, **not** the `min-width:0` the first pass guessed):* `.item-video` is a column flex box that inherits `flex-wrap:wrap`; with `align-items:stretch` that sizes children to the *content* width (a long filename → 601px) instead of the card width.
*Fix (shipped):* `.item-video{flex-wrap:nowrap}`. Measured result: every card is now a uniform 161px. **One declaration, broadest payoff.**

### L2 · **Medium** — Deep breadcrumb wraps into a giant sticky header
`nav` is `flex-wrap:wrap` + `position:sticky`, so a deep path wraps to multiple rows and the "Downloads"
button drops to its own row. *Evidence:* 6-level path → nav measured **121 px tall**, Downloads reflowed
below the crumbs (`state-deep-breadcrumb-393.png`). `overflow:hidden` never truncates because wrapping
happens first. The tall block stays pinned and covers content on scroll.
*Location:* `server.py:441–446`, `render_nav 668–690`.
*Fix:* on phones collapse middle crumbs to "…", cap sticky height (or drop sticky ≤640 px), set `aria-current` on the last crumb.

### L3 · **Low** — Download form inputs are fragile (Queue flush to the edge, not clipped)
Both `.download-form` inputs have `min-width:200px`. At 360/393 the URL field takes row 1 and subdir+Queue
take row 2, with **Queue's right edge exactly at the viewport edge (0 gutter)** — tight, but **verified not
clipped** at 360/393/412. The fixed 200 px floor makes the row brittle.
*Location:* `server.py:574`. *Fix:* lower input `min-width` to ~120 px or stack fields on ≤640 px; add a right gutter.

### L4 · **Low** — Empty directory still renders the Download/Manage action bar
`want_action_bar` is true whenever downloads are enabled, so an empty folder shows "Download URL" + "Manage"
above "Empty directory" (`state-empty-393.png`). *Location:* `server.py:724`. *Fix:* skip the bar when no videos **and** no dirs.

### L5 · **Low** — Empty "No downloads yet" board on every browse page; `.action-bar` stretch
`/download-status` returns a `.download-board` containing `.empty{padding:40px 20px}`, which renders on
each browse page. Because `.action-bar` has no `align-items` (defaults to `stretch`), an adjacent wrapped
item can make the "Manage" button stretch tall (**conditional** on what wraps onto its line — not guaranteed).
*Location:* `server.py:539`, `538`, `773`. *Fix:* `.action-bar{align-items:flex-start}` + don't render the empty board on browse.

### L6 · **Low** — Tag dropdown can clip past the right edge
`.tag-dropdown{position:absolute;left:0;min-width:220px}` anchored left; when the search input sits in the
right half of a wrapped filter row, the menu overruns `100vw` and is clipped by `overflow-x:hidden`.
*Location:* `server.py:605`. *Fix:* `right:0/left:auto` fallback or `max-width:calc(100vw - 16px)`.

---

## 2. Touch targets & ergonomics

### T1 · **High** — Nearly every control is below the 44 px touch target; some below the 24 px AA floor
Measured at 360–412 px: nav crumb **96×28**, star-pill / sort-pills / tag-search **~24–29 px tall**, `.btn` **40 px**,
`.btn-skip` **45×32**, `#btn-star` **38×40**, "🗑 Delete all" **117×24**, and the suggested-tag remove "×" **8×10**.
WCAG 2.5.5 wants ≥44 px; several (× at 8×10, pills at 24–25) miss even the 2.5.8 AA 24 px minimum.
*Fix:* raise control `min-height` to 44 px under `@media(max-width:640px)`; give pills and the "×"/`.tag-pill-x`
real padding hit-areas (`padding:6px;margin:-6px` keeps the glyph small).

### T2 · **High** — Destructive "Delete all (N)" sits beside benign "Clear all", same size/color
`browse-tag-nature-393-full.png`: `[nature ×] [Clear all] [🗑 Delete all (4)]` — "Clear all" (61×25, removes a
filter) and "Delete all" (117×24, **irreversibly deletes files**) are adjacent, both small and grey; only the
emoji + confirm text differ. *Location:* `server.py:1026` vs `1041–1044`, CSS `530/601`.
*Fix:* separate them, give Delete-all a red treatment, enlarge to ≥44 px; consider a stronger confirm for bulk delete.

### T3 · **Medium** — Player star & delete icons 8 px apart, both icon-only
`#btn-star` (38×40, ☆) and the delete 🗑 (43×42) sit 8 px apart (`play-393-full.png`). The **star is an instant,
unconfirmed `fetch`** (only a 600 ms flash) so a mis-tap silently toggles it; delete at least has `hx-confirm`.
*Location:* `server.py:1247–1259`, `1311–1323`. *Fix:* separate the destructive control, label/enlarge both, add an undo for star.

---

## 3. Accessibility (WCAG)

### A1 · **High** — No `:focus-visible` ring anywhere; keyboard focus is invisible
Only 5 `:focus` rules exist, all on text inputs (three set `outline:none`). No focus style for `.btn`, `.crumb`,
`.tag-pill`, `.sort-pill`, `.item-link`, `.playlist-item`, etc., so tabbing relies on the browser default outline,
which is near-invisible on the dark surfaces. WCAG 2.4.7. *Fix:* `a,button,select:focus-visible{outline:2px solid #a78bfa;outline-offset:2px}`.

### A2 · **High** — Icon-only buttons have no accessible name
Delete (`:828`), player star (`:1247`), player delete (`:1258`), and the "×" remove buttons expose only a
`title` tooltip; their accessible name becomes the literal emoji (or nothing). WCAG 4.1.2 / 2.5.3.
*Fix:* add `aria-label` to each, `aria-hidden="true"` on the inner glyph.

### A3 · **High** — Toggle buttons don't expose pressed state
`#btn-star`, `#btn-autoplay`, `#btn-repeat` convey on/off only via a class + emoji — no `aria-pressed`.
*Fix:* set/maintain `aria-pressed`.

### A4 · **High** — Tag-filter "combobox" isn't keyboard operable
The filter is an `<input>` + `<div>` of `<a>` links wired only to focus/input/outside-click — **no arrow/Enter/Escape**,
no `role=combobox/listbox/option`, no `aria-expanded`. WCAG 2.1.1 / 4.1.2.
*Location:* `server.py:1056–1088`. *Fix:* use a native `<datalist>`, or add ARIA + a keydown handler.

### A5 · **Medium** — No landmarks, no `<h1>`, headings start at `<h3>`
No `<main>`, no `<h1>`; the only headings are `<h3>` (related/playlist/unlock). WCAG 1.3.1 / 2.4.6.
*Fix:* wrap content in `<main>`, give `<nav aria-label="Breadcrumb">`, add an `<h1>` (folder name / video title), promote section headings to `<h2>`.

### A6 · **Medium** — Inputs labelled only by placeholder
`#tag-search`, `.video-tag-add`, and the download fields have no `<label>`/`aria-label`. WCAG 3.3.2. *Fix:* add `aria-label`s.

### A7 · **Medium** — No live regions for progress / toast feedback
Only one `role=status` exists (transcode notice). The htmx tag- and download-progress panels and the play-page
flash overlay (including the **"Star failed"** error) are silent to assistive tech. WCAG 4.1.3.
*Fix:* `role=status aria-live=polite` on the progress panels; mirror flash text into a visually-hidden `role=alert`.

### A8 · **Medium** — Global single-key shortcuts include `d` = delete
The play-page `keydown` map fires unmodified keys (incl. `d`→delete, `:1341`) whenever focus isn't on an
INPUT/SELECT — so focus on a link/button still lets `d`/`f`/`m` fire. WCAG 2.1.4.
*Fix:* also exclude BUTTON/A/[contenteditable], or require a modifier / provide an off switch.

### A9 · **Medium** — AI-suggested tags signalled only by `❓` + 50 % opacity
Reduced opacity is invisible to AT and drops contrast to ~2.13 (fails AA). *Location:* `server.py:838–839`, `553–554`, `616`.
*Fix:* add a real "Suggested" text/badge and a sufficient-contrast style instead of opacity.

### A10 · **Medium** — No `prefers-reduced-motion` guard
Infinite `spin`/`pulse` and `fadeIn` animations (`:559,567,569,570`) run unconditionally. WCAG 2.3.3.
*Fix:* add a `@media(prefers-reduced-motion:reduce)` block.

### A11–A13 · **Low** — Emoji read literally (lock/unlock state is emoji-only); breadcrumb not marked up as a nav/list with `aria-current`; hover-only feedback (delete reveal, pill hover) never appears on touch/keyboard.

### Contrast (objective, WCAG 1.4.3) · **Medium**
Computed ratios that **fail AA (4.5)** for normal text: `.item-size`/`.item-tags` `#64748b` = **3.34**;
`.tag-pill-count` = **2.81**; download `.url` = **3.58**; suggested-tag (opacity 0.5) ≈ **2.13**.
Primary text/names and `#94a3b8` muted text pass. *Fix:* lighten the muted greys; stop using opacity for "suggested".

---

## 4. Player & interaction

### P1 · **High** — No touch gestures despite a full desktop keymap
There are rich `keydown` shortcuts (next/prev/skip/speed/pause/fullscreen) but **zero** touch/pointer handlers.
On a phone the only way to advance is to scroll past the controls and tap the small Prev/Next buttons.
*Fix (~15 lines, reuses `skip()`/`nextUrl`):* swipe → next/prev, double-tap halves → ±10 s, center tap → play/pause.

### P2 · **High** — Autoplay/Repeat ON state is essentially invisible
`updateAutoBtn()` sets the label to `"Autoplay"` in **both** states (`:1305`); state shows only as a subtle purple
background + a 600 ms flash. *Fix:* stateful labels ("Autoplay: On/Off", "Repeat: Off/All/One").

### P3 · **High** — Landscape pushes all controls off-screen
`video{max-height:70vh}` fills the landscape fold; the only responsive block is **width-based** `@media(max-width:640px)`,
which doesn't fire at 800 px landscape, so every transport control is below the fold (`play-landscape-800-fold.png`).
*Fix:* add `@media(orientation:landscape) and (max-height:500px)` capping the video (~55–60vh) or overlaying compact controls.

### P4 · **Medium** — The whole video+controls block is sticky
`#player-area` (video **and** the 151 px control row, measured **379 px** total) is `position:sticky`, so ~half the
viewport stays pinned while scrolling the playlist/related. *Fix:* make only the `<video>` sticky.

### P5 · **Medium** — `#now-playing{flex:1}` forces controls to 3 rows
The position counter grabs the row's free space, wrapping the controls to **151 px / 3–4 rows**. *Fix:* on ≤640 px set
`#now-playing{flex-basis:100%}` or hide it (the playlist already shows position) → controls collapse toward 2 rows.

### P6 · **Medium** — Sort/filter taps are full-page reloads
Sort pills, the star pill, selected-tag pills, "Clear all" and dropdown tags are plain `<a href>` — each tap reloads
the whole document (white flash, scroll-to-top). *Fix:* `hx-get` → `#file-list` + `hx-push-url=true` (reuses `render_file_list`).

### P7 · **Medium** — Tag dropdown lacks an empty state & close affordance
A non-matching query leaves a silent empty box; the only way to dismiss is tapping outside (no ×/Esc); it overlaps the
action bar. *Fix:* add a "No matching tags" row, an Esc/close handler, a stronger shadow, and open only after ≥1 char.

### P8–P9 · **Low** — No quick "reset speed to 1×" on touch; related/playlist names clip to one line (these rows are the de-facto touch navigation — allow 2 lines and strengthen the "playing" highlight).

---

## 5. Visual polish & consistency

- **V1 · Medium** — Incoherent type scale: 10/11/12/13/14/15/16/18/20/32 px in use, with 1 px-apart accidental steps (crumb 15 vs `.btn` 14). Collapse to a ~5-step ramp via CSS vars.
- **V2 · Medium** — Play-page hierarchy inverted: 151 px of dense controls dominate a 203 px video; let the video be the hero.
- **V3 · Low** — Inconsistent icon weight (`⬇️` with FE0F on "Download URL" vs flat `⬇` elsewhere); emoji clash with the flat theme — consider an inline-SVG set.
- **V4 · Low** — Sort pills look identical to tag-filter chips (sort vs filter ambiguity); differentiate the shapes.
- **V5 · Low** — Inconsistent horizontal gutters (nav 12 / tag-filter 16 / grid 8 / board 16); adopt one gutter token.

---

## 6. Untested-state risks (code-grounded predictions, not live-verified)

- **U1 · Medium** — **No-ffmpeg mode**: every card becomes a clapperboard placeholder and the whole tag column disappears with no explanation (`:727`, `807–813`). Show a "Install ffmpeg for thumbnails & tagging" notice; make the placeholder an intentional poster.
- **U2 · Medium** — **Active download card**: progress meta is one long ` · `-joined string that wraps unpredictably at 360 px, and there's **no Cancel control for a running download** (only for queued) (`1805–1888`).
- **U3 · Medium** — **Transcode notice** (~60 px) sits above the already-pinned player with no dismiss (`1208–1214`).
- **U4 · Low** — **Unlock error** has no `role=alert`, 13 px text, and may reflow the button on a 2-line error (`1109–1129`).
- **U5 · Low** — **Error page** is a bare centered red string, no heading/back/`role=alert` (`1132–1135`).
- **U6 · Medium** — **Many tags**: 14 pills wrap to ~5–6 rows (verified, `state-many-tags-play-393.png`), pushing the player/playlist far down; cap visible pills with a "+N more" toggle on phones.

---

## Corrected claims (transparency)

First-pass hypotheses that re-verification **overturned or softened** — listed so they don't get re-filed:

- *"Manage button always stretches to ~120 px on every browse page"* → **conditional** on what wraps onto its flex line; not guaranteed. (Still worth `align-items:flex-start`.)
- *"Card size truncates to '59.1'"* → a **downstream symptom of L1**, not a separate size-formatting bug.
- *"Download Queue button is clipped off-screen" (flagged by two reviewers)* → **refuted by measurement**: Queue's right edge == viewport edge at 360/393/412 (flush, 0 gutter) but **not clipped**. Re-filed as L3 (fragile, no gutter).
- *"Landscape video is pillarboxed, wasting width"* → the video **element** is full-width; the real landscape issue is **P3** (controls pushed off-fold) plus the 70vh cap.

---

## Prioritized fix list (impact ÷ effort)

1. **L1** — `min-width:0;overflow-wrap:anywhere` on `.item-name`/`.item-tags` — fixes the only real layout break + 2 symptoms. *(~2 lines)*
2. **A1** — global `:focus-visible` ring. *(~3 lines, large a11y win)*
3. **T1** — control `min-height:44` + pill/× hit-areas on ≤640 px.
4. **P2 / A3** — stateful Autoplay/Repeat labels + `aria-pressed`.
5. **T2** — separate & red-style "Delete all"; harden bulk delete.
6. **P5 / P4** — `#now-playing{flex-basis:100%}` and make only the video sticky (reclaims ~150 px).
7. **Contrast / A9** — lighten muted greys; drop opacity-as-state for suggested tags.
8. **A2 / A4 / A7** — `aria-label`s, keyboard-operable filter, live regions.
9. **P6** — htmx-ify sort/filter pills (no full reloads).
10. **P1 / P3** — touch gestures + a landscape media query.

**If only one change ships:** do **L1** — it's the only defect that actually breaks layout; everything else degrades gracefully.

## Reproduce
```bash
# fixtures: a dir of small ffmpeg-generated videos + .simpleparty/tags.json (see /tmp/sp-audit)
uv run simpleparty /path/to/videos -p 8731 -b 127.0.0.1
# drive with Playwright (global module) emulating Pixel-class Android at 360/393/412 + 800x393
node /tmp/sp-audit/audit.js     # screenshots -> /tmp/sp-audit/shots, metrics -> measurements.json
```
