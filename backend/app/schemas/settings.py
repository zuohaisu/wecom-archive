"""Request and response schemas for the deployment Settings API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SettingsFieldOut(BaseModel):
    key: str
    value: Any
    source: Literal["default", "env", "db"]
    requires_restart: bool


class SettingsGetOut(BaseModel):
    groups: dict[str, list[SettingsFieldOut]]


class SettingsUpdateIn(BaseModel):
    updates: dict[str, Any] = Field(default_factory=dict)


class SettingsErrorItem(BaseModel):
    key: str
    message: str
    code: str


class SettingsUpdateOut(BaseModel):
    ok: bool
    restart_required_keys: list[str]
