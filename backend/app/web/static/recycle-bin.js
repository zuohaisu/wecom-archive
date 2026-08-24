// RND-364: tenant recycle-bin page — list/filter, restore, permanent purge.
(function () {
  'use strict';

  var state = { offset: 0, limit: 50, total: 0, items: [] };
  var selection = {};
  var busy = false;

  function t(key, values) {
    var value = I18N.t(key);
    Object.keys(values || {}).forEach(function (name) {
      value = value.replace('{' + name + '}', String(values[name]));
    });
    return value;
  }
  function esc(value) {
    return String(value == null ? '' : value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
  function node(id) { return document.getElementById(id); }
  function fmt(value) {
    if (!value) { return '—'; }
    var date = new Date(value);
    return isNaN(date.getTime()) ? '—' : date.toLocaleString(I18N.getLocale());
  }
  function typeLabel(msgtype) {
    var key = 'dashboard.type.' + (msgtype || 'other');
    var label = I18N.t(key);
    return label === key ? esc(msgtype || '—') : esc(label);
  }

  function setStatus(message, error) {
    var el = node('recycle-status');
    el.textContent = message || '';
    el.className = error ? 'field-error' : 'field-help';
  }

  function renderMetrics(metrics) {
    var el = node('recycle-metrics');
    if (!metrics) { el.textContent = ''; return; }
    var parts = [];
    parts.push(t('recycle.metricPending') + ': ' + metrics.pending_purge_count);
    if (metrics.oldest_pending_age_days != null) {
      parts.push(t('recycle.metricOldest') + ': ' + metrics.oldest_pending_age_days.toFixed(1) + t('recycle.days'));
    }
    parts.push(t('recycle.metricStorageRetry') + ': ' + metrics.storage_retry_pending);
    parts.push(t('recycle.metricLastSuccess') + ': ' + fmt(metrics.last_success_at));
    if (metrics.recent_failure_count) {
      parts.push(t('recycle.metricFailures') + ': ' + metrics.recent_failure_count);
    }
    el.textContent = parts.join(' · ');
  }

  function remainingDays(item) {
    var days = Math.ceil((new Date(item.purge_after) - new Date()) / 86400000);
    return days >= 0 ? t('recycle.remainingDays', { days: String(days) }) : t('recycle.overdueDays', { days: String(-days) });
  }

  function row(item, index) {
    var checked = selection[item.msgid] ? ' checked' : '';
    return '<tr data-msgid="' + esc(item.msgid) + '">'
      + '<td><input class="recycle-check" type="checkbox" aria-label="' + esc(item.msgid) + '"' + checked + ' data-index="' + index + '"></td>'
      + '<td class="mono">' + esc(item.msgid) + '</td>'
      + '<td>' + typeLabel(item.msgtype) + '</td>'
      + '<td class="recycle-preview">' + esc(item.preview || '—') + '</td>'
      + '<td>' + (item.has_media ? '<span class="badge badge-count" data-i18n="recycle.hasMedia">有媒体</span>' : '<span class="muted">—</span>') + '</td>'
      + '<td>' + esc(fmt(item.deleted_at)) + '</td>'
      + '<td class="mono">' + esc(item.deleted_by_admin_user_id || '—') + '</td>'
      + '<td>' + esc(fmt(item.purge_after)) + '</td>'
      + '<td>' + esc(remainingDays(item)) + '</td>'
      + '</tr>';
  }

  function render() {
    var body = node('recycle-body');
    if (!state.items.length) {
      body.innerHTML = '<tr><td colspan="9" class="empty">' + esc(t('recycle.empty')) + '</td></tr>';
    } else {
      body.innerHTML = state.items.map(row).join('');
    }
    node('recycle-count').textContent = t('recycle.results', { n: String(state.total) });
    node('recycle-more').classList.toggle('hidden', state.offset >= state.total);
    applyStaticI18n();
    updateSelectionUi();
  }

  function filters() {
    var p = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
    var type = node('recycle-type').value;
    var room = node('recycle-room').value.trim();
    var deletedBy = node('recycle-deleted-by').value.trim();
    if (type) { p.set('msgtype', type); }
    if (room) { p.set('roomid', room); }
    if (deletedBy) { p.set('deleted_by', deletedBy); }
    return p;
  }

  function load() {
    setStatus('');
    fetch('/api/admin/messages/recycle-bin?' + filters().toString(), { credentials: 'include' })
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.json();
      })
      .then(function (data) {
        state.items = data.items || [];
        state.total = data.total || 0;
        state.offset = state.items.length;
        render();
      })
      .catch(function () { setStatus(t('recycle.loadFailed'), true); });
    fetch('/api/admin/messages/recycle-bin/metrics', { credentials: 'include' })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (metrics) { if (metrics) { renderMetrics(metrics); } })
      .catch(function () {});
  }

  function reset() {
    state.offset = 0;
    state.items = [];
    selection = {};
    load();
  }

  function countSelection() {
    var n = 0;
    Object.keys(selection).forEach(function (k) { if (selection[k]) { n += 1; } });
    return n;
  }

  function selectedIds() {
    return Object.keys(selection).filter(function (k) { return selection[k]; }).slice(0, 200);
  }

  function updateSelectionUi() {
    var count = countSelection();
    node('recycle-restore').disabled = count < 1 || busy;
    node('recycle-purge').disabled = count < 1 || busy;
    node('recycle-selected').textContent = count ? t('recycle.selected', { n: String(count) }) : '';
    var all = state.items.length && state.items.every(function (item) { return selection[item.msgid]; });
    node('recycle-select-all').checked = Boolean(all);
  }

  function applyStaticI18n() {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
    document.title = t('recycle.pageTitle');
  }

  function restoreSelected() {
    var ids = selectedIds();
    if (!ids.length || busy) { return; }
    busy = true;
    updateSelectionUi();
    fetch('/api/admin/messages/restore', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_ids: ids })
    })
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.json();
      })
      .then(function (result) {
        setStatus(t('recycle.restoreDone', { n: String(result.deleted) }));
        reset();
      })
      .catch(function () { setStatus(t('recycle.restoreFailed'), true); })
      .finally(function () { busy = false; updateSelectionUi(); });
  }

  function openPurgeModal() {
    var ids = selectedIds();
    if (!ids.length) { return; }
    node('purge-count').textContent = t('recycle.purgeCount', { n: String(ids.length) });
    node('purge-confirm-check').checked = false;
    node('purge-ok').disabled = true;
    node('purge-modal').hidden = false;
  }

  function closePurgeModal() {
    node('purge-modal').hidden = true;
  }

  function purgeSelected() {
    var ids = selectedIds();
    if (!ids.length || busy) { return; }
    if (!node('purge-confirm-check').checked) { return; }
    busy = true;
    closePurgeModal();
    updateSelectionUi();
    fetch('/api/admin/messages/purge', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_ids: ids, confirm: true })
    })
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.json();
      })
      .then(function (result) {
        var message = t('recycle.purgeDone', { n: String(result.purged) });
        if (result.media_retry_pending) {
          message += ' ' + t('recycle.purgeMediaPending', { n: String(result.media_retry_pending) });
        }
        setStatus(message);
        reset();
      })
      .catch(function () { setStatus(t('recycle.purgeFailed'), true); })
      .finally(function () { busy = false; updateSelectionUi(); });
  }

  function wire() {
    node('recycle-filter-apply').addEventListener('click', reset);
    node('recycle-filter-reset').addEventListener('click', function () {
      node('recycle-type').value = '';
      node('recycle-room').value = '';
      node('recycle-deleted-by').value = '';
      reset();
    });
    node('recycle-select-all').addEventListener('change', function (event) {
      var checked = event.target.checked;
      state.items.forEach(function (item) {
        if (checked) { selection[item.msgid] = true; } else { delete selection[item.msgid]; }
      });
      updateSelectionUi();
      state.items.forEach(function (item, index) {
        var box = document.querySelector('[data-msgid="' + item.msgid + '"] .recycle-check');
        if (box) { box.checked = Boolean(selection[item.msgid]); }
      });
    });
    node('recycle-body').addEventListener('change', function (event) {
      var box = event.target.closest('.recycle-check');
      if (!box) { return; }
      var item = state.items[Number(box.dataset.index)];
      if (!item) { return; }
      if (box.checked) { selection[item.msgid] = true; } else { delete selection[item.msgid]; }
      updateSelectionUi();
    });
    node('recycle-restore').addEventListener('click', restoreSelected);
    node('recycle-purge').addEventListener('click', openPurgeModal);
    node('recycle-more').addEventListener('click', function () {
      state.offset = state.items.length;
      fetch('/api/admin/messages/recycle-bin?' + filters().toString(), { credentials: 'include' })
        .then(function (response) { return response.json(); })
        .then(function (data) {
          state.items = state.items.concat(data.items || []);
          state.total = data.total || 0;
          render();
        })
        .catch(function () { setStatus(t('recycle.loadFailed'), true); });
    });
    node('purge-cancel').addEventListener('click', closePurgeModal);
    node('purge-ok').addEventListener('click', purgeSelected);
    node('purge-confirm-check').addEventListener('change', function (event) {
      node('purge-ok').disabled = !event.target.checked;
    });
    ['recycle-type', 'recycle-room', 'recycle-deleted-by'].forEach(function (id) {
      node(id).addEventListener('keydown', function (event) {
        if (event.key === 'Enter') { reset(); }
      });
    });
  }

  window.doLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).finally(function () {
      window.location = '/admin/login';
    });
  };

  wire();
  applyStaticI18n();
  load();
}());
