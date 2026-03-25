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

## Files Changed

1. **`index.html`** — Replace Google Fonts link (Cormorant Garamond, Libre Franklin, Fira Code)
2. **`src/styles/variables.css`** — New color tokens, font stacks, shadow values
3. **`src/styles/reset.css`** — Body background, selection color
4. **`src/styles/layout.css`** — Header, login page, welcome page, viewer layout, table, grid, tabs, etc.
5. **`src/styles/components.css`** — Buttons, badges, inputs, cards, modals, toasts, floating bar, etc.
6. **Inline style fixes in components** — Where inline styles hardcode colors/fonts that conflict with the new theme (e.g., `background: 'var(--color-neutral-50)'` references in App.tsx, DocumentViewer.tsx, etc.)

## Out of Scope

- No React component restructuring
- No new components
- No layout changes (column widths, padding structure, page flow)
- No dark mode toggle (the Archive theme is inherently light)
- No changes to backend or API
