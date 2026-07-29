"""Authentication request and response schemas."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict

DEFAULT_THEME = "light"
DEFAULT_LOCALE = "zh-CN"
ALLOWED_THEMES = ["light", "dark"]
ALLOWED_LOCALES = ["zh-CN", "zh-TW", "en"]


class PreferencesUpdate(BaseModel):
    """Partial update of the current user's UI preferences."""

    model_config = ConfigDict(extra="forbid")

    theme: Optional[Literal["light", "dark"]] = None
    locale: Optional[Literal["zh-CN", "zh-TW", "en"]] = None


class PreferencesOut(BaseModel):
    theme: str
    locale: str
