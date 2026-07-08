#!/usr/bin/env python3
"""
One-shot decrypt and normalise pipeline for WeCom archived text messages.

Loads environment, initialises the WeCom Finance SDK, reads pending/failed
archive_messages from PostgreSQL, RSA-decrypts encrypt_random_key using the
configured private key, passes the result to C SDK DecryptData, parses the
decrypted JSON, normalises fields into the existing schema columns, and
updates the row.

Only handles text (msgtype="text") messages.  All other msgtypes are counted
as skipped/unsupported.  Media download, image/audio/video handling, and AI
features are out of scope.

Idempotent — re-running re-processes records that still have decrypt_status
"pending" or "failed".

Usage (from backend/):
    python scripts/decrypt_wecom_messages_once.py

Required environment variables:
    DATABASE_URL              PostgreSQL connection string
    WECOM_SDK_LIB_PATH        Absolute path to libWeWorkFinanceSdk_C.so
    WECOM_CORP_ID             WeCom corporation ID
    WECOM_ARCHIVE_SECRET      WeCom conversation archive secret
    WECOM_PRIVATE_KEY_PATH    Absolute path to RSA private key PEM file
    WECOM_PUBLIC_KEY_VERSION  Expected publickey_ver for current private key

Exit codes:
    0  Success (all records processed, possibly some failed)
    1  Fatal initialisation failure (env, SDK load, DB connect)

Safety constraints:
    - No decrypted message content is printed.
    - No encrypted payloads are printed.
    - No private keys, secrets, or raw customer data is printed.
    - No decrypted encrypt_key is printed.
"""

from __future__ import annotations

import base64
import json
import os
import sys

# Allow running from backend/ without installing the package
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import ArchiveMessage, ArchiveMessageRecipient
from app.sdk import wecom_sdk


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"[FAIL] Environment variable not set or empty: {name}", flush=True)
        sys.exit(1)
    return value


# ---------------------------------------------------------------------------
# RSA decrypt
# ---------------------------------------------------------------------------


def _load_private_key(pem_path: str) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file path.

    The WeCom C SDK demo uses PKCS#1 v1.5 padding for RSA decryption of
    encrypt_random_key.  Returns the private key object.

    Raises: FileNotFoundError, ValueError, cryptography exceptions.
    """
    with open(pem_path, "rb") as f:
        pem_data = f.read()
    return serialization.load_pem_private_key(pem_data, password=None)


def _rsa_decrypt_encrypt_key(
    private_key: rsa.RSAPrivateKey, encrypt_random_key: str
) -> str | None:
    """RSA-decrypt the WeCom encrypt_random_key field.

    WeCom base64-encodes the RSA ciphertext.  The C SDK demo uses PKCS#1 v1.5
    padding.  Returns the UTF-8 decrypted encrypt_key.

    Returns None on failure.
    """
    try:
        ciphertext = base64.b64decode(encrypt_random_key)
        plaintext = private_key.decrypt(
            ciphertext,
            padding.PKCS1v15(),
        )
        return plaintext.decode("utf-8")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Decrypt helpers (C SDK)
# ---------------------------------------------------------------------------


def _decrypt_message(
    lib, encrypt_key: str, encrypt_msg: str
) -> tuple[int, str | None]:
    """
    Call the C SDK DecryptData and return (return_code, decrypted_json_string).

    On success (return_code == 0), the second element is the decrypted JSON
    string.  On failure, the second element is None.
    """
    slice_ptr = wecom_sdk.new_slice(lib)
    if not slice_ptr:
        return -2, None  # custom error: alloc failed

    try:
        ret = wecom_sdk.decrypt_data(lib, encrypt_key, encrypt_msg, slice_ptr)
        if ret != 0:
            return ret, None

        slice_len = wecom_sdk.get_slice_len(lib, slice_ptr)
        if slice_len <= 0:
            return ret, None  # success but empty — treat as no data

        raw = wecom_sdk.get_content_from_slice(lib, slice_ptr)
        if raw is None:
            return ret, None

        return ret, raw.decode("utf-8")
    finally:
        try:
            wecom_sdk.free_slice(lib, slice_ptr)
        except Exception:
            pass


def _normalise_fields(
    decrypted: dict,
) -> dict:
    """
    Extract normalised fields from the decrypted WeCom message dict.

    Expected WeCom decrypted JSON structure:
    {
        "msgtype": "text",
        "from": "userid",
        "tolist": ["userid1", "userid2"],
        "roomid": "room_id_or_empty",
        "msgtime": 1779901200000,
        "text": {"content": "message body"}
    }

    For non-text messages, msgtype is a different string and the payload
    field varies (e.g. "image" -> {"image": {"md5sum": ...}}).
    """
    msgtype = decrypted.get("msgtype", "") or ""
    sender = decrypted.get("from", "") or None
    roomid = decrypted.get("roomid", "") or None
    msgtime = decrypted.get("msgtime", None)
    tolist = decrypted.get("tolist", [])

    # Extract content_text only for text messages
    content_text = None
    if msgtype == "text":
        text_payload = decrypted.get("text", {}) or {}
        content_text = text_payload.get("content", "") or None

    # Extract sdkfileid if present (for media messages)
    sdkfileid = None
    if msgtype != "text":
        # media messages store sdkfileid in the msgtype-specific payload
        payload = decrypted.get(msgtype, {}) or {}
        sdkfileid = payload.get("sdkfileid", None)

    return {
        "msgtype": msgtype,
        "sender": sender,
        "roomid": roomid,
        "msgtime": msgtime,
        "tolist": tolist if tolist else None,
        "content_text": content_text,
        "sdkfileid": sdkfileid,
    }


def _upsert_recipients(
    session: Session,
    message_id: int,
    tolist: list,
    tenant_id: "str | None" = None,
) -> None:
    """Insert ArchiveMessageRecipient rows for each entry in tolist."""
    existing = {
        r.receiver_userid
        for r in session.query(ArchiveMessageRecipient)
        .filter(ArchiveMessageRecipient.message_id == message_id)
        .all()
    }
    for recipient in tolist:
        if recipient and recipient not in existing:
            session.add(
                ArchiveMessageRecipient(
                    message_id=message_id,
                    receiver_userid=recipient,
                    receiver_type="user",
                    tenant_id=tenant_id,
                )
            )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # --- 1. Load environment ---
    database_url = _require_env("DATABASE_URL")
    lib_path = _require_env("WECOM_SDK_LIB_PATH")
    corp_id = _require_env("WECOM_CORP_ID")
    secret = _require_env("WECOM_ARCHIVE_SECRET")
    private_key_path = _require_env("WECOM_PRIVATE_KEY_PATH")
    expected_pubkey_ver_str = _require_env("WECOM_PUBLIC_KEY_VERSION")

    try:
        expected_pubkey_ver = int(expected_pubkey_ver_str)
    except ValueError:
        print(
            f"[FAIL] WECOM_PUBLIC_KEY_VERSION is not a valid integer: "
            f"{expected_pubkey_ver_str!r}",
            flush=True,
        )
        sys.exit(1)

    # --- 2. Load RSA private key ---
    try:
        private_key = _load_private_key(private_key_path)
    except FileNotFoundError:
        print(
            f"[FAIL] Private key file not found: {os.path.basename(private_key_path)}",
            flush=True,
        )
        sys.exit(1)
    except Exception as exc:
        print(f"[FAIL] Failed to load private key: {exc}", flush=True)
        sys.exit(1)

    # --- 3. Initialise WeCom SDK ---
    try:
        lib = wecom_sdk.load_sdk(lib_path)
    except FileNotFoundError as exc:
        print(f"[FAIL] {exc}", flush=True)
        sys.exit(1)
    except OSError as exc:
        print(f"[FAIL] Failed to load SDK library: {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (Init path): {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk_get_chat_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (GetChatData path): {exc}", flush=True)
        sys.exit(1)

    try:
        wecom_sdk.configure_sdk_decrypt_data(lib)
    except AttributeError as exc:
        print(f"[FAIL] SDK missing expected symbol (DecryptData path): {exc}", flush=True)
        sys.exit(1)

    handle = wecom_sdk.new_sdk(lib)
    if not handle:
        print("[FAIL] NewSdk() returned a null handle", flush=True)
        sys.exit(1)

    init_ret = wecom_sdk.init_sdk(lib, handle, corp_id, secret)
    if init_ret != 0:
        print(f"[FAIL] Init() failed (return code {init_ret})", flush=True)
        try:
            wecom_sdk.destroy_sdk(lib, handle)
        except Exception:
            pass
        sys.exit(1)

    # --- 4. Read pending/failed records ---
    engine = create_engine(database_url)

    scanned = 0
    success = 0
    failed = 0
    unsupported = 0
    pending_remaining = 0
    key_mismatch = 0
    rsa_failed = 0
    recipient_upsert_failed = 0
    return_codes: dict[int, int] = {}

    with Session(engine) as session:
        records = (
            session.query(ArchiveMessage)
            .filter(
                ArchiveMessage.decrypt_status.in_(["pending", "failed"])
            )
            .order_by(ArchiveMessage.id)
            .all()
        )

        scanned = len(records)

        for record in records:
            encrypt_key_raw = record.encrypt_random_key
            encrypt_msg = record.encrypt_chat_msg

            # SF-2: Guard against missing encrypted fields
            if not encrypt_key_raw or not encrypt_msg:
                record.decrypt_status = "failed"
                failed += 1
                return_codes[-1] = return_codes.get(-1, 0) + 1
                continue

            # --- Key version check ---
            if record.publickey_ver != expected_pubkey_ver:
                record.decrypt_status = "failed"
                failed += 1
                key_mismatch += 1
                continue

            # --- RSA-decrypt encrypt_random_key ---
            encrypt_key = _rsa_decrypt_encrypt_key(private_key, encrypt_key_raw)
            if encrypt_key is None:
                record.decrypt_status = "failed"
                failed += 1
                rsa_failed += 1
                continue

            # --- C SDK DecryptData ---
            ret, decrypted_str = _decrypt_message(lib, encrypt_key, encrypt_msg)
            return_codes[ret] = return_codes.get(ret, 0) + 1

            if ret != 0 or decrypted_str is None:
                record.decrypt_status = "failed"
                failed += 1
                continue

            # --- Parse decrypted JSON ---
            try:
                decrypted = json.loads(decrypted_str)
            except (json.JSONDecodeError, ValueError):
                record.decrypt_status = "failed"
                failed += 1
                continue

            # --- Normalise fields ---
            try:
                normalised = _normalise_fields(decrypted)
            except Exception:
                record.decrypt_status = "failed"
                failed += 1
                continue

            msgtype = normalised["msgtype"]

            # --- Update row (do NOT persist full decrypted_payload — SF-1) ---
            record.msgtype = normalised["msgtype"]
            record.sender = normalised["sender"]
            record.roomid = normalised["roomid"]
            record.msgtime = normalised["msgtime"]
            record.tolist = normalised["tolist"]
            record.sdkfileid = normalised["sdkfileid"]
            record.content_text = normalised["content_text"]
            record.decrypt_status = "success"

            # Upsert recipient rows — inherit tenant_id from the parent message.
            # Non-fatal by design (a recipient-persistence failure must not
            # block decrypt success), but silently swallowing it here used to
            # leave direct-conversation messages permanently unreachable with
            # no operator-visible signal (RND-178 finding). Counted below so
            # it shows up in the safe operational summary instead.
            tolist = normalised["tolist"] or []
            try:
                _upsert_recipients(session, record.id, tolist, record.tenant_id)
            except Exception:
                recipient_upsert_failed += 1

            if msgtype == "text":
                success += 1
            else:
                unsupported += 1

        # --- 5. Commit all changes ---
        try:
            session.commit()
        except Exception as exc:
            print(f"[FAIL] Database commit failed: {exc}", flush=True)
            sys.exit(1)

    # --- 6. Count remaining pending records ---
    try:
        with Session(engine) as count_session:
            pending_remaining = (
                count_session.query(ArchiveMessage)
                .filter(ArchiveMessage.decrypt_status == "pending")
                .count()
            )
    except Exception:
        pending_remaining = -1  # unable to determine

    # --- 7. Cleanup SDK ---
    try:
        wecom_sdk.destroy_sdk(lib, handle)
    except Exception:
        pass

    # --- 8. Print safe operational metrics ---
    print(f"[INFO] decrypt scanned: {scanned}", flush=True)
    print(f"[INFO] decrypt success: {success}", flush=True)
    print(f"[INFO] decrypt failed: {failed}", flush=True)
    print(f"[INFO] decrypt skipped_unsupported: {unsupported}", flush=True)
    print(f"[INFO] decrypt pending_remaining: {pending_remaining}", flush=True)
    if key_mismatch:
        print(f"[INFO] decrypt key_version_mismatch: {key_mismatch}", flush=True)
    if rsa_failed:
        print(f"[INFO] decrypt rsa_decrypt_failed: {rsa_failed}", flush=True)
    if recipient_upsert_failed:
        print(
            f"[INFO] decrypt recipient_upsert_failed: {recipient_upsert_failed}",
            flush=True,
        )
    # Safe return-code diagnostic (e.g. "ret_0=3, ret_90002=1")
    if return_codes:
        sorted_codes = sorted(return_codes.items())
        rc_diag = ", ".join(f"ret_{k}={v}" for k, v in sorted_codes)
        print(f"[INFO] decrypt return_codes: {rc_diag}", flush=True)
    print("[PASS] decrypt_wecom_messages_once completed", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()