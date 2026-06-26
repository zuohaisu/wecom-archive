#!/usr/bin/env python3
"""
Smoke test: call WeCom Conversation Archive SDK GetChatData once.

Usage (from backend/):
    python scripts/smoke_wecom_get_chat_data.py

Required environment variables:
    WECOM_SDK_LIB_PATH     Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID          WeCom corporation ID
    WECOM_ARCHIVE_SECRET   WeCom conversation archive secret

Optional environment variables:
    WECOM_CHAT_SEQ         Start sequence number (default: 0)
    WECOM_CHAT_LIMIT       Max records to fetch (default: 10)

Exit codes:
    0  GetChatData returned 0 (success)
    1  Any failure (missing env, load error, Init error, GetChatData error)

Safety constraints:
    - No message content, encrypted payload, or customer data is printed.
    - No database writes.
    - No media downloads.
    - No decryption.
"""

import json
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.sdk import wecom_sdk


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


def _optional_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"[FAIL] Environment variable {name} is not a valid integer: {raw!r}", flush=True)
        sys.exit(1)


def main() -> None:
    print("[INFO] WeCom SDK GetChatData smoke test starting", flush=True)

    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")
    seq = _optional_int_env("WECOM_CHAT_SEQ", 0)
    limit = _optional_int_env("WECOM_CHAT_LIMIT", 10)

    print(f"[INFO] SDK lib path : {lib_path}", flush=True)
    print(f"[INFO] Corp ID      : {corp_id}", flush=True)
    print(f"[INFO] Secret       : {'*' * min(len(secret), 8)} (masked)", flush=True)
    print(f"[INFO] seq          : {seq}", flush=True)
    print(f"[INFO] limit        : {limit}", flush=True)

    # Load shared library
    try:
        lib = wecom_sdk.load_sdk(lib_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)
    except OSError as exc:
        print(f"[FAIL] Failed to load SDK library: {exc}", flush=True)
        sys.exit(1)

    print("[INFO] SDK library loaded", flush=True)

    # Configure ctypes signatures for Init path
    try:
        wecom_sdk.configure_sdk(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (Init path): {exc}", flush=True)
        sys.exit(1)

    # Configure ctypes signatures for GetChatData path
    try:
        wecom_sdk.configure_sdk_get_chat_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (GetChatData path): {exc}", flush=True)
        sys.exit(1)

    # NewSdk
    handle = wecom_sdk.new_sdk(lib)
    if not handle:
        print("[FAIL] NewSdk() returned a null handle", flush=True)
        sys.exit(1)

    print("[INFO] NewSdk() returned a valid handle", flush=True)

    # Init
    init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    print(f"[INFO] Init() return code: {init_ret}", flush=True)

    if init_ret != 0:
        print(f"[FAIL] Init() failed (return code {init_ret}); skipping GetChatData", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception as exc:
            print(f"[WARN] DestroySdk() raised: {exc}", flush=True)
        sys.exit(1)

    print("[INFO] Init() succeeded", flush=True)

    # Allocate output slice
    slice_ptr = wecom_sdk.new_slice(lib)
    if not slice_ptr:
        print("[FAIL] NewSlice() returned null; cannot call GetChatData", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception as exc:
            print(f"[WARN] DestroySdk() raised: {exc}", flush=True)
        sys.exit(1)

    # GetChatData
    chat_ret = wecom_sdk.get_chat_data(lib, handle, slice_ptr, seq, limit)
    print(f"[INFO] GetChatData() return code: {chat_ret}", flush=True)

    if chat_ret == 0:
        # Check if any data was returned
        slice_len = wecom_sdk.get_slice_len(lib, slice_ptr)
        print(f"[INFO] Slice buffer length: {slice_len} bytes", flush=True)

        if slice_len > 0:
            raw = wecom_sdk.get_content_from_slice(lib, slice_ptr)
            if raw:
                try:
                    parsed = json.loads(raw)
                    records = parsed.get("chatdata", [])
                    print(f"[INFO] Record count: {len(records)}", flush=True)
                except (json.JSONDecodeError, ValueError):
                    print("[WARN] Could not parse GetChatData response as JSON", flush=True)
            else:
                print("[INFO] GetContentFromSlice returned None", flush=True)
        else:
            print("[INFO] Slice is empty (no records returned)", flush=True)
    else:
        print(f"[WARN] GetChatData() returned non-zero code: {chat_ret}", flush=True)

    # FreeSlice — always attempt
    try:
        wecom_sdk.free_slice(lib, slice_ptr)
        print("[INFO] FreeSlice() called", flush=True)
    except Exception as exc:
        print(f"[WARN] FreeSlice() raised: {exc}", flush=True)

    # DestroySdk — always attempt
    try:
        wecom_sdk.destroy_sdk(lib, handle)
        print("[INFO] DestroySdk() called", flush=True)
    except Exception as exc:
        print(f"[WARN] DestroySdk() raised: {exc}", flush=True)

    if chat_ret == 0:
        print("[PASS] GetChatData smoke test passed", flush=True)
        sys.exit(0)
    else:
        print(f"[FAIL] GetChatData returned code {chat_ret}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
