"""Run a small durable external-contact refresh queue batch.

This CLI is invoked by a systemd path unit for low-latency event work and by a
short retry timer for jobs that could not yet be read from the WeCom API. It
prints aggregate counts only.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.external_contact_refresh_worker import run_external_contact_refresh_queue
from app.services.tenant_credentials import (
    TenantCredentialError,
    active_tenant_configs,
    active_tenant_ids,
    credentials_for_active_config,
    tenant_log_tag,
)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(name)
    return value


def _positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except ValueError:
        return default


def main() -> int:
    try:
        database_url = _require_env("DATABASE_URL")
    except RuntimeError:
        print("[FAIL] external_contact_refresh configuration_missing", flush=True)
        return 1

    external_secret = os.environ.get("WECOM_EXTERNAL_CONTACT_SECRET", "").strip()
    if not external_secret:
        print("[INFO] external_contact_refresh status=skipped reason=not_configured", flush=True)
        return 0

    limit = _positive_int(os.environ.get("EXTERNAL_CONTACT_REFRESH_BATCH_SIZE", "25"), 25)
    engine = create_engine(database_url)
    selected = refreshed = unavailable = completed = failed = 0
    try:
        with Session(engine) as session:
            active_ids = active_tenant_ids(session)
            configs = active_tenant_configs(session)
            configured_ids = {config.tenant_id for config in configs}
            for missing_tenant_id in active_ids:
                if missing_tenant_id in configured_ids:
                    continue
                failed += 1
                print(
                    f"[WARN] external_contact_refresh tenant={tenant_log_tag(missing_tenant_id)} "
                    "status=failed error_class=tenant_config_unavailable",
                    flush=True,
                )
            for config in configs:
                tag = tenant_log_tag(config.tenant_id)
                try:
                    credentials = credentials_for_active_config(config)
                    summary = run_external_contact_refresh_queue(
                        session,
                        credentials.tenant_id,
                        credentials.corp_id,
                        external_secret,
                        limit=limit,
                    )
                except TenantCredentialError as exc:
                    session.rollback()
                    failed += 1
                    print(
                        f"[WARN] external_contact_refresh tenant={tag} status=failed "
                        f"error_class={exc.error_class}",
                        flush=True,
                    )
                    continue
                except Exception:  # noqa: BLE001 -- one tenant must not affect another
                    session.rollback()
                    failed += 1
                    print(
                        f"[WARN] external_contact_refresh tenant={tag} status=failed "
                        "error_class=tenant_processing_failed",
                        flush=True,
                    )
                    continue
                completed += 1
                selected += summary.selected
                refreshed += summary.refreshed
                unavailable += summary.unavailable
    except Exception:  # noqa: BLE001 -- do not expose DB or API error details
        print("[FAIL] external_contact_refresh status=failed", flush=True)
        return 1

    print(
        "[INFO] external_contact_refresh status=completed "
        f"tenant_completed={completed} tenant_failed={failed} selected={selected} "
        f"refreshed={refreshed} unavailable={unavailable}",
        flush=True,
    )
    return 1 if completed == 0 and failed else 0


if __name__ == "__main__":
    sys.exit(main())
