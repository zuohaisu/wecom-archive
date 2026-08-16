from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

SITE_ROOT = Path(__file__).resolve().parents[2] / "static_site/company_homepage"
HOME = SITE_ROOT / "index.html"
PRICING = SITE_ROOT / "pricing.html"


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.assets: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag in {"img", "script"} and values.get("src"):
            self.assets.append(values["src"])
        if tag == "link" and values.get("href"):
            self.assets.append(values["href"])


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _structured_data(html: str) -> list[dict]:
    return [
        json.loads(block)
        for block in re.findall(
            r'<script type="application/ld\+json">\s*(.*?)\s*</script>',
            html,
            flags=re.DOTALL,
        )
    ]


def _assert_local_references_exist(path: Path) -> None:
    parser = _LinkParser()
    parser.feed(_text(path))
    for raw in parser.links + parser.assets:
        parsed = urlsplit(raw)
        if parsed.scheme or raw.startswith(('#', 'mailto:', 'data:')):
            continue
        target_path = unquote(parsed.path)
        if not target_path:
            continue
        target = (path.parent / target_path).resolve()
        if target_path.endswith("/"):
            target = target / "index.html"
        assert SITE_ROOT in target.parents or target == SITE_ROOT
        assert target.exists(), f"broken local reference in {path.name}: {raw}"


def test_homepage_leads_with_customer_asset_protection_for_small_owner_team() -> None:
    html = _text(HOME)
    assert "给 1–5 人销售 / 客服小团队的老板" in html
    assert "客户沟通完整留痕" in html
    assert "老板随时可查" in html
    assert "员工离职，记录不跟着走" in html
    assert "企业微信会话存档与客户资产保护" in html
    assert "企业微信聊天记录查看" in html
    assert "销售聊天记录留存" in html


def test_public_pricing_is_only_the_frozen_server_plan() -> None:
    for html in (_text(HOME), _text(PRICING)):
        assert "99" in html
        assert "5 GiB" in html
        assert "不限座席" in html
        assert "企业微信官方" in html and "另计" in html
        assert "1 元/GB/月" not in html
        assert "1元/GB/月" not in html
        assert "超量单价" not in html or "没有正式发布超量单价" in html
    pricing = _text(PRICING)
    assert "当前没有正式发布超量单价或更大套餐" in pricing
    assert "不是免密自动扣款" in pricing


def test_capability_and_risk_copy_stays_inside_shipped_boundaries() -> None:
    html = _text(HOME)
    for statement in (
        "不读取或监控员工个人微信",
        "不承诺 100% 防止飞单",
        "不自动认定员工违规",
        "当前公开版本不承诺业务页面一键导出",
        "不会静默删除已有记录",
    ):
        assert statement in html
    assert "100% 防飞单" not in html
    assert "可以发现所有私下交易" not in html
    assert "法律证据绝对有效" not in html
    assert "自动识别飞单" not in html


def test_visible_faq_matches_faq_schema_and_covers_required_questions() -> None:
    html = _text(HOME)
    visible_questions = set(re.findall(r"<summary>(.*?)</summary>", html))
    faq = next(item for item in _structured_data(html) if item.get("@type") == "FAQPage")
    schema_questions = {item["name"] for item in faq["mainEntity"]}
    assert visible_questions == schema_questions
    assert {
        "老板能看到员工和客户的聊天吗？",
        "员工能不能自己删除已经归档的聊天？",
        "能看到员工个人微信吗？",
        "能 100% 防止飞单吗？",
        "员工离职后聊天记录还在吗？",
        "能导出自己的归档记录吗？",
        "为什么按空间计费而不是按员工计费？",
        "企业微信官方接口费用包含吗？",
        "图片、视频和文件会占空间吗？",
        "怎么知道系统是否正常存档？",
        "达到容量上限会自动删记录吗？",
    } <= visible_questions


TRIAL_CTA_URL = "https://archive.crowntime.cn/api/auth/wecom/third-party/install"


def test_public_pages_have_real_ctas_without_backend_or_tracking_dependencies() -> None:
    for page in (HOME, PRICING):
        html = _text(page)
        assert "mailto:hs@crowntime.cn" in html
        assert 'href="demo/"' in html
        assert "fetch(" not in html
        assert "googletag" not in html.lower()
        assert "tracking" not in html.lower()
        # RND-396: the one named, ticket-approved exception to the backend
        # isolation rule below -- a plain outbound link to the backend's
        # public, unauthenticated trial-start endpoint (see README.md). No
        # other backend reference is permitted.
        assert TRIAL_CTA_URL in html
        without_trial_cta = html.replace(TRIAL_CTA_URL, "")
        assert "/admin" not in without_trial_cta
        assert "/api/" not in without_trial_cta
        _assert_local_references_exist(page)


def test_public_pages_offer_self_service_trial_entry_with_fee_boundary() -> None:
    for page in (HOME, PRICING):
        html = _text(page)
        assert "开始 15 天免费试用" in html
        assert "企业微信官方" in html and "另计" in html
