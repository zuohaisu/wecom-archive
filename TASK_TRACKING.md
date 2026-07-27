# RND-198 Task Tracking

## Phase 0 — Registry Foundation
- [ ] Add `system_card` to `RendererStrategy` enum
- [ ] Register 7 new types (vote, collect, meeting, schedule, redpacket, switch_corp)
- [ ] Update `todo`: UNSUPPORTED→SUPPORTED, STRUCTURED_CARD
- [ ] Update `sys`: UNSUPPORTED→SUPPORTED, `system_card`
- [ ] Run `make typecheck` — confirm no errors

## Phase 1 — Parser Implementation
- [ ] Add 7 parser functions to structured_message_parser.py
- [ ] Register all 7 in `_STRUCTURED_FIELD_PARSERS`
- [ ] Extend `parse_structured_content()` for sys (CONTROL_SIGNAL)
- [ ] Run parser tests

## Phase 2 — Backend API Integration
- [ ] Update `classify_media()` for system event media_type
- [ ] Add `action` field to TimelineMessageOut
- [ ] Populate `action` in message assembly loop

## Phase 3 — Frontend Rendering
- [ ] Add 7 business card renderers to main.py JS
- [ ] Add renderSystemCard() with subtype dispatch
- [ ] Register in STRUCTURED_CARD_RENDERERS
- [ ] Extend renderMessageBody() for system_card

## Phase 4 — I18n
- [ ] Add messageType keys for new types (3 locales)
- [ ] Add system event subtype labels
- [ ] Add card-specific display keys
- [ ] Add placeholder keys

## Phase 5 — Mock Fixtures
- [ ] Add fixtures to mock_ingest.py

## Phase 6 — Tests
- [ ] Parser tests (valid, missing, malformed, never-raises)
- [ ] Registry tests (new types, status changes)
- [ ] Classification tests
- [ ] API serialization tests
- [ ] Frontend rendering tests

## Phase 7 — Verification
- [ ] `make lint-diff`
- [ ] `make typecheck`
- [ ] `make build`
- [ ] `make test`
- [ ] `make verify`