## QA Summary

Files changed:
- `.env.example`: documents the bounded daily external-contact tenant limit.
- `backend/app/routers/sync.py`: dispatches manual archive sync with the authenticated tenant selector.
- `backend/app/services/{tenant_credentials,external_contact_sync,external_contact_refresh_worker}.py`: discovers active tenant authority, fails closed for missing/unreadable config, and isolates external-contact runs/refresh queues.
- `backend/scripts/{download_wecom_media_once,refresh_external_contacts_once,run_reachability_automation_once}.py`: removes global CorpID/archive-secret selection from normal media, refresh, and reachability execution.
- `deploy/systemd/wecom-archive-worker.service`: versioned archive timer command unsets the legacy selector pair.
- `docs/**`: records the repository-managed transition and tenant-scoped runtime behavior.
- `backend/tests/**`: adds GH-108 isolation coverage and changes pre-existing media/Qiniu fixtures from the obsolete legacy selector setup to explicit tenant credentials; no assertion was loosened.
- `tasks/GH-108-legacy-credential-consumer-inventory.md`: complete repository evidence inventory.

Acceptance criteria:
- [x] Repository-wide legacy credential consumer inventory: pass.
- [x] Media normal execution selects each active tenant's encrypted archive credentials, not ambient legacy pair: pass.
- [x] Media tenant isolation, missing config, unreadable config, retry/quota/storage regression coverage: pass.
- [x] External-contact daily and durable-refresh selection is tenant-scoped; tenant failures are isolated: pass.
- [x] Daily external-contact traversal is bounded and daily-rotated; 04:15 schedule is unchanged: pass.
- [x] Authenticated manual archive trigger uses explicit tenant path: pass.
- [x] Archive explicit/all-active behavior remains covered by existing RND-387 tests; timer unit now repository-manages selector removal: pass.
- [x] Production deployment and observation: n/a — Ops-owned.

Commands run:
- `.venv/bin/python -m pytest backend/tests/test_download_wecom_media_once.py backend/tests/test_download_wecom_media_once_cli.py backend/tests/test_external_contact_refresh_worker.py backend/tests/test_gh108_tenant_scoped_workloads.py backend/tests/test_rnd387_multi_tenant_worker.py backend/tests/test_rnd343_worker_scheduling.py -q`: 106 passed.
- `.venv/bin/python -m pytest backend/tests/test_gh108_tenant_scoped_workloads.py backend/tests/test_external_contact_refresh_worker.py backend/tests/test_sync_api.py backend/tests/test_reachability_automation_cli.py backend/tests/test_reachability_checks.py backend/tests/test_download_wecom_media_once.py backend/tests/test_download_wecom_media_once_cli.py -q`: 134 passed, 1 skipped.
- `.venv/bin/python -m pytest backend/tests/test_qiniu_worker_integration.py backend/tests/test_rnd402_service_access.py backend/tests/test_download_wecom_media_once.py backend/tests/test_external_contact_refresh_worker.py -q`: 113 passed.
- `make BACKEND_PY="$PWD/.venv311/bin/python" verify`: pass — 3470 passed, 158 skipped, 86 warnings.
- `git diff --check`: pass.
- `git status --short`: only intentional tracked edits/new GH-108 test; task artifacts are intentionally ignored by repository policy and will be staged explicitly with `git add -f`.
- `systemd-analyze verify`: not run; neither `systemd-analyze` nor Docker is installed in this development environment.

Manual verification:
- Searched migrated normal entrypoints (`download_wecom_media_once.py`, `refresh_external_contacts_once.py`, `external_contact_sync.py`, `run_reachability_automation_once.py`, `routers/sync.py`) for direct `WECOM_CORP_ID` / `WECOM_ARCHIVE_SECRET` reads: none.
- Confirmed direct pair reads remain only in the #93-owned archive compatibility path and historical tools listed in the inventory.
- Confirmed the versioned archive unit unsets only `WECOM_CORP_ID` and `WECOM_ARCHIVE_SECRET`; it does not touch `WECOM_THIRD_PARTY_*`.

Risks or gaps:
- **OPS EVIDENCE REQUIRED.** Repository evidence cannot prove installed optional units/manual runbooks, effective production systemd state, or live media/external-contact/callback behavior.
- Repository code cannot prove whether `WECOM_EXTERNAL_CONTACT_SECRET` / `WECOM_OAUTH_SECRET` are provider-global or eventually need per-tenant storage. GH-108 removes them as tenant selectors; Ops/product evidence must decide any later credential-authority migration.
- Production transition must deploy, verify media, external-contact reconciliation, archive timer, and callback; prove no installed workload selects via the legacy pair; only then retire `rnd419-phase2.conf`.

#93 Gate 4 repository-side: PASS

Production verification still required: YES

No secrets introduced: confirmed
Only intentional files changed: confirmed (verified with git status --short)
No test weakened, skipped, or deleted: confirmed
Staged paths listed explicitly, no `git add .`: confirmed
