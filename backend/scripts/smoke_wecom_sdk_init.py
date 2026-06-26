#!/usr/bin/env python3
"""
Smoke test: load and initialize the WeCom Conversation Archive C SDK.

Usage (from backend/):
    python scripts/smoke_wecom_sdk_init.py

Required environment variables:
    WECOM_SDK_LIB_PATH     Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID          WeCom corporation ID
    WECOM_ARCHIVE_SECRET   WeCom conversation archive secret

Exit codes:
    0  SDK loaded and Init returned 0 (success)
    1  Any failure (missing env, missing file, load error, null handle, Init error)
"""

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


def main() -> None:
    print("[INFO] WeCom SDK smoke test starting", flush=True)

    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")

    print(f"[INFO] SDK lib path : {lib_path}", flush=True)
    print(f"[INFO] Corp ID      : {corp_id}", flush=True)
    print(f"[INFO] Secret       : {'*' * min(len(secret), 8)} (masked)", flush=True)

    # Load the shared library
    try:
        lib = wecom_sdk.load_sdk(lib_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)
    except OSError as exc:
        print(f"[FAIL] Failed to load SDK library: {exc}", flush=True)
        sys.exit(1)

    print("[INFO] SDK library loaded successfully", flush=True)

    # Configure ctypes signatures
    try:
        wecom_sdk.configure_sdk(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK is missing expected symbol: {exc}", flush=True)
        sys.exit(1)

    # NewSdk
    handle = wecom_sdk.new_sdk(lib)
    if not handle:
        print("[FAIL] NewSdk() returned a null handle", flush=True)
        sys.exit(1)

    print("[INFO] NewSdk() returned a valid handle", flush=True)

    # Init
    ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    print(f"[INFO] Init() return code: {ret}", flush=True)

    # DestroySdk — best-effort cleanup regardless of Init result
    try:
        wecom_sdk.destroy_sdk(lib, handle)
        print("[INFO] DestroySdk() called", flush=True)
    except Exception as exc:
        print(f"[WARN] DestroySdk() raised an exception: {exc}", flush=True)

    if ret == 0:
        print("[PASS] SDK initialized successfully (Init returned 0)", flush=True)
        sys.exit(0)
    else:
        print(f"[FAIL] SDK Init failed (return code {ret})", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
