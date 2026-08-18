"""RND-404 Owner billing page: frozen/suspended banner, cancel-at-period-end
toggle, and refund status DOM/JS wiring (extends the RND-395 billing UI)."""

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


def test_billing_page_adds_lifecycle_and_refund_blocks_without_reordering_cards() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    # RND-395's DOM-order invariant must still hold with the new sections
    # inserted.
    assert source.index('class="card subscription-hero"') < source.index('class="billing-grid"')
    assert source.index('class="billing-grid"') < source.index('class="card capacity-card"')
    assert source.index('class="billing-grid"') < source.index('id="refund-card"')
    assert source.index('id="refund-card"') < source.index('class="card capacity-card"')

    assert 'id="tenant-status-banner"' in source
    assert 'id="suspended-notice"' in source
    assert 'id="cancel-intent"' in source
    assert 'id="cancel-intent-toggle"' in source
    assert 'id="refund-status"' in source
    assert 'data-billing-is-owner="__BILLING_IS_OWNER__"' in source


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


def test_suspended_and_write_endpoints_are_hidden_client_side_when_suspended() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")

    assert "subscription.tenant_lifecycle_status === 'suspended'" in script
    assert "refresh.hidden = !hasLiveOrder || !canOrder || suspended" in script
    assert "close.hidden = !(order && (order.status === 'creating' || order.status === 'pending')) || !canOrder || suspended" in script
    assert "var isOwner = document.body.dataset.billingIsOwner === 'true'" in script
    assert "/api/billing/subscription/cancel-intent" in script
    assert "/api/billing/refunds/latest" in script


def test_cancel_intent_eligibility_requires_owner_and_a_renewable_subscription_status() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    script = _SCRIPT.read_text(encoding="utf-8")
    function = _extract_function(script, "renderCancelIntent")
    assert "cancelableSubscriptionStatus" in function
    assert "isOwner" in function
    assert "tenant_lifecycle_status !== 'suspended'" in function


def test_i18n_has_frozen_suspended_cancel_intent_and_refund_keys_in_all_three_locales() -> None:
    translations = _I18N.read_text(encoding="utf-8")
    keys = [
        "billing.tenantStatus.frozen",
        "billing.tenantStatus.suspended",
        "billing.cta.suspended",
        "billing.next.suspended",
        "billing.cancelIntent.set",
        "billing.cancelIntent.restore",
        "billing.refund.status.processing",
        "billing.refund.status.succeeded",
        "billing.refund.status.abnormal",
        "billing.refund.status.manual_recovery_required",
    ]
    for key in keys:
        assert translations.count(f'"{key}"') == 3, f"{key} must appear in all three locale blocks"
