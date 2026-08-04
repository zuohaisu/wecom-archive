# RND-343 Independent QA Handoff — Event-first archive and generic media scheduling

## Role and boundaries

Act as an **independent, read-only QA reviewer**. Do not edit, commit, push,
restart services, source a production `.env`, access production data, or run
any deployment command. Review the current worktree only.

## Scope under review

RND-343 changes the production scheduling model to:

```text
verified WeCom callback
  -> non-blocking shared Archive Worker
  -> sync + decrypt + normalization commit
  -> bounded archive-complete wake-up of existing generic Media Worker only when fresh/pending media exists

Archive reconciliation timer: :00/:30
Media reconciliation timer:   :15/:45
```

Key intended seams:

- `backend/app/routers/wecom_events.py`: callback dispatches Archive Worker
  only; it must not parse/download media.
- `backend/scripts/run_archive_worker_once.py`: shared archive entrypoint;
  only after successful sync/decrypt commits it requests media dispatch.
- `backend/app/media_event_dispatch.py`: read-only generic pending-media
  preflight then one coalescing systemd-path signal; the event service invokes
  the existing `scripts/download_wecom_media_once.py`; no SDK/media download
  logic here.
- `backend/scripts/download_wecom_media_once.py`: sole generic Media Worker;
  timer runs it with `--retry`; durable attempts/backoff apply across generic
  media types.
- Existing locks stay separate: `WORKER_LOCK_PATH` vs
  `MEDIA_DOWNLOAD_LOCK_PATH`.

## Required review checklist

### Callback / archive scheduling

- [ ] Valid callback returns without waiting for full Archive Worker.
- [ ] Callback router contains no direct media dispatch/download path.
- [ ] Callback, manual API, timer, and direct CLI reuse
  `run_archive_worker_once.py` and its existing archive lock.
- [ ] Archive-complete media request occurs only after sync/decrypt success;
  sync/decrypt failure and archive lock-held path never request media.
- [ ] Archive timer default is `OnCalendar=*:0/30`, persistent, and documented
  with a safe versioned 5-minute rollback override.

### Generic media scheduling

- [ ] Event preflight covers `GENERIC_DOWNLOAD_MSGTYPES` and nested media;
  image/voice/video/file/emotion must not use a separate image implementation.
- [ ] No media produces `no-work` and no event signal/media service start.
- [ ] The systemd event service targets the existing
  `scripts/download_wecom_media_once.py`, not a new SDK/storage/downloader
  path.
- [ ] Media timer default is `OnCalendar=*:15/30`, invokes `--retry`, and is
  staggered at least 10 minutes from archive timer.
- [ ] Media lock-held behavior is safe no-op; no unbounded in-process queue,
  thread, or task accumulation is reintroduced.
- [ ] Retry attempts are persisted before download; failed rows obey cap and
  backoff while new pending media remains processable.

### Security / observability / deployment

- [ ] Aggregate logs contain safe trigger/outcome/count/duration data without
  tokens, secrets, callback query, messages, media identifiers, signed URLs,
  storage paths, raw provider settings, or exception text. In particular, an
  invalid provider value shaped like a signed URL must not appear in stdout or
  stderr.
- [ ] Every successful, skipped, and failed Archive/Media Worker path emits a
  final `lifecycle=ended` line with `result`, `completed_at`, duration,
  CPU/RSS aggregates, and a safe error classification.
- [ ] No new route or architecture-boundary violation.
- [ ] `deploy/systemd/` templates and runbooks document default cadence,
  backup-first Ops workflow, verification, and rollback; no production change
  was executed by the implementation agent.

## Commands to run

From repository root:

```bash
make verify
cd backend
../.venv/bin/python -m pytest \
  tests/test_rnd343_worker_scheduling.py \
  tests/test_archive_worker_trigger.py \
  tests/test_wecom_events.py \
  tests/test_sync_api.py \
  tests/test_rnd_172_event_media_download.py \
  tests/test_download_wecom_media_once.py \
  tests/test_download_wecom_media_once_cli.py \
  tests/test_media_worker_service.py \
  tests/test_architecture_boundary.py -q
```

If `systemd-analyze` is available, validate the archive worker/timer, media
event service/path, and media reconciliation service/timer. Do not run it
against a production host.

## Report format

```text
RND-343 QA verdict: PASS / FAIL / BLOCKED
Files reviewed:
Acceptance checks: <pass/fail evidence>
Commands: <result>
Security / architecture: <result>
Deployment evidence gap: <none or explicit>
Findings: <ordered by severity>
No commits/pushes/production actions: confirmed
```
