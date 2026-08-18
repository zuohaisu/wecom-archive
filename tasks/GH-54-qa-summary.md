# GH-54 QA Summary — 外部联系人头像 http→https 升级

## Files changed
- `backend/app/services/avatar_sync.py`:
  - 新增 `_upgrade_wecom_http()`：仅对白名单域名（qlogo.cn/qpic.cn）的 http URL 升级为 https，其余 URL 原样返回
  - `sync_avatar_from_source()` 在 `_clean_url` 之后、`_source_is_allowed` 之前调用升级（唯一写入入口，覆盖内部/外部全部头像）
  - `urllib.parse` import 补 `urlunsplit`；模块 docstring 同步更新
- `backend/tests/test_contact_avatars.py`:
  - `test_upgrade_wecom_http_only_touches_allowlisted_hosts`：7 个边界用例（子域/裸域/qpic.cn/https 不变/非白名单 http 不变/后缀伪造不变）
  - `test_external_http_avatar_from_wecom_cdn_is_upgraded_to_https`：http://wx.qlogo.cn → 下载收到 https 版本，status=ready
  - `test_http_avatar_from_untrusted_host_stays_invalid_without_network`：http 非白名单 → 仍 invalid 且零网络请求（安全边界不稀释）

## Acceptance criteria
- [x] http://wx.qlogo.cn 头像 URL 成功缓存为 ready：`test_external_http_avatar_from_wecom_cdn_is_upgraded_to_https` pass
- [x] 非白名单 http URL 仍 invalid、不发网络：`test_http_avatar_from_untrusted_host_stays_invalid_without_network` pass
- [x] 安全边界不弱化：仅白名单域名升级，`_source_is_allowed` 的 https-only 判定原样生效（`test_upgrade_wecom_http_only_touches_allowlisted_hosts` pass）
- [x] sabotage 验证：临时移除升级行后新测试 FAIL（assert False），恢复后 pass —— 回归测试确实咬人

## Commands run
- `.venv/bin/python -m pytest tests/test_contact_avatars.py -q` → 8 passed
- `make lint-diff` → All checks passed!
- `make typecheck` → OK（import/syntax-level）
- `make test`（全量 ~3200 tests）→ 后台运行中，见 CI 结果

## Manual verification（生产实证，2026-08-18）
- 企微 `externalcontact/get` 实测 3 个样本 avatar 均为 `http://wx.qlogo.cn/mmhead/...`
- `https://wx.qlogo.cn/mmhead/...` 实测可下载：200, image/jpeg, 123KB
- 生产分布：ready 76 / invalid 11,537 / missing 21（missing 为企微 detail 无 avatar，正常）

## Risks or gaps
- 无。改动限定在单一入口；http URL 只有白名单域名会被升级；`_download_avatar` 的防御性 `_source_is_allowed` 保持不变
- 内部成员头像（contacts 表）是企微成员敏感信息权限问题（GH #54 范围外，另案处理）
- 部署后需手动重跑 reconcile oneshot 回填 11,537 个头像（约 1–3 小时），或等次日 04:15 定时任务

## No secrets introduced: confirmed
## Only intentional files changed: confirmed
