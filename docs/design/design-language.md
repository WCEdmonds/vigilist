# The Record — Vigilist design language

One design language across every Vigilist surface: the marketing site, the
product, generated artifacts (reports, logs, exports), and email. It is built
from the artifacts of litigation itself — paper, ink, redaction, highlighter,
stamp, transcript — so the brand gets more literal, not less, the closer it
gets to the courtroom.

Origin: the marketing redesign (marketing/styles.css, July 2026). This
document distills that page into portable rules so the same language can be
applied anywhere without copying the page.

## Principles

1. **Surfaces are paper; ink is near-black; true black means redaction.**
   Black is never decoration — it is the one color with domain meaning.
2. **One interactive color, one emphasis color.** Stamp blue is links,
   focus, primary affordances. Marker yellow means *"someone — human or
   AI — marked this"*: search hits, key excerpts, active selection. Yellow
   is **background-only, never text**, and never decoration. If yellow
   stops meaning something, the metaphor is dead.
3. **Data wears mono.** Bates numbers, hashes, statistics, timestamps,
   file sizes. Display serif is rationed to page titles, empty states, and
   report covers. Everything else is the workhorse sans.
4. **Structure comes from litigation artifacts.** Bates chips label
   sections and documents; redaction bars divide and stand in for loading
   content; stamps carry status; transcript numbering carries history
   (audit, custody); the validation readout carries statistics.
5. **In the product, motion is evidence, not theater.** The marketing page
   may perform (scan lines, sweeps). The tool animates only state changes,
   fast and quiet. Reduced-motion always lands on the final frame.

## Tokens

| Token | Value | Role |
|---|---|---|
| `paper` | `#fbfbf8` | canvas |
| `paper-dim` | `#f1f0ea` | chrome, wells, dividers |
| `ink` | `#14181d` | text |
| `ink-2` | `#4a545e` | secondary text |
| `redact` | `#0b0d0f` | true black: redaction, dark bands, max emphasis |
| `marker` | `#ffe24a` | emphasis background (never text) |
| `marker-deep` | `#f5ce00` | marker borders/edges |
| `stamp` | `#2f3dbd` | interactive: links, focus, primary accents |
| `stamp-soft` | `rgba(47,61,189,.12)` | focus rings, selected wash |

Semantic colors (success green `#2e7d32`, warning orange `#e65100`, danger
red `#b71c1c`) predate the record language and stay. Danger doubles as the
privilege/destructive color.

Type: **Besley** (display, 600–800, rationed) · **Public Sans** (body/UI) ·
**IBM Plex Mono** (data). Radii stay small (2–6px): paper is cut square.
Shadows are ink-tinted, not blue-tinted.

Contrast rules: marker yellow fails as text on white — background only,
with ink text on top. Text-toned "gold" accents use the darker `#9a7b00`.
Stamp blue on paper passes for text and small UI.

## Surface map

| Surface | Volume | Notes |
|---|---|---|
| Marketing site | Full drama | Source of the language. Animations allowed. |
| Product (app.vigilist.co) | Same DNA, quiet | Tokens + motifs; density and ergonomics first. Grids stay white; canvas is paper. |
| Generated artifacts | High formality | Validation reports, custody reports, privilege logs, production manifests. These get filed with courts — serif title block, mono data, stamp marks. The deepest brand surface. |
| Auth / emails / docs | Light touch | Bates chip + type + palette. |

## Motifs → product components

- **`.bates-chip`** — mono, stamp-blue bordered label. Section eyebrows,
  document IDs, panel headers.
- **`.stamp-badge`** — status as a rubber stamp: PRODUCED, PRIVILEGED,
  QC APPROVED. Straight in tables; may rotate on empty states and report
  covers only.
- **Marker highlight** — search hits, AI key excerpts, active queue item.
  This is where the marketing metaphor becomes the literal tool.
- **`.skel-redact`** — skeleton loaders drawn as redacted lines.
- **Transcript numbering** — audit log and chain-of-custody rows.
- **Validation readout** — one mono line: `RECALL 87.4% [CI 81.2–92.1] ·
  ELUSION 0.4%`. Sampling/TAR panels, report footers.
- **Dark band** — the entity graph lives on `redact` black with white node
  cards and marker focus edges (see marketing §000004).

## What does NOT carry over from the site

Display-size Besley in chrome; scan-line/sweep animations; 104px section
rhythm; wide-screen staggering; yellow as decoration. The site is a poster;
the app is a desk.

## Rollout

1. **Tokens + fonts** (this branch): `frontend/src/styles/variables.css`
   values remapped in place — token *names* unchanged (`parchment-*` now
   holds paper values; rename later, separately, to keep the diff legible).
2. **Pilot surfaces** (this branch): auth page + Defensibility panel carry
   the first motifs (`.bates-chip`, marker-edged card).
3. **Chrome pass**: AppHeader, panels, buttons, tables.
4. **Motif components**: BatesChip / StampBadge / Readout as React
   components; audit + custody transcript treatment; entity graph dark band.
5. **Artifacts**: restyle generated PDFs/reports (backend render services).

Marketing (`marketing/styles.css`) and app (`variables.css`) each hold a
copy of the token values; this file is the source of truth when they drift.

Caution for step 3+: thirulaw is live in the app mid-matter. Ship the visual
change as one coherent release with a heads-up, not a drip.
