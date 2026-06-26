"""
Minimal ctypes wrapper for the WeCom Conversation Archive C SDK.

Covers NewSdk, Init, DestroySdk, GetChatData, NewSlice, FreeSlice,
GetSliceLen, and GetContentFromSlice.
Does not implement decryption or media download.
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


def configure_sdk_get_chat_data(lib: ctypes.CDLL) -> None:
    """Set ctypes signatures for GetChatData, NewSlice, FreeSlice, GetSliceLen, GetContentFromSlice."""
    # NewSlice() -> opaque pointer to a Slice_t buffer
    lib.NewSlice.argtypes = []
    lib.NewSlice.restype = ctypes.c_void_p

    # FreeSlice(slice) -> void
    lib.FreeSlice.argtypes = [ctypes.c_void_p]
    lib.FreeSlice.restype = None

    # GetChatData(sdk, seq, limit, proxy, passwd, timeout, chatdata) -> int (0 = success)
    lib.GetChatData.argtypes = [
        ctypes.c_void_p,    # sdk
        ctypes.c_ulonglong, # seq
        ctypes.c_uint,      # limit
        ctypes.c_char_p,    # proxy (NULL = no proxy)
        ctypes.c_char_p,    # passwd (NULL = no proxy auth)
        ctypes.c_int,       # timeout in seconds
        ctypes.c_void_p,    # output: Slice_t*
    ]
    lib.GetChatData.restype = ctypes.c_int

    # GetSliceLen(slice) -> int (byte length of buffer content)
    lib.GetSliceLen.argtypes = [ctypes.c_void_p]
    lib.GetSliceLen.restype = ctypes.c_int

    # GetContentFromSlice(slice) -> const char* (raw JSON bytes; do not print directly)
    lib.GetContentFromSlice.argtypes = [ctypes.c_void_p]
    lib.GetContentFromSlice.restype = ctypes.c_char_p


def new_slice(lib: ctypes.CDLL) -> ctypes.c_void_p:
    """Allocate a new Slice_t via the SDK allocator."""
    return lib.NewSlice()


def free_slice(lib: ctypes.CDLL, slice_ptr: ctypes.c_void_p) -> None:
    """Free a Slice_t buffer allocated by new_slice."""
    lib.FreeSlice(slice_ptr)


def get_chat_data(
    lib: ctypes.CDLL,
    handle: ctypes.c_void_p,
    slice_ptr: ctypes.c_void_p,
    seq: int,
    limit: int,
    proxy: str = "",
    passwd: str = "",
    timeout: int = 5,
) -> int:
    """Call GetChatData. Writes output into slice_ptr. Returns 0 on success."""
    return lib.GetChatData(
        handle,
        seq,
        limit,
        proxy.encode("utf-8") if proxy else None,
        passwd.encode("utf-8") if passwd else None,
        timeout,
        slice_ptr,
    )


def get_slice_len(lib: ctypes.CDLL, slice_ptr: ctypes.c_void_p) -> int:
    """Return the byte length of the data written into the slice."""
    return lib.GetSliceLen(slice_ptr)


def get_content_from_slice(lib: ctypes.CDLL, slice_ptr: ctypes.c_void_p):
    """Return raw bytes from the slice. Caller must not log or print this directly."""
    return lib.GetContentFromSlice(slice_ptr)
