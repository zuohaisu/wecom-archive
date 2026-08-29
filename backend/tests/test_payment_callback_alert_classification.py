"""Regression coverage for GitHub #113 callback alert classification."""

from __future__ import annotations

import pytest

from app.services.payment_recovery import (
    FINDING_CALLBACK_DECRYPT_FAILURE,
    FINDING_CALLBACK_SIGNATURE_FAILURE,
    record_callback_failure,
)


def test_signature_failure_does_not_open_payment_recovery_session() -> None:
    """An untrusted public request has no trusted order identity to recover."""

    def unexpected_session_factory():
        raise AssertionError("signature rejection must not persist a recovery finding")

    record_callback_failure(
        unexpected_session_factory,
        kind=FINDING_CALLBACK_SIGNATURE_FAILURE,
    )


def test_decrypt_failure_remains_actionable() -> None:
    """A decrypt failure occurs after signature verification and stays actionable."""

    class SessionFactoryCalled(RuntimeError):
        pass

    def sentinel_session_factory():
        raise SessionFactoryCalled

    with pytest.raises(SessionFactoryCalled):
        record_callback_failure(
            sentinel_session_factory,
            kind=FINDING_CALLBACK_DECRYPT_FAILURE,
        )
