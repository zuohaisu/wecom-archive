"""
Minimal ctypes wrapper for the WeCom Conversation Archive C SDK.

Only covers NewSdk, Init, and DestroySdk.
Does not implement GetChatData, decryption, or media download.
"""

import ctypes
from pathlib import Path


def load_sdk(lib_path: str) -> ctypes.CDLL:
    """Load the SDK shared library. Raises OSError on failure."""
    if not Path(lib_path).exists():
        raise FileNotFoundError(f"SDK library not found at path: {lib_path}")
    return ctypes.CDLL(lib_path)


def configure_sdk(lib: ctypes.CDLL) -> None:
    """Set ctypes argtypes and restype for SDK functions."""
    # NewSdk() -> opaque pointer
    lib.NewSdk.argtypes = []
    lib.NewSdk.restype = ctypes.c_void_p

    # Init(sdk, corpid, secret) -> int (0 = success)
    lib.Init.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    lib.Init.restype = ctypes.c_int

    # DestroySdk(sdk) -> void
    lib.DestroySdk.argtypes = [ctypes.c_void_p]
    lib.DestroySdk.restype = None


def new_sdk(lib: ctypes.CDLL) -> ctypes.c_void_p:
    """Create a new SDK instance. Returns an opaque handle."""
    return lib.NewSdk()


def init_sdk(lib: ctypes.CDLL, handle: ctypes.c_void_p, corp_id: str, secret: str) -> int:
    """Initialize the SDK with corp credentials. Returns 0 on success."""
    return lib.Init(handle, corp_id.encode("utf-8"), secret.encode("utf-8"))


def destroy_sdk(lib: ctypes.CDLL, handle: ctypes.c_void_p) -> None:
    """Destroy the SDK handle and free resources."""
    lib.DestroySdk(handle)
