"""Static browser-contract checks for RND-211 sync-aware refresh."""

from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent
_REFRESH_JS = (_BACKEND / "app/web/static/console/refresh.js").read_text()
_API_CLIENT_JS = (_BACKEND / "app/web/static/console/api-client.js").read_text()
_STATE_JS = (_BACKEND / "app/web/static/console/console-state.js").read_text()
_CONSOLE_HTML = (_BACKEND / "app/web/templates/review_console.html").read_text()
_I18N_JS = (_BACKEND / "app/assets/i18n.js").read_text()


def test_sync_controls_and_status_api_are_wired() -> None:
    assert 'id="btn-sync-now"' in _CONSOLE_HTML
    assert 'onclick="syncNow()"' in _CONSOLE_HTML
    assert 'id="sync-status"' in _CONSOLE_HTML
    assert "function fetchSyncStatus()" in _API_CLIENT_JS
    assert "/api/admin/sync-status" in _API_CLIENT_JS
    assert "function syncNow()" in _REFRESH_JS
    assert "/api/admin/sync-now" in _REFRESH_JS


def test_auto_refresh_is_sync_version_aware() -> None:
    assert "lastSeenSyncVersion" in _STATE_JS
    assert "syncInProgress" in _STATE_JS
    assert "function _recordSyncStatus(data)" in _REFRESH_JS
    assert "version!==lastSeenSyncVersion" in _REFRESH_JS
    assert "if(reason==='manual'||versionChanged)return refreshData();" in _REFRESH_JS
    assert "if(versionChanged)refreshForSyncVersion();" in _REFRESH_JS


def test_sync_i18n_keys_exist_in_all_locales() -> None:
    for key in ("sync.now", "sync.inProgress", "sync.lastSync", "sync.noData"):
        assert _I18N_JS.count(f'"{key}"') == 3
