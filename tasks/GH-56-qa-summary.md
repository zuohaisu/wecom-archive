# GH-56 QA Summary — 对话审阅会话列表头像

## Files changed
- `backend/app/schemas/listing.py`: adds controlled avatar fields to conversation summaries.
- `backend/app/services/listing_service.py`: attaches tenant-scoped counterpart avatars to direct conversations; group conversations retain no image source.
- `backend/app/web/static/console/conversation-list.js`: renders controlled direct avatars and a generic group glyph.
- `backend/app/web/templates/review_console.html`: adds conversation-card avatar layout and styling.
- `backend/tests/test_listing_service.py`: covers direct controlled-avatar and group fallback API data.
- `backend/tests/test_archive_console_v2.py`: executes the real card renderer for direct and group avatar output.

## Acceptance criteria
- [x] Direct conversation cards display the cached, tenant-scoped counterpart avatar, with the existing initial fallback on an unavailable image.
- [x] Group conversation cards display a non-identifying group glyph without inventing or exposing an upstream image URL.
- [x] A changed avatar response re-renders the conversation list.

## Commands run
- `node --check backend/app/web/static/console/conversation-list.js` → pass
- `python3 -m compileall -q backend/app backend/tests` → pass
- `uv run --isolated --python 3.11 --with 'ruff==0.15.21' ruff check backend/app/schemas/listing.py backend/app/services/listing_service.py backend/tests/test_listing_service.py backend/tests/test_archive_console_v2.py` → pass
- `cd backend && uv run --isolated --python 3.11 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q` → 3386 passed, 158 skipped

## Manual verification
- The Node regression renders a direct controlled-avatar endpoint and the inline SVG group glyph from the production `renderConvList()` code.

## Risks or gaps
- The documented WeCom group-chat metadata endpoint supplies a group name but no group image. The generic glyph is therefore intentional; it avoids fabricating or leaking an external image source.

No secrets introduced: confirmed
Only intentional files changed: confirmed
