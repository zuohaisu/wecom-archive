# F0 账号体系后端 — 完成度与前端对接核验（2026-07-28）

## 结论：地基已完成且代码已提交，但上线前必须跑一次 migration

### Linear 状态（5 张计划内子票全部 Done）
| 票 | 内容 | 状态 |
|---|---|---|
| RND-277 (F0-1) | AdminUser 模型扩展 + Alembic migration | Done / committed |
| RND-276 (F0-2) | per-user 密码鉴权（替换 env 单 hash） | Done / committed |
| RND-278 (F0-3) | password_reset_tokens 表 + 邮箱找回 | Done / committed |
| RND-279 (F0-4) | 会话生命周期自动化 + last_active_at | Done / committed |
| RND-280 (F0-5) | 角色枚举 + require_role scaffold | Done / committed |
| RND-321 | 企业微信扫码登录 | **Backlog（未做，属增强，非地基）** |

> F0 epic 本身仍 `In Progress`，仅因 RND-321 未做；计划内 5 张已全部收口。

### 代码实地核验（git 干净，5 commit 齐全：d35a017/aa7c676/84ea560/ad490c3/efa86b3）
- `models.py`：`AdminUser` 已含 `password_hash/role/status/email/phone/department/last_active_at/invite_token/invited_by/invite_status`，`PasswordResetToken` 表存在。
- `auth.py`：`password_login` 走 per-user（`func.lower(email)` + `password_hash` 校验），失败回退 env 凭据并 `_upsert_env_admin_user`；`/api/auth/me` 返回真实 `role`。
- `session_lifecycle.py`：维护 `last_active_at`；`app/auth.py`：`require_role` scaffold 就位（`ADMIN_ROLES` 枚举）。
- migration `0017_admin_users_account_fields.py` + `0018_password_reset_tokens.py` 已提交。

### 对接前端前必须做的两件事
1. **应用数据库迁移**（阻塞项）：`backend/app/main.py:54` 注释明确——服务启动前必须先 `alembic upgrade head`，否则新列不存在会抛 `UndefinedColumn`。项目无自动 migration，`scripts/deploy_server.sh` 会跑，本地 dev 需手动执行：
   ```bash
   cd backend && alembic upgrade head
   ```
2. **首次管理员是 env 引导账户**：`_upsert_env_admin_user`（auth.py:369）创建的账户 **role=None / status=None / password_hash=None**——它只用于密码模式（AUTH_MODE=password）下用 env 凭据登录，不会成为带角色的 per-user 账户。真正带角色、可改密、可走找回流程的账户，需经邀请流程（**A3-2，尚未建**）或手动补 `role/status`。现有 review_console 等前端不依赖 role，登录可用。

### 前端对接可行性
- ✅ 密码模式 + env 凭据：可直接登录、签发会话、`/api/auth/me` 可用。
- ⚠️ 角色化前端页面（users/settings/RBAC，A1–A9）尚未建，当前无前端依赖 role 字段，故不阻塞。
- ❌ 企业微信扫码登录（RND-321）未做，PC 端扫码场景暂不可用（企微内 OAuth 静默授权仍可用）。
