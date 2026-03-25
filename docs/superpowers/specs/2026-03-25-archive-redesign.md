# Vigilist "Archive" Visual Redesign

## Summary

Restyle Vigilist from its current generic admin-dashboard aesthetic to the "Archive" theme: warm parchment backgrounds, deep ink-blue accents, Cormorant Garamond headings, and subtle ruled-line texture. The goal is a distinctive, scholarly feel — like a well-organized legal reference desk — without sacrificing readability during long review sessions.

This is a CSS/style-only change. No React component restructuring. Only touch component files where inline styles conflict with the new theme.

## Palette

### Core Colors (CSS custom properties)

| Token | Value | Use |
|-------|-------|-----|
| `--color-parchment-light` | `#f5f0e8` | Page background (gradient start) |
| `--color-parchment-mid` | `#ebe4d8` | Page background (gradient end) |
| `--color-parchment-warm` | `#f2ede5` | Gradient midpoint for depth |
| `--color-ink` | `#2c3e6b` | Primary text, headings, logo, active states |
| `--color-ink-light` | `#4a5f8a` | Secondary text, hover states |
| `--color-ink-faint` | `rgba(44,62,107,0.1)` | Borders, dividers |
| `--color-card` | `#ffffff` | Card/panel backgrounds (solid, not translucent) |
| `--color-card-hover` | `#faf7f2` | Card/row hover state |
| `--color-rule` | `#8b7355` | Ruled-line texture color (at ~3.5% opacity) |
| `--color-margin` | `rgba(183,28,28,0.06)` | Red margin line accent |

### Semantic Colors

Muted, ink-on-paper feel — lower saturation than current:

| Token | Value | Use |
|-------|-------|-----|
| `--color-success-ink` | `#2e7d32` | Responsive tags, positive states |
| `--color-danger-ink` | `#b71c1c` | Privilege tags, errors, destructive actions |
| `--color-warning-ink` | `#e65100` | Processing states, warnings |
| `--color-brand-ink` | `#2c3e6b` | Brand accent (same as ink) |

Semantic badge backgrounds use the ink color at 8-10% opacity with a 1px border at 12-15% opacity.

## Typography

### Font Stack

| Token | Value | Use |
|-------|-------|-----|
| `--font-serif` | `'Cormorant Garamond', Garamond, 'Times New Roman', serif` | Logo, headings, section titles, empty-state text |
| `--font-sans` | `'Libre Franklin', 'Segoe UI', system-ui, sans-serif` | Body text, buttons, labels, UI chrome |
| `--font-mono` | `'Fira Code', ui-monospace, 'Cascadia Code', Consolas, monospace` | Bates numbers, counts, code, search input |

### Google Fonts Import

Replace the current font import in `index.html`:

```
Cormorant+Garamond:wght@400;600;700
Libre+Franklin:ital,wght@0,400;0,500;0,600;0,700;1,400
Fira+Code:wght@400;500
```

### Scale

Keep the existing rem-based scale (`--text-xs` through `--text-3xl`). No changes needed — the fonts themselves carry the personality shift.

## Background Texture

Applied to `body` or the root layout container:

1. **Parchment gradient:** `linear-gradient(175deg, #f5f0e8 0%, #ebe4d8 50%, #f2ede5 100%)`
2. **Ruled lines:** `repeating-linear-gradient(0deg, transparent, transparent 23px, rgba(139,115,85,0.035) 23px, rgba(139,115,85,0.035) 24px)` — via a `::before` pseudo-element, `position:fixed`, full viewport, `pointer-events:none`
3. **Margin line:** A fixed `::after` pseudo-element, `left:72px`, `width:1px`, `background:rgba(183,28,28,0.06)`

The ruled lines and margin line are purely decorative atmosphere. They sit behind all content and don't interact with scrolling (they're fixed to the viewport).

## Component Changes

### Header (`.app-header`)

- Background: `rgba(44,62,107,0.03)` (very faint ink wash over parchment)
- Border-bottom: `2px solid #2c3e6b` (strong ink rule)
- Logo: Cormorant Garamond, 700 weight, ink-blue color
- Header buttons: `background:transparent`, `border:1px solid rgba(44,62,107,0.2)`, `color:rgba(44,62,107,0.6)`
- On hover: `border-color:rgba(44,62,107,0.4)`, `color:#2c3e6b`

### Cards (`.card`)

- Background: `#ffffff` (solid white)
- Border: `1px solid rgba(44,62,107,0.1)`
- Box-shadow: `0 1px 3px rgba(44,62,107,0.04)`
- Border-radius: keep `--radius-lg` (10px)

### Table (`.doc-table`)

- Header row: `background:rgba(44,62,107,0.02)`, `border-bottom:2px solid rgba(44,62,107,0.08)`
- Header text: ink-blue at 45% opacity, uppercase, 0.05em tracking
- Body rows: `border-bottom:1px solid rgba(44,62,107,0.05)`
- Hover: `background:var(--color-card-hover)` with a `3px solid var(--color-ink)` left border that transitions in (the micro-interaction)
- Bates cells: Fira Code, ink-blue, 500 weight

### Badges/Tags

- Desaturated ink-on-paper style: colored ink at 8% opacity background, 12% border, 70-80% text
- Remove the current bright Tailwind-style badge colors
- Example responsive tag: `background:rgba(46,125,50,0.1)`, `border:1px solid rgba(46,125,50,0.15)`, `color:rgba(46,125,50,0.8)`

### Buttons

- Primary: `background:#2c3e6b`, `color:#fff`, `border:1px solid #243356`
- Primary hover: `background:#243356`
- Secondary: `background:#fff`, `border:1px solid rgba(44,62,107,0.2)`, `color:rgba(44,62,107,0.7)`
- Ghost: `background:transparent`, `color:rgba(44,62,107,0.6)`, hover → `background:rgba(44,62,107,0.04)`

### Inputs

- Background: `#fff`
- Border: `1px solid rgba(44,62,107,0.15)`
- Focus ring: `box-shadow:0 0 0 3px rgba(44,62,107,0.12)`
- Placeholder: `color:rgba(44,62,107,0.3)`

### Tabs

- Inactive: `color:rgba(44,62,107,0.45)`
- Active: `color:#2c3e6b`, `font-weight:600`
- Active indicator: `2px solid #2c3e6b` bottom border (currently uses brand-500 gold — change to ink-blue)
- The indicator should `transition: left 200ms ease, width 200ms ease` for a smooth slide effect

### Login Page (`.login-page`)

- Background: `#2c3e6b` with `radial-gradient(ellipse 80% 60% at 50% 0%, #3a5080, transparent)` for subtle depth
- Login card: solid white, same as cards above
- Logo: Cormorant Garamond, ink-blue

### Welcome Page

- Background: Same as login — deep ink-blue
- Title: Cormorant Garamond, white
- Feature cards: `background:rgba(255,255,255,0.05)`, `border:1px solid rgba(255,255,255,0.1)`

### Modals

- Overlay: `rgba(44,62,107,0.3)` (ink-tinted, not black)
- Panel: solid white, `border-radius:var(--radius-xl)`

### Floating Bar

- Background: `#2c3e6b` (ink-blue instead of near-black)

### Scrollbars (optional polish)

Thin, ink-blue tinted scrollbar thumb on webkit browsers.

## Motion

All CSS-only. No JS animation library needed.

### Table Row Hover

```css
.doc-table tbody tr {
  border-left: 3px solid transparent;
  transition: background 100ms ease, border-color 150ms ease;
}
.doc-table tbody tr:hover {
  background: var(--color-card-hover);
  border-left-color: var(--color-ink);
}
```

### Tag Flash

Keep existing `flash-success` keyframe but use parchment-tinted colors:

```css
@keyframes flash-success {
  0% { background: rgba(46,125,50,0.08); }
  100% { background: transparent; }
}
```

### Tab Indicator Slide

The active tab underline transitions smoothly when switching tabs. This requires a small structural change: instead of `::after` on each tab, use a single sliding indicator element positioned with JS-set `left`/`width` CSS custom properties.

Alternatively, keep the `::after` approach and add `transition: opacity 150ms ease` for a crossfade effect (simpler, still noticeable).

**Recommendation:** Crossfade — simpler, no structural change needed.

### Modal Entrance

```css
.modal-panel {
  animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
}
@keyframes modal-in {
  from { opacity: 0; transform: scale(0.96) translateY(8px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
```

### Dropdown

Keep existing `dropdown-in` animation, adjust timing to 180ms.

### Toast

Keep existing, adjust colors to Archive palette.

## Token Migration Strategy

**The existing token names (`--color-neutral-*`, `--color-primary-*`, `--color-brand-*`) are redefined in-place, not renamed.** This is critical because ~200+ inline style references across 29 component files use these tokens. Introducing new names would force a massive component-by-component migration that violates the CSS-only scope.

The approach:
- `--color-neutral-*` values shift from cool grays to warm parchment-tinted grays
- `--color-primary-*` values shift from navy-slate to the ink-blue scale
- `--color-brand-*` values shift from gold/amber to ink-blue (the brand IS the ink)
- New semantic aliases (`--color-ink`, `--color-parchment-light`, etc.) are added as convenient shorthand but are not required — existing token references just work

### Shadow Scale

All shadows shift from near-black (`rgba(13,17,23,...)`) to ink-blue tinted (`rgba(44,62,107,...)`):

- `--shadow-xs`: `0 1px 2px rgba(44,62,107,0.04)`
- `--shadow-sm`: `0 1px 3px rgba(44,62,107,0.06), 0 1px 2px rgba(44,62,107,0.03)`
- `--shadow-md`: `0 4px 8px -1px rgba(44,62,107,0.07), 0 2px 4px -2px rgba(44,62,107,0.04)`
- `--shadow-lg`: `0 12px 24px -4px rgba(44,62,107,0.1), 0 4px 8px -4px rgba(44,62,107,0.05)`
- `--shadow-xl`: `0 24px 48px -8px rgba(44,62,107,0.14), 0 8px 16px -6px rgba(44,62,107,0.06)`
- `--shadow-ring`: `0 0 0 3px rgba(44,62,107,0.12)` (was gold)

### Focus & Selection

- `:focus-visible` outline: `2px solid rgba(44,62,107,0.5)` (was gold)
- `::selection`: `background:rgba(44,62,107,0.12); color:#2c3e6b` (ink-tinted)

### Small Components Not Previously Listed

- **Spinner**: `border-top-color` changes from gold to `var(--color-primary-600)` (ink-blue) — automatic via token remap
- **`mark` (search highlight)**: `background:rgba(44,62,107,0.1); color:#2c3e6b` — ink-tinted highlight
- **`.ai-indicator`**: Keeps a distinct accent to differentiate AI content. Use `--color-warning-ink` (`#e65100`) background at 8% opacity with orange-tinted text — stands out from the dominant ink-blue
- **`.kbd`**: Automatic via neutral token remap — no explicit change needed
- **`progress` elements**: Accent color should follow ink-blue

## Heavy-Inline-Style Components

These components have 15+ inline style references to CSS tokens. Because the old tokens are redefined in-place, they theme passively — no file edits required unless specific overrides look wrong after the remap:

| Component | Inline styles | Notes |
|---|---|---|
| `Dashboard.tsx` | ~56 | Stat cards, progress bars, bar charts — all use `--color-primary-*` and `--color-neutral-*` tokens |
| `QCReview.tsx` | ~48 | Full-screen overlay with its own header |
| `QueueManager.tsx` | ~47 | Tables, status badges, progress indicators |
| `IngestWizard.tsx` | ~35 | Step indicators, progress bars |
| `BatchReview.tsx` | ~30 | Dark header bar, side panel, progress dots |
| `AnnotationPopover.tsx` | ~26 | Uses non-standard tokens with hardcoded fallbacks — works independently |
| `ManageAccess.tsx` | ~16 | User management cards |

### Full-Screen Overlay Headers (BatchReview, QCReview)

These components use `var(--color-primary-900)` for a dark contrasting header bar. After the remap, `--color-primary-900` will be a deep ink-blue (`#102a43` → keeping it dark). **These headers intentionally stay dark** — they represent a focused review mode distinct from the main list view. The ink-blue just replaces the navy.

## Files Changed

1. **`index.html`** — Replace Google Fonts link (Cormorant Garamond, Libre Franklin, Fira Code)
2. **`src/styles/variables.css`** — Redefine all color tokens in-place to Archive palette, add semantic aliases, update font stacks and shadow values
3. **`src/styles/reset.css`** — Body background gradient, `::selection`, `:focus-visible`
4. **`src/styles/layout.css`** — Header, login page, welcome page, viewer layout, table hover interactions, grid, tabs, background texture pseudo-elements
5. **`src/styles/components.css`** — Buttons, badges, inputs, cards, modals, toasts, floating bar, spinner, mark, ai-indicator, etc.
6. **Inline style fixes** — Only where hardcoded hex values or removed tokens cause visual breakage. Expected to be minimal given in-place token redefinition.

## Out of Scope

- No React component restructuring
- No new components
- No layout changes (column widths, padding structure, page flow)
- No dark mode toggle (the Archive theme is inherently light)
- No changes to backend or API
