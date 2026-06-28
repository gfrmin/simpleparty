# SimpleParty — Frontend Redesign (Design Spec)

**Date:** 2026-06-28 · **Scope:** whole UI (5 page types) — visual system, icon set,
and two layout restructures. **Presentation only:** no backend/route/htmx/URL changes,
no new features.

## Goal

The frontend is "a mess" in four ways the user named: visual design, CSS code quality,
layout/UX flow, and cross-page inconsistency. The 2026-06-05 mobile/a11y audit already
fixed *correctness* (touch targets, focus rings, contrast, landscape, reduced-motion) —
those wins are **carried forward, not discarded**. This redesign addresses the
*aesthetic + structural* layer the audit didn't: a real design system, role-encoded
components, and decluttered layouts.

Direction (user-approved): **modern dark, new identity**, **one committed palette**,
inline-SVG icons replacing emoji.

## What's wrong today (the audit)

Grounded in `src/simpleparty/render.py` + `src/simpleparty/static/style.css`:

- **No token system.** ~12 ad-hoc hex values used inconsistently; the audit-fix tail at
  the bottom of `style.css` re-overrides several. No semantic naming.
- **Type chaos:** 10/11/12/13/14/15/16/18/20/32px in use (audit V1, only partially patched).
- **Buttons don't encode role.** Primary, secondary, toggle, and destructive *delete* all
  render as the same grey `.btn`. Sort pills, tag pills, and the star pill look nearly
  identical (sort-vs-filter ambiguity, audit V4).
- **Emoji icons** (⬇ 🗑 ★ 🧠 🏷 🔮 ⚙ ▶ ◀ ⇅ 📁 🔒 🎬 ✔ ✘ ⚠) clash with the flat theme and
  vary in weight (audit V3).
- **Browse action bar** crams view-controls (Shuffle, 4 sort pills), library-management
  (Embed all / Embed selected / Train, coverage badge), and status (tag + download
  progress) plus Download/Manage into one wrapping flex — no grouping or hierarchy.
- **Player controls row** stuffs ~10 controls into one wrapping row; controls dominate the
  video, which should be the hero (audit V2).

## Section 1 — Design system

A single tokenized layer, consumed by both the Claude Design mockups and the final CSS.

### Committed palette (modern dark)

```
--bg:            #0a0c10   /* app background, near-black cool */
--surface:       #12151c   /* cards, nav, panels */
--surface-2:     #1a1f29   /* raised: pills, inputs, hover base */
--surface-hover: #222836   /* row/card hover */
--border:        #262c38   /* hairlines, input borders */
--text:          #e6e9ef   /* primary */
--text-muted:    #9aa4b2   /* secondary — must meet AA (≥4.5:1) on surface */
--text-faint:    #7a8494   /* tertiary; large/non-essential text only */
--accent:        #6366f1   /* indigo — primary actions, active state */
--accent-hover:  #818cf8
--accent-weak:   rgba(99,102,241,0.14)  /* tinted backgrounds, focus glow */
--on-accent:     #ffffff   /* text/icon on accent fills */
--danger:        #ef4444   /* delete / destructive */
--danger-weak:   rgba(239,68,68,0.16)
--warn:          #f59e0b   /* transcode notice, suspect-tag */
--success:       #34d399   /* done states */
--star:          #facc15   /* favourite */
```

Constraint: any grey used for **normal-size body text** must clear WCAG AA 4.5:1 on its
surface (the audit's contrast failures must not return). `--text-faint` is reserved for
large text or decorative counts.

### Type ramp (replaces the 10→32 sprawl)

```
--text-xs: 12px    /* counts, meta, fine print (floor — no sub-12px) */
--text-sm: 13px    /* secondary labels, pills */
--text-base: 15px  /* body, item names, buttons */
--text-lg: 18px    /* section headings, video title */
--text-xl: 22px    /* page heading */
--text-display: 28px  /* library / brand wordmark where used */
```

Line-height: `--lh-tight: 1.2`, `--lh: 1.45`.

### Spacing / radius / elevation

```
--space-1: 4px  --space-2: 8px  --space-3: 12px  --space-4: 16px  --space-5: 24px  --space-6: 32px
--radius-sm: 6px  --radius-md: 8px  --radius-lg: 12px  --radius-pill: 999px
--shadow: 0 4px 16px rgba(0,0,0,0.4)   /* single elevation token, for dropdown/overlay */
```

### Icon set

One inline-SVG set (~16 glyphs, 24×24, `currentColor`, `stroke-width` consistent),
replacing every emoji: download, trash, star (outline/filled), brain/embed, tag, magic
(zero-shot), gear/transcode, play, prev, next, shuffle, folder, lock, lock-open, check,
x, warning, clock. Each icon is `aria-hidden="true"` inside a control that carries a real
`aria-label` (preserves the existing accessible-name fixes A2/A3).

### Component vocabulary (role-encoded)

- **Buttons:** `.btn` = secondary/ghost (default), `.btn-primary` = accent fill,
  `.btn-toggle` = on/off with visible pressed state + `aria-pressed` (keeps A3),
  `.btn-danger` = destructive (delete, lock). One height token; ≥44px on phones (keeps T1).
- **Pills, differentiated:** **sort** becomes a **segmented control** (joined buttons with
  a sliding/active fill); **tag filters** stay rounded chips; **star filter** uses the
  `--star` color so it reads as a distinct mode. Resolves sort-vs-filter ambiguity.
- **Inputs:** one field style (bg `--surface-2`, border `--border`, focus ring
  `--accent-weak`), 16px font on the unlock password (keeps the iOS no-zoom win).
- **Cards / panels / progress bars:** one shared surface + border + radius recipe.

## Section 2 — Per-page layout restructure

### Browse (`render_browse_page`, `render_file_list`, `render_tag_filter`)
Split the jumbled action bar into a **toolbar with clear zones**:
- **View zone (left):** Shuffle Play, sort segmented control, tag filter (star pill +
  selected chips + searchable dropdown).
- **Library zone (right / secondary):** Embed (all-missing + selected) and Train grouped
  under the coverage badge; Download (URL + Manage) grouped. Visually subordinate to the
  view zone — these are occasional management actions, not primary navigation.
- **Status:** tag-progress and download-progress become a slim inline strip that appears
  only when active, not buttons competing for attention.
- **Cards:** keep thumbnail-first layout; tighten name/size/tags typography with the new
  scale; delete control stays an overlay but uses the SVG trash + danger color on hover.

### Player (`render_play_page`)
Let the **video be the hero**; restructure the control row into tiers:
- **Primary transport:** Prev / Next, prominent.
- **Secondary:** skip cluster (−30/−10/+10/+30) + speed select, grouped.
- **Toggles:** Shuffle / Autoplay / Repeat as `.btn-toggle` with *visible* state and
  stateful intent (keeps P2/A3 — on/off must be obvious).
- **Destructive:** Delete separated from the cluster, `.btn-danger`.
- **Star:** separated, `--star` styling, distinct from delete (keeps T3).
- **Tagging panel (`#video-meta`):** calmer accept/reject/suggest flow with the new button
  hierarchy; suggested vs confirmed vs suspect tags differentiated by real styling (badge
  + color), never by opacity alone (keeps A9).

### Download / Unlock / Error
Restyled by **applying the system** (cards, inputs, buttons, icons). No structural change —
consistency does the work. Keep `role=alert`/`role=status` regions intact.

## Section 3 — Workflow

1. **Claude Design (MCP):** build mockups for the two hero pages (Browse + Player) using
   the committed palette + tokens above; render in-browser for user approval. Smaller pages
   are derived from the same system, not separately mocked.
2. **Port:** translate the approved design into `render.py` markup + the stylesheet,
   **preserving every feature, htmx attribute, and URL**. Recommend the CSS approach
   (Tailwind-compiled-to-static vs. hand-written CSS custom properties) *at this point*,
   based on what the approved design needs — user deferred this decision to the mockups.
   - *Note for the Tailwind branch:* it adds a build step + dev-time tool and requires
     softening the README's "zero dependencies / no build step" claim to "zero **runtime**
     dependencies." The hand-written branch keeps the claim fully intact. Either way the
     shipped artifact is one `style.css`.
3. **Verify:** Playwright screenshots at desktop + audit phone widths (360/393/412 portrait
   + 800×393 landscape); re-confirm the carried-forward a11y fixes (focus rings, 44px
   targets, contrast, reduced-motion, accessible names, pressed state) still hold.

## Scope & non-goals

- **In:** all 5 page types; design tokens; SVG icon set; button/pill role hierarchy; the
  browse-toolbar and player-control-tier restructures.
- **Out:** backend, routes, htmx behavior, URL shapes, feature set. Pure presentation.
- **Carried forward:** every fix from `docs/ui-audit-mobile-android-2026-06-05.md`.

## Acceptance criteria

1. All color/spacing/type/radius values flow from the token layer; no stray ad-hoc hex in
   component rules.
2. Type sizes are confined to the 6-step ramp (no sub-12px text).
3. Buttons visually encode role (primary/secondary/toggle/danger); sort vs. filter is
   unambiguous.
4. No emoji in the rendered UI; all icons are inline SVG with accessible labels.
5. Browse toolbar and player controls are grouped into the zones/tiers above.
6. All five pages share one coherent visual identity.
7. Every existing feature and htmx interaction works unchanged; all 2026-06-05 a11y fixes
   verified intact.
