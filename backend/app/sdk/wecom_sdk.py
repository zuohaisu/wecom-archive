"""
Minimal ctypes wrapper for the WeCom Conversation Archive C SDK.

Covers NewSdk, Init, DestroySdk, GetChatData, NewSlice, FreeSlice,
GetSliceLen, GetContentFromSlice, DecryptData, and the media-download
family (NewMediaData, FreeMediaData, GetMediaData, GetData, GetDataLen,
GetOutIndexBuf, GetIndexLen, IsMediaDataFinish — see
configure_sdk_media_data and iter_media_chunks below).
"""

import ctypes
from pathlib import Path


class SdkMediaError(Exception):
    """Raised when a WeCom media-download SDK call fails.

    Messages here must never embed a sdkfileid, indexbuf, or any other
    media identifier — only a short internal diagnostic (return code,
    stage name).
    """


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


def configure_sdk_decrypt_data(lib: ctypes.CDLL) -> None:
    """Set ctypes signatures for DecryptData.

    Deployed SDK signature (no SDK handle):
        int DecryptData(const char* encrypt_key,
                        const char* encrypt_msg, Slice_t* msg);
    """
    lib.DecryptData.argtypes = [
        ctypes.c_char_p,    # encrypt_key
        ctypes.c_char_p,    # encrypt_msg
        ctypes.c_void_p,    # output: Slice_t*
    ]
    lib.DecryptData.restype = ctypes.c_int


def decrypt_data(
    lib: ctypes.CDLL,
    encrypt_key: str,
    encrypt_msg: str,
    slice_ptr: ctypes.c_void_p,
) -> int:
    """Call DecryptData. Writes decrypted JSON into slice_ptr. Returns 0 on success.

    The deployed C SDK does NOT take an SDK handle — only encrypt_key,
    encrypt_msg, and an output Slice_t pointer.
    """
    return lib.DecryptData(
        encrypt_key.encode("utf-8"),
        encrypt_msg.encode("utf-8"),
        slice_ptr,
    )


# ---------------------------------------------------------------------------
# Media download (RND-147)
# ---------------------------------------------------------------------------
#
# Deployed SDK signature (WeCom Conversation Archive C SDK, MediaData_t
# family):
#
#   MediaData_t *NewMediaData();
#   void FreeMediaData(MediaData_t *media_data);
#   int GetMediaData(WeWorkFinanceSdk_t *sdk, const char *indexbuf,
#                     const char *sdkfileid, const char *proxy,
#                     const char *passwd, int timeout,
#                     MediaData_t *media_data);
#   char *GetData(MediaData_t *media_data);
#   int GetDataLen(MediaData_t *media_data);
#   char *GetOutIndexBuf(MediaData_t *media_data);
#   int GetIndexLen(MediaData_t *media_data);
#   int IsMediaDataFinish(MediaData_t *media_data);
#
# A large media file is downloaded across multiple GetMediaData calls: each
# call fills one chunk into media_data and reports whether more chunks
# remain (IsMediaDataFinish) plus an opaque indexbuf to pass as the next
# call's index argument. GetData/GetOutIndexBuf are not in the caller's
# enumerated symbol list but are the same MediaData_t API family and are
# required to actually read a chunk's bytes and the next-page token — the
# five enumerated symbols alone cannot produce any file content.

_DEFAULT_MAX_MEDIA_CHUNKS = 10_000


def configure_sdk_media_data(lib: ctypes.CDLL) -> None:
    """Set ctypes signatures for the media-download family.

    Raises AttributeError if the loaded library does not export one of
    these symbols — callers must catch this and treat media download as
    unavailable in this deployment rather than crashing the whole SDK
    wrapper (mirrors configure_sdk_decrypt_data's contract).
    """
    lib.NewMediaData.argtypes = []
    lib.NewMediaData.restype = ctypes.c_void_p

    lib.FreeMediaData.argtypes = [ctypes.c_void_p]
    lib.FreeMediaData.restype = None

    lib.GetMediaData.argtypes = [
        ctypes.c_void_p,  # sdk
        ctypes.c_char_p,  # indexbuf (NULL/empty on the first call)
        ctypes.c_char_p,  # sdkfileid
        ctypes.c_char_p,  # proxy (NULL = no proxy)
        ctypes.c_char_p,  # passwd (NULL = no proxy auth)
        ctypes.c_int,  # timeout in seconds
        ctypes.c_void_p,  # output: MediaData_t*
    ]
    lib.GetMediaData.restype = ctypes.c_int

    # Binary-safe accessor: restype is a raw pointer value (c_void_p), never
    # c_char_p. c_char_p would make ctypes treat the buffer as a
    # NUL-terminated C string and silently truncate at the first zero byte
    # — media bytes are arbitrary binary and routinely contain embedded
    # NULs. Callers must pair this pointer with GetDataLen and
    # ctypes.string_at(ptr, length); see get_media_data_bytes below.
    lib.GetData.argtypes = [ctypes.c_void_p]
    lib.GetData.restype = ctypes.c_void_p

    lib.GetDataLen.argtypes = [ctypes.c_void_p]
    lib.GetDataLen.restype = ctypes.c_int

    # The next-page index token is an opaque identifier string, not media
    # content — c_char_p (NUL-terminated) is the correct, SDK-documented
    # representation for it.
    lib.GetOutIndexBuf.argtypes = [ctypes.c_void_p]
    lib.GetOutIndexBuf.restype = ctypes.c_char_p

    lib.GetIndexLen.argtypes = [ctypes.c_void_p]
    lib.GetIndexLen.restype = ctypes.c_int

    lib.IsMediaDataFinish.argtypes = [ctypes.c_void_p]
    lib.IsMediaDataFinish.restype = ctypes.c_int


def new_media_data(lib: ctypes.CDLL):
    """Allocate a new MediaData_t via the SDK allocator."""
    return lib.NewMediaData()


def free_media_data(lib: ctypes.CDLL, media_data_ptr) -> None:
    """Free a MediaData_t buffer allocated by new_media_data."""
    lib.FreeMediaData(media_data_ptr)


def get_media_data(
    lib: ctypes.CDLL,
    handle,
    media_data_ptr,
    sdkfileid: str,
    indexbuf: str = "",
    proxy: str = "",
    passwd: str = "",
    timeout: int = 30,
) -> int:
    """Call GetMediaData for one chunk. Writes output into media_data_ptr.

    Returns 0 on success. indexbuf must be "" on the first call for a given
    sdkfileid and the previous call's next-index token thereafter.
    """
    return lib.GetMediaData(
        handle,
        indexbuf.encode("utf-8") if indexbuf else None,
        sdkfileid.encode("utf-8"),
        proxy.encode("utf-8") if proxy else None,
        passwd.encode("utf-8") if passwd else None,
        timeout,
        media_data_ptr,
    )


def get_media_data_bytes(lib: ctypes.CDLL, media_data_ptr) -> bytes:
    """Return the raw bytes copied out of one MediaData_t chunk buffer.

    Always uses GetDataLen for the copy length and ctypes.string_at for the
    copy — never relies on NUL-terminated string semantics — so embedded
    NUL bytes in binary image data are preserved exactly.
    """
    length = lib.GetDataLen(media_data_ptr)
    if length <= 0:
        return b""
    ptr = lib.GetData(media_data_ptr)
    if not ptr:
        return b""
    return ctypes.string_at(ptr, length)


def get_media_next_index(lib: ctypes.CDLL, media_data_ptr) -> str:
    """Return the opaque next-page index token for the following
    GetMediaData call, or "" when GetIndexLen reports nothing."""
    index_len = lib.GetIndexLen(media_data_ptr)
    if index_len <= 0:
        return ""
    raw = lib.GetOutIndexBuf(media_data_ptr)
    if not raw:
        return ""
    return raw.decode("utf-8", errors="ignore")


def is_media_data_finish(lib: ctypes.CDLL, media_data_ptr) -> bool:
    """Return True once the SDK reports no further chunks remain."""
    return bool(lib.IsMediaDataFinish(media_data_ptr))


def iter_media_chunks(
    lib: ctypes.CDLL,
    handle,
    sdkfileid: str,
    proxy: str = "",
    passwd: str = "",
    timeout: int = 30,
    max_chunks: int = _DEFAULT_MAX_MEDIA_CHUNKS,
):
    """Yield binary chunks for one media file, paginating GetMediaData
    calls until IsMediaDataFinish reports done.

    Each MediaData_t is freed immediately after its bytes are copied out
    and before the next call, regardless of success or failure. Raises
    SdkMediaError on a non-zero GetMediaData return code, a null
    NewMediaData allocation, or exceeding max_chunks (guards against a
    misbehaving SDK that never reports finished). Never logs or includes
    sdkfileid in any exception message.
    """
    if not sdkfileid:
        raise SdkMediaError("sdkfileid is required")

    index = ""
    chunks_yielded = 0

    while True:
        if chunks_yielded >= max_chunks:
            raise SdkMediaError("media download exceeded max chunk count")

        media_data_ptr = new_media_data(lib)
        if not media_data_ptr:
            raise SdkMediaError("NewMediaData returned a null pointer")

        try:
            ret = get_media_data(
                lib, handle, media_data_ptr, sdkfileid, index, proxy, passwd, timeout
            )
            if ret != 0:
                raise SdkMediaError(f"GetMediaData failed with return code {ret}")

            chunk = get_media_data_bytes(lib, media_data_ptr)
            finished = is_media_data_finish(lib, media_data_ptr)
            index = get_media_next_index(lib, media_data_ptr)
        finally:
            try:
                free_media_data(lib, media_data_ptr)
            except Exception:
                pass

        yield chunk
        chunks_yielded += 1

        if finished:
            break
