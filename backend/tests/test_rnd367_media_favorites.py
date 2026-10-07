"""RND-367 media favorite-selection state and API contract tests."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests._node_runner import run_node

_BACKEND = Path(__file__).resolve().parent.parent
_FAVORITES_JS = _BACKEND / "app" / "web" / "static" / "media-favorites.js"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not available in this environment")


def _run(script: str) -> str:
    result = run_node(
        "var MediaFavorites = require(%s);\n%s" % (json.dumps(str(_FAVORITES_JS)), script)
    )
    assert result.returncode == 0, "node harness failed: %s" % result.stderr
    return result.stdout


def test_read_only_role_cannot_mutate_and_mixed_batch_result_updates_state() -> None:
    output = _run(
        """
(async function () {
  var calls = [];
  var favorites = MediaFavorites.create({request: function (url, options) {
    calls.push({url: url, body: JSON.parse(options.body)});
    if (url === '/api/favorites/status') return Promise.resolve({items: [
      {object_type: 'media', object_id: '11', result: 'found', is_favorited: true},
      {object_type: 'media', object_id: '22', result: 'found', is_favorited: false}
    ]});
    return Promise.resolve({requested: 2, unique: 2, applied: 1, unchanged: 1, not_found: 0, items: [
      {object_type: 'media', object_id: '11', result: 'already_favorited'},
      {object_type: 'media', object_id: '22', result: 'favorited'}
    ]});
  }});
  await favorites.setItems([{id: 11}, {id: 22}]);
  favorites.setRole('readonlyaudit');
  favorites.selectAllLoaded();
  try { await favorites.apply('favorite'); throw new Error('readonly mutation unexpectedly passed'); }
  catch (error) { if (error.message !== 'favorite_action_unavailable') throw error; }
  if (calls.length !== 1) throw new Error('readonly role reached mutation API');
  favorites.setRole('admin');
  var result = await favorites.apply('favorite');
  if (result.applied !== 1 || result.unchanged !== 1 || result.not_found !== 0) throw new Error('mixed result was not aggregated');
  if (favorites.getStatus(11).isFavorited !== true || favorites.getStatus(22).isFavorited !== true) throw new Error('favorite state was not updated');
  if (favorites.getSnapshot().selectedCount !== 0) throw new Error('successful operation did not clear selection');
  var body = calls[1].body;
  if (body.action !== 'favorite' || body.items.some(function (item) { return item.source_page !== 'media' || item.object_type !== 'media'; })) throw new Error('unified API payload was not canonical');
  console.log('readonly=blocked mixed=1/1 statuses=updated selection=cleared');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "readonly=blocked mixed=1/1 statuses=updated selection=cleared"


def test_loaded_results_are_chunked_at_api_limit_and_partial_failure_is_retryable() -> None:
    output = _run(
        """
(async function () {
  var calls = [];
  var batchAttempt = 0;
  var favorites = MediaFavorites.create({request: function (url, options) {
    var body = JSON.parse(options.body);
    calls.push({url: url, body: body});
    if (url === '/api/favorites/status') return Promise.resolve({items: body.items.map(function (item) {
      return {object_type: 'media', object_id: item.object_id, result: 'found', is_favorited: false};
    })});
    batchAttempt += 1;
    if (batchAttempt === 2) return Promise.reject(new Error('synthetic network failure'));
    var result = body.items.map(function (item) {
      return {object_type: 'media', object_id: item.object_id, result: 'favorited'};
    });
    return Promise.resolve({requested: result.length, unique: result.length, applied: result.length, unchanged: 0, not_found: 0, items: result});
  }});
  await favorites.setItems(Array.from({length: 101}, function (_, index) { return {id: index + 1}; }));
  favorites.setRole('owner');
  favorites.selectAllLoaded();
  try { await favorites.apply('favorite'); throw new Error('partial batch unexpectedly passed'); }
  catch (error) {
    if (!error.favoriteSummary || error.favoriteSummary.applied !== 100) throw error;
  }
  if (favorites.getSnapshot().selectedCount !== 101 || !favorites.getSnapshot().canApply) throw new Error('partial failure lost retryable selection');
  if (favorites.getStatus(1).isFavorited !== true || favorites.getStatus(101).isFavorited !== false) throw new Error('partial response state is incorrect');
  var statusCalls = calls.filter(function (call) { return call.url === '/api/favorites/status'; });
  var batchCalls = calls.filter(function (call) { return call.url === '/api/favorites/batch'; });
  if (statusCalls.length !== 2 || statusCalls[0].body.items.length !== 100 || statusCalls[1].body.items.length !== 1) throw new Error('status request exceeded limit');
  if (batchCalls.length !== 2 || batchCalls[0].body.items.length !== 100 || batchCalls[1].body.items.length !== 1) throw new Error('batch request exceeded limit');
  console.log('status=100+1 batch=100+1 partial=retryable');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "status=100+1 batch=100+1 partial=retryable"


def test_successful_write_reconciles_visible_status_after_items_change() -> None:
    output = _run(
        """
(async function () {
  var serverFavorited = false;
  var resolveBatch;
  var statusCalls = 0;
  var favorites = MediaFavorites.create({request: function (url, options) {
    var body = JSON.parse(options.body);
    if (url === '/api/favorites/status') {
      statusCalls += 1;
      return Promise.resolve({items: body.items.map(function (item) {
        return {object_type: 'media', object_id: item.object_id, result: 'found', is_favorited: item.object_id === '1' && serverFavorited};
      })});
    }
    return new Promise(function (resolve) { resolveBatch = resolve; });
  }});
  await favorites.setItems([{id: 1}]);
  favorites.setRole('owner');
  favorites.selectAllLoaded();
  var action = favorites.apply('favorite');
  await new Promise(function (resolve) { setTimeout(resolve, 0); });
  await favorites.setItems([{id: 1}, {id: 2}]);
  if (favorites.getStatus(1).isFavorited !== false) throw new Error('fixture did not capture the pre-write status');
  serverFavorited = true;
  resolveBatch({requested: 1, unique: 1, applied: 1, unchanged: 0, not_found: 0, items: [
    {object_type: 'media', object_id: '1', result: 'favorited'}
  ]});
  await action;
  if (favorites.getStatus(1).isFavorited !== true) throw new Error('successful write left the visible item stale');
  if (favorites.getStatus(2).isFavorited !== false) throw new Error('new item status was not preserved during reconciliation');
  if (statusCalls !== 3) throw new Error('stale revision did not trigger one post-write status query');
  console.log('revision-change=visible-status-reconciled');
}()).catch(function (error) { console.error(error); process.exitCode = 1; });
"""
    )
    assert output.strip() == "revision-change=visible-status-reconciled"
