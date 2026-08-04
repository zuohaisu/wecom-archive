"""Bounded callback refresh dispatch contracts for RND-170."""

from __future__ import annotations

from collections import deque

import pytest
from app.services import external_contact_refresh_trigger as trigger


@pytest.fixture()
def isolated_queue():
    with trigger._dispatch_lock:
        original_pending = trigger._pending
        original_keys = trigger._pending_keys
        original_running = trigger._worker_running
        trigger._pending = deque()
        trigger._pending_keys = set()
        trigger._worker_running = False
    try:
        yield
    finally:
        with trigger._dispatch_lock:
            trigger._pending = original_pending
            trigger._pending_keys = original_keys
            trigger._worker_running = original_running


def test_callback_refresh_dispatch_coalesces_one_identity_into_one_worker(
    isolated_queue, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts: list[object] = []
    refreshed: list[tuple[str, str, str]] = []

    class DeferredThread:
        def __init__(self, *, target, name: str, daemon: bool) -> None:
            assert name == "wecom-external-contact-refresh"
            assert daemon is True
            self.target = target

        def start(self) -> None:
            starts.append(self.target)

    monkeypatch.setattr(trigger.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        trigger,
        "refresh_external_contact_from_callback",
        lambda *key: refreshed.append(key),
    )

    assert trigger.dispatch_external_contact_refresh("tenant-a", "corp-a", "wm-001") is (
        trigger.ExternalContactRefreshDispatch.ACCEPTED
    )
    assert trigger.dispatch_external_contact_refresh("tenant-a", "corp-a", "wm-001") is (
        trigger.ExternalContactRefreshDispatch.COALESCED
    )
    assert len(starts) == 1

    starts[0]()

    assert refreshed == [("tenant-a", "corp-a", "wm-001")]
    assert trigger._pending == deque()
    assert trigger._pending_keys == set()
    assert trigger._worker_running is False


def test_callback_refresh_dispatch_drops_overflow_for_periodic_recovery(
    isolated_queue,
) -> None:
    with trigger._dispatch_lock:
        keys = {("tenant-a", "corp-a", f"wm-{index}") for index in range(64)}
        trigger._pending = deque(sorted(keys))
        trigger._pending_keys = set(keys)
        trigger._worker_running = True

    result = trigger.dispatch_external_contact_refresh("tenant-a", "corp-a", "wm-overflow")

    assert result is trigger.ExternalContactRefreshDispatch.SKIPPED_CAPACITY
    assert len(trigger._pending) == 64
