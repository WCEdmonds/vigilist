# Vigilist marketing site

Standalone single-page promotional site. No build step.

## Run locally
Open `index.html` directly, or serve the folder:
`python -m http.server 8080` then visit http://localhost:8080

## Deploy — Cloudflare Pages (production: vigilist.co)
This folder is the Pages **build output**. Connect the GitHub repo to a Pages
project once, then every push to `main` auto-deploys.

Cloudflare Pages project settings:
- **Production branch:** `main`
- **Build command:** _(leave empty — no build step)_
- **Build output directory:** `marketing`
- **Root directory:** `/`

Custom domains (added in the Pages project → Custom domains):
- `vigilist.co` (apex) and `www.vigilist.co` — Cloudflare provisions DNS + TLS
  automatically. `_redirects` forces `www` → apex.

`_headers` and `_redirects` are read by Pages from this output directory.

Any static host also works (Netlify, GCS bucket, S3): just serve these files.

## Editing
- Copy lives inline in `index.html`.
- Brand tokens are the `:root` custom properties at the top of `styles.css`.
- All "Request a demo" CTAs link to `https://intake.qndary.com`; the footer
  attribution links to `https://qndary.com` ("A QNDARY Project").

## Reveal animations
Cards/steps carry `.reveal` and are visible by default. `main.js` adds
`.reveal-anim` to `<html>` only when it can drive the fade-in (IntersectionObserver
present, motion not reduced), so disabled/cached/failed JS can never hide content.
