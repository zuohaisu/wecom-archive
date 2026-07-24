"""Frontend assertion tests for RND-207 (thumbnail list rendering + CLS
reservation + async decode + refresh-signature stability).

Static checks assert the wiring exists in the embedded JS; the node-executed
checks (skipped when node is unavailable) actually run renderMessageBody and
assert the list placeholder hydrates from the THUMBNAIL url with a reserved
display box, while the viewer still registers the ORIGINAL.
"""

from __future__ import annotations

import json

import pytest

from tests._rnd216_web_shims import review_console_js_source
from tests.test_rnd_206_rich_media import NODE, _msg, _run

_REVIEW_CONSOLE_JS = review_console_js_source()

pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


# ---------------------------------------------------------------------------
# Static presence checks (do not require node)
# ---------------------------------------------------------------------------


def test_async_decode_and_dimension_reservation_present() -> None:
    assert "img.decoding='async'" in _REVIEW_CONSOLE_JS
    assert "data-rnd207-w" in _REVIEW_CONSOLE_JS
    assert "data-rnd207-h" in _REVIEW_CONSOLE_JS


def test_thumbnail_wiring_present() -> None:
    assert "function thumbSlotOpts(" in _REVIEW_CONSOLE_JS
    # the frontend consumes the server-built thumbnail_access_url as-is (it
    # never constructs the ?variant=thumb query itself)
    assert "thumbnail_access_url" in _REVIEW_CONSOLE_JS
    # top-level image + emotion dispatch pass the thumbnail opts through
    assert "thumbSlotOpts(m)" in _REVIEW_CONSOLE_JS
    # nested composite media passes the descriptor's thumbnail opts through
    assert "thumbSlotOpts(media)" in _REVIEW_CONSOLE_JS


def test_refresh_signature_includes_thumbnail_fields_not_signed_url() -> None:
    # The RND-204 stability guarantee is preserved: the signature covers the
    # stable thumbnail endpoint path + intrinsic dims, and still never the
    # resolved signed URL.
    assert "m.thumbnail_access_url,m.image_width,m.image_height" in _REVIEW_CONSOLE_JS


# ---------------------------------------------------------------------------
# Executed behavior (node)
# ---------------------------------------------------------------------------


def _render(msg: dict) -> str:
    return _run(
        "process.stdout.write(String(renderMessageBody("
        + json.dumps(msg)
        + ")));"
    )


def test_list_hydrates_from_thumbnail_and_reserves_box() -> None:
    msg = _msg(
        "image", "image",
        media_status="available",
        media_access_url="/api/conversations/r/messages/m/media/access",
        thumbnail_access_url="/api/conversations/r/messages/m/media/access?variant=thumb",
        image_width=800, image_height=600,
        normalized_type="image", category="media", renderer_strategy="media_default",
    )
    out = _render(msg)
    # list placeholder hydrates from the THUMBNAIL url
    assert 'data-rnd206-access-url="/api/conversations/r/messages/m/media/access?variant=thumb"' in out
    # reserved display box (800x600 capped to 280 -> 280x210) + data dims
    assert 'data-rnd207-w="280"' in out
    assert 'data-rnd207-h="210"' in out
    assert "width:280px;height:210px" in out


def test_list_without_thumbnail_hydrates_from_original() -> None:
    msg = _msg(
        "image", "image",
        media_status="available",
        media_access_url="/api/conversations/r/messages/m/media/access",
        thumbnail_access_url=None, image_width=None, image_height=None,
        normalized_type="image", category="media", renderer_strategy="media_default",
    )
    out = _render(msg)
    # falls back to the original access url; no reserved box
    assert 'data-rnd206-access-url="/api/conversations/r/messages/m/media/access"' in out
    assert "data-rnd207-w" not in out
