"""Safe, lightweight connectivity checks for saved deployment configuration."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from app.auth import get_wecom_token
from app.qiniu_storage import QiniuStorageProvider

_DOMAIN_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def check_qiniu(
    access_key: str,
    secret_key: str,
    bucket: str,
    domain: str,
    region: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Verify Qiniu credentials with one small SDK-backed list request."""
    try:
        provider = QiniuStorageProvider(access_key, secret_key, bucket, domain, region=region)
        _items, info, _eof = provider._bucket_manager.list(bucket, limit=1)
        if not info.ok():
            return False, "qiniu_connection_failed"
    except Exception:  # noqa: BLE001 - never expose SDK/configuration details
        return False, "qiniu_connection_failed"
    return True, None


def check_wecom(corp_id: str, oauth_secret: str) -> tuple[bool, Optional[str]]:
    """Verify WeCom credentials without sharing the production token cache."""
    try:
        get_wecom_token(
            corp_id,
            oauth_secret,
            cache_key="config-self-check:wecom",
        )
    except Exception:  # noqa: BLE001 - never expose provider details or credentials
        return False, "wecom_connection_failed"
    return True, None


def check_domain_format(domain: str) -> tuple[bool, Optional[str]]:
    """Validate an HTTPS domain URL locally, without a network request."""
    try:
        parsed = urlparse(domain.strip())
        hostname = parsed.hostname
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or hostname is None
        ):
            return False, "invalid_domain_format"
        parsed.port  # Validate a supplied port without including it in an error.
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return False, "invalid_domain_format"

    labels = ascii_hostname.split(".")
    if (
        len(ascii_hostname) > 253
        or len(labels) < 2
        or any(not _DOMAIN_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
    ):
        return False, "invalid_domain_format"
    return True, None
