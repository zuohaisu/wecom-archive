"""RND-360/RND-393 shared export-center frontend contract."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.auth import require_html_session
from app.main import create_app


REPO = Path(__file__).resolve().parents[2]


def test_export_center_renders_real_quota_forms_and_job_table() -> None:
    app = create_app()
    app.dependency_overrides[require_html_session] = lambda: "tenant-a"
    with TestClient(app) as client:
        response = client.get("/admin/exports")

    assert response.status_code == 200
    assert 'id="text-quota"' in response.text
    assert 'id="media-quota"' in response.text
    assert 'id="text-export-form"' in response.text
    assert 'id="media-export-form"' in response.text
    assert 'id="export-jobs"' in response.text
    assert "/web/static/exports.js?" in response.text


def test_export_messages_are_hidden_until_a_request_produces_one() -> None:
    template = (REPO / "backend/app/web/templates/exports.html").read_text()
    styles = (REPO / "backend/app/web/static/design-system.css").read_text()

    assert 'id="exports-error" class="alert alert-danger" role="alert" hidden' in template
    assert 'id="exports-success" class="alert alert-success" role="status" hidden' in template
    assert ".alert[hidden]{display:none}" in styles


def test_export_frontend_uses_server_authority_without_foreground_polling() -> None:
    script = (REPO / "backend/app/web/static/exports.js").read_text()
    assert "/api/admin/exports/quota" in script
    assert "/api/admin/exports/text" in script
    assert "/api/admin/exports/media" in script
    assert "/api/admin/exports/jobs" in script
    assert "quota.text.limit!==null&&quota.text.remaining<=0" in script
    assert "quota.media_zip.limit!==null&&quota.media_zip.remaining<=0" in script
    assert "exports.quotaTitleUnlimited" in script
    assert "exports.unlimited" in script
    assert "setInterval" not in script


def test_review_search_and_customer_pages_handoff_to_one_export_center() -> None:
    review = (REPO / "backend/app/web/templates/review_console.html").read_text()
    timeline = (REPO / "backend/app/web/static/console/timeline.js").read_text()
    search = (REPO / "backend/app/web/static/search.js").read_text()
    contacts = (REPO / "backend/app/web/templates/contacts.html").read_text()

    assert "openConversationExport()" in review
    assert "openSelectedMessageExport()" in timeline
    assert "wecom.exportPrefill" in search
    assert "'/admin/exports?participant_id='" in contacts
