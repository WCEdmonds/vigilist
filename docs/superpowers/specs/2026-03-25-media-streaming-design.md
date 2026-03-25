# Media Streaming Design

**Date:** 2026-03-25
**Status:** Approved

## Problem

Documents with native video (MP4, MOV) and audio (WAV) files currently only offer a download link. The requirements spec (Section 4.3) calls for in-browser streaming playback with standard controls. The existing `get_native` endpoint uses `FileResponse` which doesn't support HTTP range requests, so seeking and progressive playback don't work for large media files.

## Decision

**Approach A: Custom streaming endpoint with range requests.** A new `/api/documents/{id}/stream` endpoint handles the `Range` header and returns HTTP 206 partial content. The frontend adds a `<video>`/`<audio>` element and a tab bar to switch between image and native views.

Alternatives considered:
- **StaticFiles mount** — bypasses auth, exposes paths
- **Pre-signed URLs** — overkill for local dev

## Backend

### New endpoint: `GET /api/documents/{doc_id}/stream`

Located in `backend/app/routers/documents.py`.

- Resolves document's `native_path` to an absolute file path
- Detects media type from extension:
  - `.mp4` → `video/mp4`
  - `.mov` → `video/quicktime`
  - `.wav` → `audio/wav`
- If `Range` header present: parse byte range, return HTTP 206 with:
  - `Content-Range: bytes {start}-{end}/{total}`
  - `Accept-Ranges: bytes`
  - `Content-Length: {chunk_size}`
  - 1MB chunk size for partial responses
- If no `Range` header: return full file with HTTP 200 and `Accept-Ranges: bytes`
- Protected by `get_current_user` dependency

### Update `get_native` endpoint

Add MP4/MOV/WAV to the media type map for consistency.

## Frontend

### New component: `MediaPlayer.tsx`

- Props: `docId: string`, `mediaType: 'video' | 'audio'`
- Video: `<video>` element with `controls`, `width: 100%`, centered in panel
- Audio: `<audio>` element with `controls`, centered in panel
- Source URL: `/api/documents/${docId}/stream`

### Update `DocumentViewer.tsx`

- Detect streamable native by checking `native_path` extension against mp4/mov/wav
- If streamable + has images: render tab bar ("Images" | "Native") above center panel
- If streamable + no images: render MediaPlayer directly
- If not streamable: current behavior unchanged

### Update `client.ts`

- Add `streamUrl(docId: string): string` helper

### Styles

Minimal additions to `components.css` for the viewer tab bar and media player container.

## Files touched

1. `backend/app/routers/documents.py` — new `/stream` endpoint, update media type map
2. `frontend/src/components/MediaPlayer.tsx` — new file
3. `frontend/src/components/DocumentViewer.tsx` — tab bar logic
4. `frontend/src/api/client.ts` — stream URL helper
5. `frontend/src/styles/components.css` — tab bar and media player styles
