# RND-389 QA Report — 非生产 E2E 验证结果

> Skeleton only. Fill this in when the run happens (after RND-351 / RND-392 / RND-352 are
> Done), then rename to `RND-389-qa-summary.md`. Do not run against production.

## Run metadata

- Date / operator: `________`
- Deployment commit(s): `________` (T1–T3 SHAs on the non-prod env)
- Base URL: `________`
- Test corp (RND-352): `________`
- Driver command + env var names used (values redacted): `________`

## Step results (copy from the driver's step table)

| Step | Status | Detail |
|---|---|---|
| provisioning_session | ☐ ok / ☐ fail / ☐ skip | |
| negative_wrong_secret | ☐ ok / ☐ fail / ☐ skip | |
| negative_no_entitlement | ☐ ok / ☐ fail / ☐ skip | |
| configure | ☐ ok / ☐ fail / ☐ skip | |
| self_test | ☐ ok / ☐ fail / ☐ skip | |
| wait_ready | ☐ ok / ☐ fail / ☐ skip | |
| activate | ☐ ok / ☐ fail / ☐ skip | |
| replay_idempotent | ☐ ok / ☐ fail / ☐ skip | |
| post_activation_access | ☐ ok / ☐ fail / ☐ skip | |
| billing_probe | ☐ ok / ☐ fail / ☐ skip | |
| first_message | ☐ ok / ☐ fail / ☐ skip | |
| second_tenant_isolation | ☐ ok / ☐ fail / ☐ skip | |

Exit code: `____`

## Acceptance criteria (from the Linear issue)

- [ ] Non-prod full-journey: 授权 → 配置 → 自检 → 自动激活 → 首条消息归档, all green.
- [ ] Negative paths: wrong secret blocked; expired/no subscription → `no_entitlement`, no grant.
- [ ] Exactly one `self_service_trial` subscription row; replay/second activate does not double-grant.
- [ ] Post-activation access transition: provisioning surface 401, `/admin/login` → `/dashboard`.
- [ ] Per-tenant runtime: `sync_states` + `archive_messages` rows are tenant-scoped; second-tenant probe passes (or documented as skipped).
- [ ] No secret/corp_id/message content appears in driver output, logs, or this report.

## Notes / deviations

`________`

## Verdict

- [ ] PASS — report PASS in Linear (RND-389)
- [ ] FAIL — list failing steps, link component tickets (RND-386/387/388) for the fixes, re-run after fix
