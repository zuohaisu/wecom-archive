import html as _html
from datetime import datetime, timedelta, timezone
from typing import Optional


def _e(value) -> str:
    """HTML-escape any value for safe inline rendering."""
    return _html.escape(str(value)) if value is not None else ""


# Beijing time has used a fixed UTC+8 offset (no DST) since 1991; a fixed-offset
# timezone avoids depending on system tzdata being installed at deploy time.
_BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _fmt_msgtime(ms: Optional[int]) -> str:
    """Format an epoch-ms timestamp as Beijing time (UTC+8) for admin display only.

    Stored/raw msgtime values are untouched; this is purely for rendering.
    """
    if ms is None:
        return ""
    try:
        return (
            datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
            .astimezone(_BEIJING_TZ)
            .strftime("%Y-%m-%d %H:%M:%S")
        )
    except Exception:
        return _e(ms)


def _badge(status: str) -> str:
    cls = f"badge badge-{_e(status)}"
    return f'<span class="{cls}">{_e(status)}</span>'
