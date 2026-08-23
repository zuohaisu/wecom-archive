from __future__ import annotations

from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent
_TEMPLATE = _BACKEND / "app/web/templates/billing.html"
_SCRIPT = _BACKEND / "app/web/static/billing.js"
_I18N = _BACKEND / "app/assets/i18n.js"


def test_billing_page_keeps_wechat_qr_and_adds_a_separate_alipay_cashier_entry() -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")
    script = _SCRIPT.read_text(encoding="utf-8")

    assert 'id="checkout-qr"' in template
    assert 'id="checkout-redirect"' in template
    assert "order.checkout_kind === 'qr_code'" in script
    assert "order.checkout_kind === 'redirect'" in script
    assert "/checkout'" in script
    assert "checkout_url" not in template
    assert "checkout_url" not in script


def test_alipay_cashier_copy_exists_in_every_supported_locale() -> None:
    translations = _I18N.read_text(encoding="utf-8")

    assert translations.count('"billing.checkout.alipay"') == 3
    assert "支付宝收银台" in translations
    assert "支付寶收銀台" in translations
    assert "Alipay cashier" in translations
