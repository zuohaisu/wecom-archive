"""Read-only diagnostic tool handlers (RND-358 / T4).

Every handler here is a plain read: no db.add/commit/delete, no subprocess
beyond the one cached, argument-free `git rev-parse` used for the version
string, no arbitrary SQL. See backend/tests/test_ai_tools_read_only_boundary.py
for the automated guard on that property.

Handlers prefer reusing an existing service's pure-read computation
(capacity_from_values, get_subscription_summary, TenantWecomConfig,
SyncState, ReachabilityFinding) over writing a new query, so this module
stays a thin read-only view rather than a second implementation of logic
that already exists elsewhere. Note storage_quota_summary_handler
deliberately does NOT call app.services.storage_capacity's top-level
measure_storage_capacity — that convenience wrapper upserts a daily
rollup as a side effect, which would violate this package's read-only
guarantee; see its own docstring for the pure-read equivalent used here.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.resolver import resolve as resolve_config
from app.db.models import ReachabilityFinding, SyncState, Tenant, TenantWecomConfig
from app.services.ai_tools.registry import ToolContext, ToolScope, ToolSpec, register
from app.services.service_access import (
    INTERACTIVE,
    WORKER_MEDIA,
    tenant_service_allows,
)
from app.services.storage_capacity import read_storage_capacity

# Pages this tool will echo back verbatim. Kept as an independent, small
# allowlist rather than importing app.web.sidenav.NAV — that module can
# change shape for reasons unrelated to this diagnostic tool, and this
# list only needs to be "the pages we know how to talk about", not a
# mirror of the live navigation.
_KNOWN_PAGE_IDS = frozenset(
    {
        "dashboard",
        "billing",
        "conversations",
        "search",
        "messages",
        "media",
        "exports",
        "users",
        "contacts",
        "settings",
        "audit",
        "support",
    }
)

# Config keys diagnosed here are deliberately few and all boolean-presence
# only — see config_status_handler. Never add a key here without also
# confirming the handler never returns resolve_config()'s actual value.
_DIAGNOSED_CONFIG_KEYS = ("smtp_host", "media_storage_provider")

# A small, verified-real reference list (see RND-358 dev notes): every code
# below was found as a literal `?error=<code>` redirect in app/auth.py or
# app/routers/wecom_org_authorization.py, not invented for this tool.
_KNOWN_ERROR_CODES = {
    "config_error": "服务端 WeCom/企业微信相关配置缺失或无效，需要管理员检查环境配置",
    "auth_failed": "企业微信 OAuth 授权失败或返回的身份无法验证",
    "user_inactive": "对应管理员账号当前处于禁用状态",
    "access_pending": "账号访问请求尚待审批",
    "invalid_state": "OAuth state 校验失败（可能是链接过期或重复使用）",
    "organization_exists": "该企业微信组织已绑定过账号，无法重复创建",
    "organization_not_found": "未找到对应的组织记录",
}

_version_cache: dict = {}


def _product_version() -> str:
    if "value" in _version_cache:
        return _version_cache["value"]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        value = result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001 - version lookup must never break the diagnostic flow
        value = "unknown"
    _version_cache["value"] = value
    return value


def product_version_handler(db: Session, context: ToolContext) -> dict:
    return {"version": _product_version()}


def current_page_handler(db: Session, context: ToolContext) -> dict:
    page_id = context.page_context.get("page_id")
    return {"page_id": page_id if page_id in _KNOWN_PAGE_IDS else "unknown"}


def tenant_service_status_handler(db: Session, context: ToolContext) -> dict:
    tenant = db.execute(select(Tenant).where(Tenant.id == context.tenant_id)).scalar_one_or_none()
    if tenant is None:
        return {"lifecycle_status": "unknown", "is_active": False}
    return {
        "lifecycle_status": tenant.lifecycle_status,
        "is_active": tenant_service_allows(tenant.lifecycle_status, INTERACTIVE),
    }


def config_status_handler(db: Session, context: ToolContext) -> dict:
    wecom_connected = (
        db.execute(
            select(TenantWecomConfig.id).where(
                TenantWecomConfig.tenant_id == context.tenant_id,
                TenantWecomConfig.is_active.is_(True),
            )
        ).first()
        is not None
    )
    data = {"wecom_connected": wecom_connected}
    for key in _DIAGNOSED_CONFIG_KEYS:
        value = resolve_config(db, key)
        data[f"{key}_configured"] = bool(value and str(value).strip())
    return data


def sync_status_summary_handler(db: Session, context: ToolContext) -> dict:
    config = db.execute(
        select(TenantWecomConfig).where(
            TenantWecomConfig.tenant_id == context.tenant_id,
            TenantWecomConfig.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if config is None:
        return {"status": "not_connected", "last_synced_at": None, "has_recent_error": False}

    state = db.execute(
        select(SyncState).where(
            SyncState.tenant_id == context.tenant_id, SyncState.corp_id == config.corp_id
        )
    ).scalar_one_or_none()
    if state is None:
        return {"status": "idle", "last_synced_at": None, "has_recent_error": False}
    return {
        "status": state.status,
        "last_synced_at": state.started_at.isoformat() if state.started_at else None,
        "has_recent_error": bool(state.error_message),
    }


def storage_quota_summary_handler(db: Session, context: ToolContext) -> dict:
    # Use the same edition-aware capacity policy as media writes, but through
    # its read-only entry point so diagnostics never write TenantStorageDaily.
    now = datetime.now(timezone.utc)
    snapshot = read_storage_capacity(db, context.tenant_id, at=now)
    lifecycle_status = db.scalar(
        select(Tenant.lifecycle_status).where(Tenant.id == context.tenant_id)
    )
    service_allows_media = tenant_service_allows(lifecycle_status, WORKER_MEDIA)
    return {
        "quota_bytes": snapshot.quota_bytes,
        "used_bytes": snapshot.used_bytes,
        "remaining_bytes": snapshot.remaining_bytes,
        "utilization_basis_points": snapshot.utilization_basis_points,
        "usage_status": snapshot.usage_status,
        "can_accept_new_media": (
            snapshot.can_accept_new_media and service_allows_media
        ),
    }


def archive_health_summary_handler(db: Session, context: ToolContext) -> dict:
    """INTERNAL_SUPPORT only — see its ToolSpec below. Reachability
    findings are documented in app/db/models.py as an internal-only
    lifecycle record, so this tool's scope mirrors that, not the lighter
    sync_status_summary tenant admins already get."""
    active_finding_count = (
        db.execute(
            select(func.count(ReachabilityFinding.id)).where(
                ReachabilityFinding.tenant_id == context.tenant_id,
                ReachabilityFinding.status == "active",
            )
        ).scalar()
        or 0
    )
    sync = sync_status_summary_handler(db, context)
    return {
        "active_reachability_finding_count": active_finding_count,
        "sync_status": sync["status"],
        "last_synced_at": sync["last_synced_at"],
    }


def known_error_codes_handler(db: Session, context: ToolContext) -> dict:
    return {"error_codes": dict(_KNOWN_ERROR_CODES)}


register(
    ToolSpec(
        name="product_version",
        description="当前部署的产品版本标识",
        allowed_fields=frozenset({"version"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=product_version_handler,
    )
)
register(
    ToolSpec(
        name="current_page",
        description="用户当前所在的管理后台页面标识",
        allowed_fields=frozenset({"page_id"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=current_page_handler,
    )
)
register(
    ToolSpec(
        name="tenant_service_status",
        description="租户当前服务生命周期状态",
        allowed_fields=frozenset({"lifecycle_status", "is_active"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=tenant_service_status_handler,
    )
)
register(
    ToolSpec(
        name="config_status",
        description="非敏感配置项是否已设置（仅布尔值，不含具体配置内容）",
        allowed_fields=frozenset({"wecom_connected", "smtp_host_configured", "media_storage_provider_configured"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=config_status_handler,
    )
)
register(
    ToolSpec(
        name="sync_status_summary",
        description="最近一次企业微信会话同步状态摘要",
        allowed_fields=frozenset({"status", "last_synced_at", "has_recent_error"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=sync_status_summary_handler,
    )
)
register(
    ToolSpec(
        name="storage_quota_summary",
        description="存储配额使用情况摘要",
        allowed_fields=frozenset(
            {
                "quota_bytes",
                "used_bytes",
                "remaining_bytes",
                "utilization_basis_points",
                "usage_status",
                "can_accept_new_media",
            }
        ),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=storage_quota_summary_handler,
    )
)
register(
    ToolSpec(
        name="archive_health_summary",
        description="归档健康摘要（含未解决的可见性问题数量）——仅内部客服可见",
        allowed_fields=frozenset({"active_reachability_finding_count", "sync_status", "last_synced_at"}),
        required_scope=ToolScope.INTERNAL_SUPPORT,
        handler=archive_health_summary_handler,
    )
)
register(
    ToolSpec(
        name="known_error_codes",
        description="已知错误码及其含义（静态参考，非租户特定数据）",
        allowed_fields=frozenset({"error_codes"}),
        required_scope=ToolScope.TENANT_ADMIN,
        handler=known_error_codes_handler,
    )
)
