"""
Tests for RND-147 — WeCom SDK media-download bindings.

Scope: app/sdk/wecom_sdk.py's MediaData_t family (configure_sdk_media_data,
get_media_data_bytes, iter_media_chunks). Exercises the binary-safety
contract (explicit-length copies, NUL-byte preservation, never relying on
c_char_p/NUL-terminated semantics for the media payload) using a mocked
ctypes.CDLL — no real WeCom shared library is required.

Run (from backend/):
    pytest tests/test_wecom_sdk_media.py -v
"""

from __future__ import annotations

import ctypes
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# configure_sdk_media_data — missing symbols surface as AttributeError
# ---------------------------------------------------------------------------


def test_configure_sdk_media_data_missing_symbol_raises_attribute_error() -> None:
    from app.sdk import wecom_sdk

    class FakeLibMissingMediaSymbols:
        pass

    with pytest.raises(AttributeError):
        wecom_sdk.configure_sdk_media_data(FakeLibMissingMediaSymbols())


# ---------------------------------------------------------------------------
# get_media_data_bytes — explicit length copy, NUL-byte safe
# ---------------------------------------------------------------------------


def test_get_media_data_bytes_preserves_embedded_nul_bytes() -> None:
    from app.sdk import wecom_sdk

    payload = b"abc\x00def\x00\x00ghi"
    buf = ctypes.create_string_buffer(payload, len(payload))
    address = ctypes.addressof(buf)

    lib = MagicMock()
    lib.GetDataLen.return_value = len(payload)
    lib.GetData.return_value = address

    result = wecom_sdk.get_media_data_bytes(lib, ctypes.c_void_p(1))

    assert result == payload
    assert len(result) == len(payload)
    lib.GetDataLen.assert_called_once()
    lib.GetData.assert_called_once()


def test_get_media_data_bytes_zero_length_returns_empty_without_reading_pointer() -> None:
    from app.sdk import wecom_sdk

    lib = MagicMock()
    lib.GetDataLen.return_value = 0

    result = wecom_sdk.get_media_data_bytes(lib, ctypes.c_void_p(1))

    assert result == b""
    lib.GetData.assert_not_called()


def test_get_media_data_bytes_null_pointer_returns_empty() -> None:
    from app.sdk import wecom_sdk

    lib = MagicMock()
    lib.GetDataLen.return_value = 10
    lib.GetData.return_value = None

    result = wecom_sdk.get_media_data_bytes(lib, ctypes.c_void_p(1))

    assert result == b""


def test_get_media_data_bytes_does_not_truncate_at_first_nul() -> None:
    """The whole point of using GetDataLen + string_at instead of c_char_p:
    a naive c_char_p read would stop at the first \\x00 and silently
    truncate binary image data."""
    from app.sdk import wecom_sdk

    payload = b"\x00\x01\x02\x00\xff\xfe"
    buf = ctypes.create_string_buffer(payload, len(payload))
    address = ctypes.addressof(buf)

    lib = MagicMock()
    lib.GetDataLen.return_value = len(payload)
    lib.GetData.return_value = address

    result = wecom_sdk.get_media_data_bytes(lib, ctypes.c_void_p(1))
    assert result == payload


# ---------------------------------------------------------------------------
# iter_media_chunks — pagination, error handling, cleanup
# ---------------------------------------------------------------------------


def test_iter_media_chunks_requires_sdkfileid() -> None:
    from app.sdk import wecom_sdk

    with pytest.raises(wecom_sdk.SdkMediaError):
        list(wecom_sdk.iter_media_chunks(MagicMock(), MagicMock(), ""))


def test_iter_media_chunks_paginates_until_finish_and_uses_returned_index() -> None:
    from app.sdk import wecom_sdk

    payload1 = b"first-chunk-bytes"
    payload2 = b"second-chunk\x00with-nul"
    buf1 = ctypes.create_string_buffer(payload1, len(payload1))
    buf2 = ctypes.create_string_buffer(payload2, len(payload2))

    lib = MagicMock()
    lib.NewMediaData.side_effect = [111, 222]
    lib.GetMediaData.return_value = 0
    lib.GetData.side_effect = [ctypes.addressof(buf1), ctypes.addressof(buf2)]
    lib.GetDataLen.side_effect = [len(payload1), len(payload2)]
    lib.IsMediaDataFinish.side_effect = [0, 1]
    lib.GetIndexLen.side_effect = [4, 0]
    lib.GetOutIndexBuf.side_effect = [b"next", b""]

    handle = MagicMock()
    chunks = list(wecom_sdk.iter_media_chunks(lib, handle, "some-sdk-file-id"))

    assert chunks == [payload1, payload2]
    assert lib.GetMediaData.call_count == 2
    assert lib.FreeMediaData.call_count == 2

    first_call_args = lib.GetMediaData.call_args_list[0][0]
    second_call_args = lib.GetMediaData.call_args_list[1][0]
    assert first_call_args[1] is None  # first call: no index yet
    assert second_call_args[1] == b"next"  # second call: prior page's index


def test_iter_media_chunks_raises_on_nonzero_return_code_without_leaking_identifier() -> None:
    from app.sdk import wecom_sdk

    lib = MagicMock()
    lib.NewMediaData.return_value = 111
    lib.GetMediaData.return_value = 90002

    with pytest.raises(wecom_sdk.SdkMediaError) as exc_info:
        list(wecom_sdk.iter_media_chunks(lib, MagicMock(), "super-secret-sdk-file-id"))

    assert "super-secret-sdk-file-id" not in str(exc_info.value)
    lib.FreeMediaData.assert_called_once()


def test_iter_media_chunks_raises_on_null_alloc() -> None:
    from app.sdk import wecom_sdk

    lib = MagicMock()
    lib.NewMediaData.return_value = None

    with pytest.raises(wecom_sdk.SdkMediaError):
        list(wecom_sdk.iter_media_chunks(lib, MagicMock(), "sdk-file-id"))


def test_iter_media_chunks_enforces_max_chunk_count() -> None:
    """A misbehaving SDK that never reports IsMediaDataFinish must not spin
    forever — the loop must bail out with a clear exception."""
    from app.sdk import wecom_sdk

    payload = b"x"
    buf = ctypes.create_string_buffer(payload, len(payload))

    lib = MagicMock()
    lib.NewMediaData.return_value = 111
    lib.GetMediaData.return_value = 0
    lib.GetData.return_value = ctypes.addressof(buf)
    lib.GetDataLen.return_value = len(payload)
    lib.IsMediaDataFinish.return_value = 0  # never finishes
    lib.GetIndexLen.return_value = 0
    lib.GetOutIndexBuf.return_value = b""

    with pytest.raises(wecom_sdk.SdkMediaError):
        list(
            wecom_sdk.iter_media_chunks(
                lib, MagicMock(), "sdk-file-id", max_chunks=3
            )
        )
    # Each attempted chunk must still be freed even though the loop aborts.
    assert lib.FreeMediaData.call_count == 3


def test_iter_media_chunks_frees_media_data_even_on_error() -> None:
    from app.sdk import wecom_sdk

    lib = MagicMock()
    lib.NewMediaData.return_value = 111
    lib.GetMediaData.return_value = 1  # non-zero => error

    with pytest.raises(wecom_sdk.SdkMediaError):
        list(wecom_sdk.iter_media_chunks(lib, MagicMock(), "sdk-file-id"))

    lib.FreeMediaData.assert_called_once_with(111)
