# Archive Visual Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle Vigilist from its generic admin-dashboard aesthetic to the "Archive" theme — warm parchment, ink-blue accents, Cormorant Garamond headings, ruled-line texture, subtle micro-interactions.

**Architecture:** Pure CSS restyling. Redefine existing CSS custom property values in-place (no renames) so 200+ inline style references across 29 React components theme passively. Only touch `.tsx` files where hardcoded hex values cause breakage.

**Tech Stack:** CSS custom properties, Google Fonts (Cormorant Garamond, Libre Franklin, Fira Code), CSS pseudo-elements for background texture, CSS transitions for micro-interactions.

**Spec:** `docs/superpowers/specs/2026-03-25-archive-redesign.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `frontend/index.html` | Modify | Replace Google Fonts import |
| `frontend/src/styles/variables.css` | Modify | Redefine all color/font/shadow tokens in-place, add semantic aliases |
| `frontend/src/styles/reset.css` | Modify | Body background gradient, texture pseudo-elements, selection, focus-visible |
| `frontend/src/styles/layout.css` | Modify | Header, login, welcome, table hover, tabs, viewer, grid, scrollbars |
| `frontend/src/styles/components.css` | Modify | Buttons, badges, inputs, cards, modals, toasts, floating bar, spinner, mark, ai-indicator |
| `frontend/src/App.tsx` | Modify | Remove hardcoded hex/color values in inline styles |
| `frontend/src/components/DocumentViewer.tsx` | Modify | Remove hardcoded hex/color values in inline styles |

---

### Task 1: Replace Google Fonts Import

**Files:**
- Modify: `frontend/index.html:8-10`

- [ ] **Step 1: Read the current font import line**

Open `frontend/index.html` and locate the Google Fonts `<link>` tag (line 10).

- [ ] **Step 2: Replace the font import**

Replace the existing `<link href="https://fonts.googleapis.com/css2?family=DM+Sans:...` line with:

```html
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@400;600;700&family=Libre+Franklin:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet" />
```

- [ ] **Step 3: Verify the page still loads**

Run: `npm run dev` (if not already running)
Open `http://localhost:5173` — the page should load. Fonts will look different even before CSS changes since the old fonts are gone.

- [ ] **Step 4: Commit**

```bash
git add frontend/index.html
git commit -m "style: replace fonts with Cormorant Garamond, Libre Franklin, Fira Code"
```

---

### Task 2: Redefine CSS Variables (Tokens)

**Files:**
- Modify: `frontend/src/styles/variables.css`

This is the highest-leverage change — redefining token values propagates to every component that uses them.

- [ ] **Step 1: Read current variables.css**

Read `frontend/src/styles/variables.css` to understand all existing tokens.

- [ ] **Step 2: Rewrite the `:root` block**

Replace the entire `:root` block in `variables.css` with:

```css
:root {
  /* ── Archive Theme: Semantic Aliases ── */
  --color-ink: #2c3e6b;
  --color-ink-light: #4a5f8a;
  --color-ink-faint: rgba(44, 62, 107, 0.1);
  --color-parchment-light: #f5f0e8;
  --color-parchment-mid: #ebe4d8;
  --color-parchment-warm: #f2ede5;
  --color-card: #ffffff;
  --color-card-hover: #faf7f2;
  --color-rule: #8b7355;
  --color-margin: rgba(183, 28, 28, 0.06);

  /* ── Brand (ink-blue) ── */
  --color-brand-50: rgba(44, 62, 107, 0.04);
  --color-brand-100: rgba(44, 62, 107, 0.08);
  --color-brand-200: rgba(44, 62, 107, 0.15);
  --color-brand-400: #4a5f8a;
  --color-brand-500: #2c3e6b;
  --color-brand-600: #243356;
  --color-brand-700: #1c2844;

  /* ── Primary (ink-blue scale) ── */
  --color-primary-50: #f0f2f7;
  --color-primary-100: #d9dfe9;
  --color-primary-200: #b3bed3;
  --color-primary-300: #8a9abc;
  --color-primary-400: #6478a3;
  --color-primary-500: #4a5f8a;
  --color-primary-600: #3a4e76;
  --color-primary-700: #2c3e6b;
  --color-primary-800: #243356;
  --color-primary-900: #1a2540;

  /* ── Neutral (warm parchment grays) ── */
  --color-neutral-0: #ffffff;
  --color-neutral-50: #f5f0e8;
  --color-neutral-100: #ebe4d8;
  --color-neutral-200: #ddd5c5;
  --color-neutral-300: #c5bba8;
  --color-neutral-400: #9a8e7a;
  --color-neutral-500: #6e6354;
  --color-neutral-600: #4a4234;
  --color-neutral-700: #2d2720;
  --color-neutral-800: #1a1610;
  --color-neutral-900: #0d0b08;

  /* ── Semantic ── */
  --color-success-50: rgba(46, 125, 50, 0.06);
  --color-success-100: rgba(46, 125, 50, 0.1);
  --color-success-500: #2e7d32;
  --color-success-600: #256428;
  --color-success-700: #1b5e20;

  --color-warning-50: rgba(230, 81, 0, 0.06);
  --color-warning-100: rgba(230, 81, 0, 0.1);
  --color-warning-500: #e65100;
  --color-warning-600: #bf4400;

  --color-danger-50: rgba(183, 28, 28, 0.06);
  --color-danger-100: rgba(183, 28, 28, 0.1);
  --color-danger-500: #c62828;
  --color-danger-600: #b71c1c;
  --color-danger-700: #8e0000;

  --color-purple-50: rgba(69, 39, 160, 0.06);
  --color-purple-500: #4527a0;
  --color-purple-600: #381e87;
  --color-purple-700: #2c166e;

  /* ── Typography ── */
  --font-sans: 'Libre Franklin', 'Segoe UI', system-ui, sans-serif;
  --font-serif: 'Cormorant Garamond', Garamond, 'Times New Roman', serif;
  --font-mono: 'Fira Code', ui-monospace, 'Cascadia Code', Consolas, monospace;

  --text-xs: 0.6875rem;
  --text-sm: 0.8125rem;
  --text-base: 0.875rem;
  --text-lg: 1rem;
  --text-xl: 1.125rem;
  --text-2xl: 1.375rem;
  --text-3xl: 1.75rem;

  --leading-tight: 1.25;
  --leading-normal: 1.5;
  --leading-relaxed: 1.65;

  --font-normal: 400;
  --font-medium: 500;
  --font-semibold: 600;
  --font-bold: 700;

  /* ── Spacing ── */
  --space-px: 1px;
  --space-0-5: 0.125rem;
  --space-1: 0.25rem;
  --space-1-5: 0.375rem;
  --space-2: 0.5rem;
  --space-3: 0.75rem;
  --space-4: 1rem;
  --space-5: 1.25rem;
  --space-6: 1.5rem;
  --space-8: 2rem;
  --space-10: 2.5rem;
  --space-12: 3rem;

  /* ── Border radius ── */
  --radius-sm: 3px;
  --radius-md: 6px;
  --radius-lg: 10px;
  --radius-xl: 14px;
  --radius-full: 9999px;

  /* ── Shadows (ink-blue tinted) ── */
  --shadow-xs: 0 1px 2px rgba(44, 62, 107, 0.04);
  --shadow-sm: 0 1px 3px rgba(44, 62, 107, 0.06), 0 1px 2px rgba(44, 62, 107, 0.03);
  --shadow-md: 0 4px 8px -1px rgba(44, 62, 107, 0.07), 0 2px 4px -2px rgba(44, 62, 107, 0.04);
  --shadow-lg: 0 12px 24px -4px rgba(44, 62, 107, 0.1), 0 4px 8px -4px rgba(44, 62, 107, 0.05);
  --shadow-xl: 0 24px 48px -8px rgba(44, 62, 107, 0.14), 0 8px 16px -6px rgba(44, 62, 107, 0.06);
  --shadow-inner: inset 0 2px 4px rgba(44, 62, 107, 0.04);
  --shadow-ring: 0 0 0 3px rgba(44, 62, 107, 0.12);

  /* ── Transitions ── */
  --transition-fast: 100ms ease;
  --transition-base: 180ms ease;
  --transition-slow: 300ms cubic-bezier(0.4, 0, 0.2, 1);

  /* ── Layout ── */
  --header-height: 52px;
  --nav-height: 44px;
  --sidebar-width: 400px;
}
```

- [ ] **Step 3: Verify page loads without errors**

Open browser devtools console — check for missing CSS variable warnings. The page should already look dramatically different with the new palette applied.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles/variables.css
git commit -m "style: redefine all CSS tokens for Archive palette"
```

---

### Task 3: Update Reset Styles (Background, Selection, Focus)

**Files:**
- Modify: `frontend/src/styles/reset.css`

- [ ] **Step 1: Read current reset.css**

Read `frontend/src/styles/reset.css`.

- [ ] **Step 2: Replace the entire file with Archive reset**

```css
*,
*::before,
*::after {
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

html {
  font-size: 16px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  text-rendering: optimizeLegibility;
}

body {
  font-family: var(--font-sans);
  font-size: var(--text-base);
  line-height: var(--leading-normal);
  color: var(--color-neutral-800);
  background: linear-gradient(175deg, var(--color-parchment-light) 0%, var(--color-parchment-mid) 50%, var(--color-parchment-warm) 100%);
  background-attachment: fixed;
  min-height: 100vh;
}

/* Ruled-line texture */
body::before {
  content: '';
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background: repeating-linear-gradient(
    0deg,
    transparent,
    transparent 23px,
    rgba(139, 115, 85, 0.035) 23px,
    rgba(139, 115, 85, 0.035) 24px
  );
}

/* Red margin line */
body::after {
  content: '';
  position: fixed;
  top: 0;
  bottom: 0;
  left: 72px;
  width: 1px;
  background: var(--color-margin);
  pointer-events: none;
  z-index: 0;
}

/* Ensure all content sits above the texture */
#root {
  position: relative;
  z-index: 1;
}

img, svg {
  display: block;
  max-width: 100%;
}

a {
  color: var(--color-ink);
  text-decoration: none;
}
a:hover {
  color: var(--color-ink-light);
  text-decoration: underline;
}

table {
  border-collapse: collapse;
  width: 100%;
}

:focus-visible {
  outline: 2px solid rgba(44, 62, 107, 0.5);
  outline-offset: 2px;
}

::selection {
  background: rgba(44, 62, 107, 0.12);
  color: var(--color-ink);
}

/* Webkit scrollbar styling */
::-webkit-scrollbar {
  width: 8px;
  height: 8px;
}
::-webkit-scrollbar-track {
  background: transparent;
}
::-webkit-scrollbar-thumb {
  background: rgba(44, 62, 107, 0.15);
  border-radius: 4px;
}
::-webkit-scrollbar-thumb:hover {
  background: rgba(44, 62, 107, 0.25);
}

/* Progress bar accent */
progress {
  accent-color: var(--color-ink);
}
```

- [ ] **Step 3: Verify the parchment background and ruled lines appear**

Open `http://localhost:5173`. You should see:
- Warm parchment gradient background
- Faint horizontal ruled lines across the viewport
- A subtle red vertical margin line near the left edge
- Content sitting above the texture layers

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles/reset.css
git commit -m "style: add parchment background, ruled lines, and margin texture"
```

---

### Task 4: Update Component Styles

**Files:**
- Modify: `frontend/src/styles/components.css`

- [ ] **Step 1: Read current components.css**

Read `frontend/src/styles/components.css`.

- [ ] **Step 2: Replace the entire file with Archive component styles**

```css
/* ══════════════════════════════════════════
   VIGILIST — Component Styles
   Archive Theme: Ink & Parchment
   ══════════════════════════════════════════ */

/* ── Buttons ── */

.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  padding: 7px var(--space-4);
  font-family: inherit;
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  line-height: var(--leading-tight);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;
  user-select: none;
  letter-spacing: 0.01em;
}
.btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
  pointer-events: none;
}

.btn-primary {
  background: var(--color-ink);
  color: var(--color-neutral-0);
  border-color: #243356;
}
.btn-primary:hover:not(:disabled) {
  background: #243356;
  box-shadow: var(--shadow-sm);
}
.btn-primary:active:not(:disabled) {
  background: #1a2540;
}

.btn-brand {
  background: var(--color-ink);
  color: var(--color-neutral-0);
  border-color: #243356;
}
.btn-brand:hover:not(:disabled) {
  background: #243356;
  box-shadow: var(--shadow-sm);
}

.btn-secondary {
  background: var(--color-card);
  color: rgba(44, 62, 107, 0.7);
  border-color: rgba(44, 62, 107, 0.2);
  box-shadow: var(--shadow-xs);
}
.btn-secondary:hover:not(:disabled) {
  background: var(--color-card-hover);
  border-color: rgba(44, 62, 107, 0.35);
  color: var(--color-ink);
  box-shadow: var(--shadow-sm);
}

.btn-ghost {
  background: transparent;
  color: rgba(44, 62, 107, 0.6);
}
.btn-ghost:hover:not(:disabled) {
  background: rgba(44, 62, 107, 0.04);
  color: var(--color-ink);
}

.btn-danger {
  background: var(--color-danger-600);
  color: #fff;
}
.btn-danger:hover:not(:disabled) {
  background: var(--color-danger-700);
}

.btn-sm {
  padding: 4px var(--space-3);
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
}

.btn-xs {
  padding: 2px var(--space-2);
  font-size: var(--text-xs);
  border-radius: var(--radius-sm);
}

.btn-icon {
  padding: var(--space-1);
  min-width: 28px;
  min-height: 28px;
}

/* ── Inputs ── */

.input {
  display: block;
  width: 100%;
  padding: 7px var(--space-3);
  font-family: inherit;
  font-size: var(--text-sm);
  color: var(--color-ink);
  background: var(--color-card);
  border: 1px solid rgba(44, 62, 107, 0.15);
  border-radius: var(--radius-md);
  box-shadow: var(--shadow-inner);
  transition: border-color var(--transition-fast), box-shadow var(--transition-fast);
}
.input:hover {
  border-color: rgba(44, 62, 107, 0.25);
}
.input:focus {
  outline: none;
  border-color: var(--color-ink);
  box-shadow: var(--shadow-ring);
}
.input::placeholder {
  color: rgba(44, 62, 107, 0.3);
}

.input-sm {
  padding: 4px var(--space-2);
  font-size: var(--text-xs);
}

textarea.input {
  resize: vertical;
  min-height: 60px;
}

/* ── Badges / Tags ── */

.badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 1px 8px;
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  border-radius: var(--radius-sm);
  line-height: 1.5;
  white-space: nowrap;
  letter-spacing: 0.02em;
}

.badge-green {
  background: rgba(46, 125, 50, 0.1);
  color: rgba(46, 125, 50, 0.8);
  border: 1px solid rgba(46, 125, 50, 0.15);
}
.badge-red {
  background: rgba(183, 28, 28, 0.08);
  color: rgba(183, 28, 28, 0.75);
  border: 1px solid rgba(183, 28, 28, 0.12);
}
.badge-yellow {
  background: rgba(230, 81, 0, 0.08);
  color: rgba(230, 81, 0, 0.8);
  border: 1px solid rgba(230, 81, 0, 0.12);
}
.badge-purple {
  background: rgba(69, 39, 160, 0.08);
  color: rgba(69, 39, 160, 0.75);
  border: 1px solid rgba(69, 39, 160, 0.12);
}
.badge-gray {
  background: rgba(44, 62, 107, 0.05);
  color: rgba(44, 62, 107, 0.6);
  border: 1px solid rgba(44, 62, 107, 0.1);
}
.badge-blue {
  background: rgba(44, 62, 107, 0.08);
  color: rgba(44, 62, 107, 0.75);
  border: 1px solid rgba(44, 62, 107, 0.12);
}

.badge-remove {
  cursor: pointer;
  margin-left: 2px;
  opacity: 0.5;
  font-size: 10px;
  transition: opacity var(--transition-fast);
}
.badge-remove:hover {
  opacity: 1;
}

/* ── Cards / Panels ── */

.card {
  background: var(--color-card);
  border: 1px solid rgba(44, 62, 107, 0.1);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.panel-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: rgba(44, 62, 107, 0.45);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  background: rgba(44, 62, 107, 0.02);
  border-bottom: 1px solid rgba(44, 62, 107, 0.08);
  user-select: none;
}

/* ── Tabs ── */

.tabs {
  display: flex;
  align-items: center;
  gap: 0;
  border-bottom: 1px solid rgba(44, 62, 107, 0.1);
  background: var(--color-card);
  padding: 0 var(--space-2);
}

.tab {
  position: relative;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  color: rgba(44, 62, 107, 0.45);
  background: none;
  border: none;
  cursor: pointer;
  transition: all var(--transition-fast);
  letter-spacing: 0.02em;
}
.tab:hover {
  color: rgba(44, 62, 107, 0.7);
}
.tab.active {
  color: var(--color-ink);
  font-weight: var(--font-semibold);
}
.tab.active::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: var(--space-2);
  right: var(--space-2);
  height: 2px;
  background: var(--color-ink);
  border-radius: 2px 2px 0 0;
  animation: tab-fade 150ms ease;
}

@keyframes tab-fade {
  from { opacity: 0; }
  to { opacity: 1; }
}

/* ── Spinner ── */

.spinner {
  display: inline-block;
  border: 2px solid rgba(44, 62, 107, 0.12);
  border-top-color: var(--color-ink);
  border-radius: 50%;
  animation: spin 0.7s linear infinite;
}
.spinner-sm { width: 14px; height: 14px; }
.spinner-md { width: 22px; height: 22px; }
.spinner-lg { width: 32px; height: 32px; }

@keyframes spin {
  to { transform: rotate(360deg); }
}

.loading-center {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-12);
  color: rgba(44, 62, 107, 0.5);
  font-size: var(--text-sm);
}

/* ── Highlight (search results) ── */

mark {
  background: rgba(44, 62, 107, 0.1);
  color: var(--color-ink);
  padding: 1px 3px;
  border-radius: 2px;
}

/* ── Keyboard shortcut hint ── */

.kbd {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  padding: 1px 5px;
  font-family: var(--font-mono);
  font-size: 10px;
  font-weight: var(--font-medium);
  color: rgba(44, 62, 107, 0.5);
  background: rgba(44, 62, 107, 0.04);
  border: 1px solid rgba(44, 62, 107, 0.1);
  border-radius: var(--radius-sm);
  box-shadow: 0 1px 0 rgba(44, 62, 107, 0.08);
}

/* ── Dropdown ── */

.dropdown {
  position: absolute;
  z-index: 50;
  background: var(--color-card);
  border: 1px solid rgba(44, 62, 107, 0.1);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xl);
  overflow: hidden;
  min-width: 200px;
  animation: dropdown-in 0.18s ease;
}

@keyframes dropdown-in {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.dropdown-header {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: rgba(44, 62, 107, 0.35);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  border-bottom: 1px solid rgba(44, 62, 107, 0.06);
}

.dropdown-item {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  color: rgba(44, 62, 107, 0.7);
  cursor: pointer;
  transition: background var(--transition-fast);
}
.dropdown-item:hover {
  background: rgba(44, 62, 107, 0.03);
}

/* ── Flash animation ── */

@keyframes flash-success {
  0% { background: rgba(46, 125, 50, 0.08); }
  100% { background: transparent; }
}
@keyframes flash-remove {
  0% { background: rgba(183, 28, 28, 0.06); }
  100% { background: transparent; }
}
.flash-success { animation: flash-success 0.6s ease; }
.flash-remove { animation: flash-remove 0.6s ease; }

/* ── Checkbox ── */

.checkbox-wrapper {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  flex-shrink: 0;
}

.checkbox-wrapper input[type="checkbox"] {
  width: 15px;
  height: 15px;
  cursor: pointer;
  accent-color: var(--color-ink);
  border-radius: var(--radius-sm);
}

/* ── Floating action bar ── */

.floating-bar {
  position: fixed;
  bottom: var(--space-6);
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-6);
  background: var(--color-ink);
  color: var(--color-neutral-0);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl), 0 0 0 1px rgba(255, 255, 255, 0.06);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  z-index: 100;
  animation: float-in 0.2s ease;
}

@keyframes float-in {
  from { opacity: 0; transform: translateX(-50%) translateY(12px); }
  to { opacity: 1; transform: translateX(-50%) translateY(0); }
}

.floating-bar .btn {
  border-color: rgba(255, 255, 255, 0.15);
  color: #fff;
}

/* ── Divider ── */

.divider {
  width: 1px;
  height: 18px;
  background: rgba(44, 62, 107, 0.15);
  flex-shrink: 0;
}

/* ── Viewer center tabs (Images / Video / Audio) ── */

.viewer-center-tabs {
  display: flex;
  align-items: center;
  gap: 0;
  padding: 0 var(--space-2);
  border-bottom: 1px solid rgba(44, 62, 107, 0.1);
  background: rgba(44, 62, 107, 0.02);
  flex-shrink: 0;
}

.viewer-center-tab {
  position: relative;
  padding: var(--space-2) var(--space-4);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  color: rgba(44, 62, 107, 0.45);
  background: none;
  border: none;
  cursor: pointer;
  transition: all var(--transition-fast);
  letter-spacing: 0.02em;
}
.viewer-center-tab:hover {
  color: rgba(44, 62, 107, 0.7);
}
.viewer-center-tab.active {
  color: var(--color-ink);
  font-weight: var(--font-semibold);
}
.viewer-center-tab.active::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: var(--space-2);
  right: var(--space-2);
  height: 2px;
  background: var(--color-ink);
  border-radius: 2px 2px 0 0;
}

/* ── Media player ── */

.media-player-container {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-4);
  background: var(--color-neutral-900);
}

.media-player-container video {
  border-radius: var(--radius-md);
  max-height: calc(100vh - 200px);
}

.media-player-audio {
  background: var(--color-neutral-100);
}

/* ── Modal ── */

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(44, 62, 107, 0.3);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 200;
  animation: fade-in 0.15s ease;
}

@keyframes fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

.modal-panel {
  background: var(--color-card);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl);
  width: 480px;
  max-height: 80vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  animation: modal-in 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

@keyframes modal-in {
  from { opacity: 0; transform: scale(0.96) translateY(8px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}

.modal-panel.modal-large {
  width: 800px;
  max-width: 90vw;
  max-height: 85vh;
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4);
  border-bottom: 1px solid rgba(44, 62, 107, 0.1);
}

/* ── Toast ── */

.toast-container {
  position: fixed;
  bottom: var(--space-6);
  right: var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  z-index: 300;
}

.toast {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--font-medium);
  box-shadow: var(--shadow-lg);
  animation: toast-in 0.2s ease;
  max-width: 360px;
}

@keyframes toast-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.toast-success {
  background: rgba(46, 125, 50, 0.08);
  color: var(--color-success-700);
  border: 1px solid rgba(46, 125, 50, 0.15);
}

.toast-error {
  background: rgba(183, 28, 28, 0.08);
  color: var(--color-danger-700);
  border: 1px solid rgba(183, 28, 28, 0.12);
}

.toast-info {
  background: rgba(44, 62, 107, 0.06);
  color: var(--color-ink);
  border: 1px solid rgba(44, 62, 107, 0.12);
}

/* ── AI indicator ── */

.ai-indicator {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: rgba(230, 81, 0, 0.8);
  background: rgba(230, 81, 0, 0.08);
  border: 1px solid rgba(230, 81, 0, 0.15);
  border-radius: var(--radius-sm);
  letter-spacing: 0.04em;
}
```

- [ ] **Step 3: Verify all component styles look correct**

Check in browser:
- Buttons render with ink-blue primary, white secondary
- Badges use muted ink-on-paper colors
- Cards are solid white with subtle ink-blue borders
- Modals have ink-tinted overlay and scale-in animation
- Spinner uses ink-blue
- AI indicator is orange-tinted (distinct from ink-blue)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/styles/components.css
git commit -m "style: rewrite component styles for Archive theme"
```

---

### Task 5: Update Layout Styles

**Files:**
- Modify: `frontend/src/styles/layout.css`

- [ ] **Step 1: Read current layout.css**

Read `frontend/src/styles/layout.css`.

- [ ] **Step 2: Replace the header section**

Replace the `.app-header` block (lines 7-60) with:

```css
.app-header {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  height: var(--header-height);
  padding: 0 var(--space-5);
  background: rgba(44, 62, 107, 0.03);
  color: var(--color-ink);
  flex-shrink: 0;
  border-bottom: 2px solid var(--color-ink);
}

.app-header .logo {
  font-family: var(--font-serif);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  letter-spacing: -0.01em;
  cursor: pointer;
  color: var(--color-ink);
  transition: opacity var(--transition-fast);
}
.app-header .logo:hover {
  opacity: 0.7;
}

.app-header .logo-accent {
  color: var(--color-ink-light);
}

.app-header .user-menu {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-xs);
  color: rgba(44, 62, 107, 0.5);
}

.app-header .btn-header {
  padding: 3px var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-medium);
  background: transparent;
  border: 1px solid rgba(44, 62, 107, 0.2);
  color: rgba(44, 62, 107, 0.6);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: all var(--transition-fast);
  letter-spacing: 0.01em;
}
.app-header .btn-header:hover {
  background: rgba(44, 62, 107, 0.04);
  border-color: rgba(44, 62, 107, 0.4);
  color: var(--color-ink);
}
```

- [ ] **Step 3: Update the login page styles**

Replace the `.login-page` and `.login-card` blocks with:

```css
.login-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  background: var(--color-ink);
  background-image:
    radial-gradient(ellipse 80% 60% at 50% 0%, #3a5080, transparent),
    radial-gradient(ellipse 60% 40% at 70% 100%, rgba(139, 115, 85, 0.08), transparent);
}

.login-card {
  width: 380px;
  padding: var(--space-10);
  background: var(--color-card);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-xl), 0 0 0 1px rgba(0, 0, 0, 0.04);
}

.login-card h1 {
  font-family: var(--font-serif);
  font-size: var(--text-3xl);
  font-weight: var(--font-bold);
  color: var(--color-ink);
  margin-bottom: 2px;
  letter-spacing: -0.02em;
}

.login-card .subtitle {
  font-size: var(--text-sm);
  color: rgba(44, 62, 107, 0.4);
  margin-bottom: var(--space-8);
  letter-spacing: 0.02em;
}

.login-card .form-group {
  margin-bottom: var(--space-4);
}

.login-card label {
  display: block;
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: rgba(44, 62, 107, 0.5);
  margin-bottom: var(--space-1-5);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.login-card .error {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  color: var(--color-danger-700);
  background: rgba(183, 28, 28, 0.06);
  border: 1px solid rgba(183, 28, 28, 0.12);
  border-radius: var(--radius-md);
  margin-bottom: var(--space-4);
}
```

- [ ] **Step 4: Update the welcome page styles**

Replace the `.welcome-page` through `.welcome-hint` blocks with:

```css
.welcome-page {
  min-height: 100vh;
  background: var(--color-ink);
  background-image:
    radial-gradient(ellipse 80% 50% at 50% 0%, #3a5080, transparent),
    radial-gradient(ellipse 50% 30% at 70% 100%, rgba(139, 115, 85, 0.08), transparent);
  color: var(--color-neutral-0);
  display: flex;
  flex-direction: column;
}

.welcome-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-6);
}

.welcome-logo {
  font-family: var(--font-serif);
  font-size: var(--text-xl);
  font-weight: var(--font-bold);
  letter-spacing: -0.01em;
}

.welcome-user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-sm);
  color: rgba(255, 255, 255, 0.6);
}

.welcome-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: var(--space-8) var(--space-6);
  text-align: center;
  max-width: 800px;
  margin: 0 auto;
}

.welcome-title {
  font-family: var(--font-serif);
  font-size: 2.5rem;
  font-weight: var(--font-bold);
  letter-spacing: -0.02em;
  margin-bottom: var(--space-3);
}

.welcome-subtitle {
  font-size: var(--text-lg);
  color: rgba(255, 255, 255, 0.6);
  margin-bottom: var(--space-10);
  line-height: var(--leading-relaxed);
}

.welcome-features {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-6);
  margin-bottom: var(--space-10);
  width: 100%;
}

.welcome-feature {
  padding: var(--space-5);
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: var(--radius-lg);
  text-align: center;
}

.welcome-feature-icon {
  font-size: 1.75rem;
  margin-bottom: var(--space-3);
}

.welcome-feature h3 {
  font-family: var(--font-serif);
  font-size: var(--text-base);
  font-weight: var(--font-semibold);
  margin-bottom: var(--space-2);
}

.welcome-feature p {
  font-size: var(--text-sm);
  color: rgba(255, 255, 255, 0.55);
  line-height: var(--leading-relaxed);
}

.welcome-actions {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
}

.welcome-hint {
  font-size: var(--text-sm);
  color: rgba(255, 255, 255, 0.4);
  font-style: italic;
}
```

- [ ] **Step 5: Update table styles with hover micro-interaction**

Replace the `.doc-table` section with:

```css
.doc-table {
  width: 100%;
}

.doc-table th {
  position: sticky;
  top: 0;
  z-index: 10;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--font-semibold);
  color: rgba(44, 62, 107, 0.45);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  text-align: left;
  background: rgba(44, 62, 107, 0.02);
  border-bottom: 2px solid rgba(44, 62, 107, 0.08);
}

.doc-table td {
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-sm);
  border-bottom: 1px solid rgba(44, 62, 107, 0.05);
  vertical-align: middle;
}

.doc-table tr {
  cursor: pointer;
}
.doc-table tbody tr {
  border-left: 3px solid transparent;
  transition: background var(--transition-fast), border-color 150ms ease;
}
.doc-table tbody tr:hover {
  background: var(--color-card-hover);
  border-left-color: var(--color-ink);
}
.doc-table tbody tr:active {
  background: rgba(44, 62, 107, 0.06);
}

.doc-table .bates-cell {
  font-family: var(--font-mono);
  font-weight: var(--font-medium);
  font-size: var(--text-xs);
  color: var(--color-ink);
  white-space: nowrap;
  letter-spacing: 0.02em;
}

.doc-table .meta-cell {
  color: rgba(44, 62, 107, 0.45);
  font-size: var(--text-xs);
}

.doc-table .tags-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
}
```

- [ ] **Step 6: Verify remaining layout sections need no changes**

The remaining layout sections (doc-nav, viewer-layout, viewer-sidebar, sidebar-section, image-toolbar, search-toolbar, filter-bar, result-item, pagination, tag-bar, note-item, empty-state, content-area, section-header, doc-grid, saved-searches, auth-divider, auth-toggle, view-toggle, production-grid) use **only CSS custom property references** (`var(--color-neutral-*)`, `var(--color-primary-*)`, `var(--color-brand-*)`). Since Task 2 redefined these tokens in-place, all these sections theme automatically with zero edits.

Confirm by reading the remaining sections of `layout.css` (after the welcome page block) and verifying there are no hardcoded hex or rgba values that conflict with the Archive theme. The only hardcoded values in layout.css are in the header, login, and welcome sections already replaced in Steps 2-4.

- [ ] **Step 7: Verify layout looks correct**

Check in browser:
- Header: faint ink wash background, strong bottom border, ink-blue text
- Login page: deep ink-blue background, white card
- Welcome page: deep ink-blue, white text, translucent feature cards
- Document table: row hover shows left ink-blue border slide-in
- Tabs: ink-blue active indicator
- Viewer layout: warm tones, sidebar borders correct

- [ ] **Step 8: Commit**

```bash
git add frontend/src/styles/layout.css
git commit -m "style: update layout styles for Archive theme with micro-interactions"
```

---

### Task 6: Fix Inline Style Conflicts in React Components

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/DocumentViewer.tsx`

Most inline styles reference CSS custom properties that were redefined in Task 2, so they theme automatically. This task only fixes **hardcoded values** that conflict.

- [ ] **Step 1: Search for hardcoded color values in App.tsx**

Read `frontend/src/App.tsx` and look for inline style properties with hardcoded hex colors or `rgba()` values (not CSS variable references).

Key lines to fix in `App.tsx`:
- Line 173: `background: 'var(--color-neutral-50)'` — this now resolves to parchment, which is correct. No change needed.
- Line 530: The reprocess progress panel uses `background: 'var(--color-neutral-900)'` — resolves to dark brown-black. Acceptable for a dark panel. No change needed.

- [ ] **Step 2: Check DocumentViewer.tsx for hardcoded colors**

Read `frontend/src/components/DocumentViewer.tsx`. Look for hardcoded hex colors. The left tab bar (lines 230-245) uses inline styles for the tab buttons — these reference CSS variables, so they theme automatically.

- [ ] **Step 3: Spot-check other heavy-inline components**

Quickly scan `Dashboard.tsx`, `BatchReview.tsx`, and `QueueManager.tsx` for hardcoded hex values that would clash with the Archive theme. These should mostly use CSS variables that are now redefined.

If any hardcoded `#fff`, `#666`, `rgba(0,0,0,...)` values appear in visible UI elements, update them to use the appropriate Archive-themed CSS variables.

- [ ] **Step 4: Commit if any changes were made**

```bash
git add -u frontend/src/
git commit -m "style: fix inline style conflicts for Archive theme"
```

---

### Task 7: Visual QA Pass

**Files:** None (read-only verification)

- [ ] **Step 1: Check the login page**

Navigate to `http://localhost:5173` (logged out). Verify:
- Deep ink-blue background with subtle gradient
- White login card with Cormorant Garamond "Vigilist" heading
- Ink-blue form labels, inputs with proper focus ring
- Google sign-in button styled correctly

- [ ] **Step 2: Check the welcome/production picker pages**

Log in and verify:
- Welcome page (if no productions): ink-blue background, white text, feature cards
- Production picker (if multiple productions): parchment background, white cards

- [ ] **Step 3: Check the main document list**

- Parchment background with faint ruled lines visible
- Red margin line on left edge
- Header: faint ink wash, 2px bottom border, Cormorant Garamond logo
- Document table: white card, ink-blue headers, row hover shows left border slide
- Badges: muted ink-on-paper colors
- Search bar: ink-blue focus ring

- [ ] **Step 4: Check the document viewer**

Open a document:
- Three-column layout: sidebars white, center area slightly tinted
- Tabs: ink-blue active indicator with crossfade
- Tags, notes panels: themed correctly
- AI indicator: orange-tinted (distinct from ink-blue)

- [ ] **Step 5: Check modals**

Open Dashboard, Queue Manager, Ingest Wizard:
- Ink-tinted overlay (not black)
- White panel with scale-in animation
- Content themed correctly via CSS variable inheritance

- [ ] **Step 6: Fix any visual issues found**

If anything looks wrong, trace it to the relevant CSS file or inline style and fix it. Then commit:

```bash
git add -u frontend/
git commit -m "style: fix visual QA issues from Archive redesign"
```

---

### Task 8: Final Commit & Cleanup

- [ ] **Step 1: Run the TypeScript type checker**

```bash
cd frontend && npx tsc --noEmit
```

Expected: No errors (CSS changes don't affect types, but verify no `.tsx` edits broke anything).

- [ ] **Step 2: Verify no console errors**

Open browser devtools console on every major page. No errors should appear.

- [ ] **Step 3: Commit any remaining fixes**

If the type checker or console revealed issues:

```bash
git add -u frontend/
git commit -m "style: final Archive redesign cleanup"
```
