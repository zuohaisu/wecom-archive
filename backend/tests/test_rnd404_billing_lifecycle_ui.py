"""Owner billing page: lifecycle status and refund-status DOM/JS wiring."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app/web/templates/billing.html"
_SCRIPT = _BACKEND / "app/web/static/billing.js"
_I18N = _BACKEND / "app/assets/i18n.js"


def _extract_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(brace, len(source)):
        char = source[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_billing_page_keeps_lifecycle_and_refund_blocks_without_reordering_cards() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    # RND-395's DOM-order invariant must still hold with the new sections
    # inserted.
    assert source.index('class="card subscription-hero"') < source.index('class="billing-grid"')
    assert source.index('class="billing-grid"') < source.index('class="card capacity-card"')
    assert source.index('class="billing-grid"') < source.index('id="refund-card"')
    assert source.index('id="refund-card"') < source.index('class="card capacity-card"')

    assert 'id="tenant-status-banner"' in source
    assert 'id="suspended-notice"' in source
    assert 'id="refund-status"' in source
    assert 'cancel-intent' not in source
    assert 'data-billing-is-owner' not in source


def test_cta_policy_treats_suspended_as_the_single_highest_priority_state() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    function = _extract_function(_SCRIPT.read_text(encoding="utf-8"), "billingCta")
    cases = [
        # Suspended wins even with a pending order and payment enabled.
        ["paid_active", None, "admin", "pending", True, "suspended"],
        ["expired", "subscription_expired", "admin", None, True, "suspended"],
        # Frozen tenants keep the ordinary payment/restore CTA — paying is
        # exactly how they unfreeze (ADR-0005 §2.6).
        ["expired", "subscription_expired", "admin", None, True, "frozen"],
        # A 6th arg of undefined (RND-395's original 5-arg calls) must not
        # change behavior.
        ["paid_active", None, "admin", "succeeded", True, None],
    ]
    harness = f"""
var activeOrder = {{ creating: true, pending: true, paid_activation_pending: true }};
{function}
var cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(function(args) {{ return billingCta.apply(null, args); }})));
"""
    result = run_node(harness)

    assert result.returncode == 0, result.stderr
    policies = json.loads(result.stdout)
    assert [item["kind"] for item in policies] == ["suspended", "suspended", "payment", "payment"]
    assert policies[0]["labelKey"] == "billing.cta.suspended"


def test_suspended_payment_writes_are_hidden_client_side_and_cancel_intent_code_is_absent() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")

    assert "subscription.tenant_lifecycle_status === 'suspended'" in script
    assert "refresh.hidden = !hasLiveOrder || !canOrder || suspended" in script
    assert "close.hidden = !(order && (order.status === 'creating' || order.status === 'pending')) || !canOrder || suspended" in script
    assert "/api/billing/refunds/latest" in script
    assert "cancel-intent" not in script
    assert "cancelIntent" not in script
    assert "billingIsOwner" not in script


def test_i18n_keeps_lifecycle_and_refund_keys_but_removes_cancel_intent_keys() -> None:
    translations = _I18N.read_text(encoding="utf-8")
    retained_keys = [
        "billing.tenantStatus.frozen",
        "billing.tenantStatus.suspended",
        "billing.cta.suspended",
        "billing.next.suspended",
        "billing.refund.status.processing",
        "billing.refund.status.succeeded",
        "billing.refund.status.abnormal",
        "billing.refund.status.manual_recovery_required",
    ]
    for key in retained_keys:
        assert translations.count(f'"{key}"') == 3, f"{key} must appear in all three locale blocks"

    removed_keys = [
        "billing.cancelIntent.active",
        "billing.cancelIntent.inactive",
        "billing.cancelIntent.set",
        "billing.cancelIntent.restore",
        "billing.error.cancelIntent",
    ]
    for key in removed_keys:
        assert f'"{key}"' not in translations
