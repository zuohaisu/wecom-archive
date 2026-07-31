"""Shared constants for the Settings configuration registry."""

from __future__ import annotations

from enum import Enum

RESOLVE_ORDER = ("db", "env", "default")

# Changing either selector changes the implementation used for newly written
# media. Existing rows retain their own backend reference, but restarting
# prevents in-flight workers from operating under a mixed configuration.
RESTART_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"media_storage_provider", "storage_backend"}
)


class ConfigGroup(str, Enum):
    """UI groupings for configurable Settings values."""

    GENERAL = "general"
    THIRD_PARTY = "third_party"
    STORAGE = "storage"
    WECOM = "wecom"
    ADVANCED = "advanced"
