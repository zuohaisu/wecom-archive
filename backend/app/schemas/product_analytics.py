"""Versioned, privacy-minimal contracts for product-use analytics (RND-162)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict


# Keep this catalogue aligned with https://github.com/zuohaisu/wecom-archive/wiki/Product-Analytics-Events.  It is
# intentionally closed: accepting a free-form event name would turn this
# endpoint into an unreviewed telemetry sink.
LOGIN_SUCCEEDED = "product.auth.login_succeeded.v1"
LOGIN_FAILED = "product.auth.login_failed.v1"
REVIEW_OPENED = "product.conversation.review_opened.v1"
VIEW_SELECTED = "product.directory.view_selected.v1"
SUBJECT_SELECTED = "product.directory.subject_selected.v1"
CONVERSATION_OPENED = "product.conversation.detail_opened.v1"
OLDER_MESSAGES_LOADED = "product.conversation.older_messages_loaded.v1"
SEARCH_EXECUTED = "product.search.executed.v1"
FILTER_APPLIED = "product.search.filter_applied.v1"
MEDIA_PREVIEW_OPENED = "product.media.preview_opened.v1"
SETTINGS_OPENED = "product.settings.opened.v1"
LANGUAGE_CHANGED = "product.settings.language_changed.v1"
FEEDBACK_SUBMITTED = "product.feedback.submitted.v1"

ALL_EVENT_NAMES = frozenset(
    {
        LOGIN_SUCCEEDED,
        LOGIN_FAILED,
        REVIEW_OPENED,
        VIEW_SELECTED,
        SUBJECT_SELECTED,
        CONVERSATION_OPENED,
        OLDER_MESSAGES_LOADED,
        SEARCH_EXECUTED,
        FILTER_APPLIED,
        MEDIA_PREVIEW_OPENED,
        SETTINGS_OPENED,
        LANGUAGE_CHANGED,
        FEEDBACK_SUBMITTED,
    }
)
FRONTEND_EVENT_NAMES = frozenset(
    {
        REVIEW_OPENED,
        VIEW_SELECTED,
        SUBJECT_SELECTED,
        CONVERSATION_OPENED,
        OLDER_MESSAGES_LOADED,
        SEARCH_EXECUTED,
        FILTER_APPLIED,
        MEDIA_PREVIEW_OPENED,
        SETTINGS_OPENED,
    }
)
CORE_WORKFLOW_EVENT_NAMES = frozenset(
    {
        REVIEW_OPENED,
        VIEW_SELECTED,
        SUBJECT_SELECTED,
        CONVERSATION_OPENED,
        OLDER_MESSAGES_LOADED,
        SEARCH_EXECUTED,
        FILTER_APPLIED,
        MEDIA_PREVIEW_OPENED,
    }
)

_EVENT_CLASS_BY_NAME = {
    LOGIN_SUCCEEDED: "authentication",
    LOGIN_FAILED: "authentication",
    REVIEW_OPENED: "core_workflow",
    VIEW_SELECTED: "core_workflow",
    SUBJECT_SELECTED: "core_workflow",
    CONVERSATION_OPENED: "core_workflow",
    OLDER_MESSAGES_LOADED: "core_workflow",
    SEARCH_EXECUTED: "core_workflow",
    FILTER_APPLIED: "core_workflow",
    MEDIA_PREVIEW_OPENED: "core_workflow",
    SETTINGS_OPENED: "secondary_workflow",
    LANGUAGE_CHANGED: "secondary_workflow",
    FEEDBACK_SUBMITTED: "secondary_workflow",
}


def event_class(event_name: str) -> str:
    return _EVENT_CLASS_BY_NAME[event_name]


class ProductAnalyticsEventIn(BaseModel):
    """One allowlisted browser action, without identity or content fields.

    Tenant and actor identity are derived by the server from the authenticated
    session.  Event-specific attributes are top-level so Pydantic rejects
    arbitrary nested telemetry payloads.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_name: Literal[
        "product.conversation.review_opened.v1",
        "product.directory.view_selected.v1",
        "product.directory.subject_selected.v1",
        "product.conversation.detail_opened.v1",
        "product.conversation.older_messages_loaded.v1",
        "product.search.executed.v1",
        "product.search.filter_applied.v1",
        "product.media.preview_opened.v1",
        "product.settings.opened.v1",
    ]
    occurred_at: datetime
    view_kind: Optional[Literal["employee", "contact"]] = None
    subject_kind: Optional[Literal["employee", "contact"]] = None
    search_scope: Optional[Literal["contact_nickname", "message_content", "combined"]] = None
    filter_dimension: Optional[Literal["date", "person", "message_type"]] = None
    filter_action: Optional[Literal["applied", "cleared"]] = None

    def attributes(self) -> dict[str, str]:
        values = {
            "view_kind": self.view_kind,
            "subject_kind": self.subject_kind,
            "search_scope": self.search_scope,
            "filter_dimension": self.filter_dimension,
            "filter_action": self.filter_action,
        }
        return {key: value for key, value in values.items() if value is not None}


class ProductAnalyticsEventAcceptedOut(BaseModel):
    accepted: bool = True


class ProductAnalyticsFunctionAdoptionOut(BaseModel):
    event_name: str
    tenant_count: int
    admin_count: int
    event_count: int


class ProductAnalyticsLoginOut(BaseModel):
    success_count: int
    failure_count: int
    failure_rate: Optional[float]


class ProductAnalyticsDailyTrendOut(BaseModel):
    date: date
    login_success_count: int
    login_failure_count: int
    active_tenant_count: int
    event_counts: dict[str, int]


class ProductAnalyticsOverviewOut(BaseModel):
    starts_at: datetime
    ends_at: datetime
    event_name: Optional[str]
    tenant_id: Optional[str]
    provisioned_tenant_count: int
    active_tenant_count: int
    inactive_tenant_count: int
    active_admin_count: int
    login: ProductAnalyticsLoginOut
    function_adoption: list[ProductAnalyticsFunctionAdoptionOut]
    trends: list[ProductAnalyticsDailyTrendOut]


class ProductAnalyticsTenantOut(BaseModel):
    tenant_id: str
    tenant_name: str
    tenant_slug: str
    lifecycle_status: str
    last_login_at: Optional[datetime]
    last_product_event_at: Optional[datetime]
    active_admin_count: int
    event_count: int
    activity_status: Literal["active", "inactive"]


class ProductAnalyticsTenantListOut(BaseModel):
    items: list[ProductAnalyticsTenantOut]
    page: int
    page_size: int
    total: int


class ProductAnalyticsTenantDetailOut(ProductAnalyticsTenantOut):
    starts_at: datetime
    ends_at: datetime
    login: ProductAnalyticsLoginOut
    function_adoption: list[ProductAnalyticsFunctionAdoptionOut]
    trends: list[ProductAnalyticsDailyTrendOut]
