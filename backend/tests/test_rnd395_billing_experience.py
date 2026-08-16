"""RND-395 Owner trial/renewal information hierarchy and CTA contract."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node


_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app/web/templates/billing.html"
_SCRIPT = _BACKEND / "app/web/static/billing.js"
_STYLES = _BACKEND / "app/web/static/billing.css"
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


def test_billing_page_uses_static_assets_and_state_first_dom_order() -> None:
    source = _TEMPLATE.read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/web/static/billing.css?v=__STATIC_VERSION__">' in source
    assert "<style" not in source
    assert "<script>" not in source
    assert 'data-billing-mode="__BILLING_MODE__"' in source
    assert source.index('class="card subscription-hero"') < source.index('class="billing-grid"')
    assert source.index('class="billing-grid"') < source.index('class="card capacity-card"')
    assert 'id="trial-remaining"' in source
    assert 'id="complete-setup"' in source
    assert 'id="create-order"' in source
    assert 'id="checkout-qr"' in source


def test_navigation_and_provisioning_copy_do_not_present_payment_as_first_step() -> None:
    translations = _I18N.read_text(encoding="utf-8")
    # The trial-first provisioning navigation and copy moved out of the
    # routers into the shared sidenav module and the i18n asset when the
    # RND-383 wizard/status-page rework merged; the payment-not-first policy
    # is enforced at those sources of truth.
    sidenav_module = (_BACKEND / "app/web/sidenav.py").read_text(encoding="utf-8")

    for entry in ('"nav.billing": "续费"', '"nav.billing": "續費"', '"nav.billing": "Renewal"'):
        assert entry in translations
    assert "开始 15 天免费试用" in sidenav_module
    assert "无需先付款" in translations
    assert "配置和自动检查" in translations


def test_cta_policy_is_unique_for_subscription_and_pending_order_states() -> None:
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    function = _extract_function(_SCRIPT.read_text(encoding="utf-8"), "billingCta")
    cases = [
        ["unavailable", "no_subscription", "provisioning", None, True],
        ["trial", None, "admin", None, True],
        ["paid_active", None, "admin", "succeeded", True],
        ["expiring_soon", None, "admin", None, True],
        ["expired", "subscription_expired", "admin", None, True],
        ["trial", None, "admin", "pending", True],
        ["paid_active", None, "admin", "paid_activation_pending", True],
        ["trial", None, "admin", None, False],
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
    assert [(item["kind"], item["labelKey"]) for item in policies] == [
        ("setup", "billing.cta.completeSetup"),
        ("payment", "billing.cta.renewTrial"),
        ("payment", "billing.cta.renew"),
        ("payment", "billing.cta.renewSoon"),
        ("payment", "billing.cta.restore"),
        ("order", "billing.cta.orderPending"),
        ("order", "billing.cta.orderPending"),
        ("unavailable", "billing.cta.renewTrial"),
    ]


def test_qr_and_duplicate_order_guards_use_provider_and_expiry_truth() -> None:
    script = _SCRIPT.read_text(encoding="utf-8")
    styles = _STYLES.read_text(encoding="utf-8")

    assert "order.status === 'pending'" in script
    assert "order.qr_available" in script
    assert "new Date(order.expires_at) > new Date()" in script
    assert "activeOrder[order && order.status]" in script
    assert "create.hidden = policy.kind !== 'payment'" in script
    assert "refresh.hidden = !hasLiveOrder" in script
    assert "plan.payment_enabled" in script
    assert "unavailable.hidden = plan.payment_enabled || policy.kind === 'setup'" in script
    assert "checkout_url" not in script
    assert "@media (max-width: 1024px)" in styles
    assert "@media (max-width: 850px)" in styles
    assert "@media (max-width: 640px)" in styles
