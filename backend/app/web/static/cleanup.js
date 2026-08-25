// RND-370: bulk message cleanup page — filter, preview, async task, status.
(function () {
  'use strict';

  var preview = null; // last /cleanup/preview response
  var pollTimer = null;
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
  function fmtBytes(n) {
    if (n == null) { return '—'; }
    var units = ['B', 'KB', 'MB', 'GB'];
    var value = Number(n), unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return (unit ? value.toFixed(value >= 10 ? 0 : 1) : String(value)) + ' ' + units[unit];
  }
  function setStatus(message, error) {
    var el = node('cleanup-status');
    el.textContent = message || '';
    el.className = error ? 'field-error' : 'field-help';
  }

  function filterParams() {
    var params = {};
    var preset = node('cleanup-preset').value;
    var dateFrom = node('cleanup-date-from').value;
    var dateTo = node('cleanup-date-to').value;
    if (preset) { params.older_than_days = Number(preset); }
    if (dateFrom) { params.date_from_ms = new Date(dateFrom + 'T00:00:00+08:00').getTime(); }
    if (dateTo) { params.date_to_ms = new Date(dateTo + 'T23:59:59+08:00').getTime(); }
    var room = node('cleanup-room').value.trim();
    var staff = node('cleanup-staff').value.trim();
    var contact = node('cleanup-contact').value.trim();
    if (room) { params.roomid = room; }
    if (staff) { params.staff_id = staff; }
    if (contact) { params.contact_id = contact; }
    var kind = node('cleanup-kind').value;
    if (kind && kind !== 'all') { params.conversation_kind = kind; }
    var msgtype = node('cleanup-msgtype').value;
    if (msgtype) { params.msgtypes = [msgtype]; }
    var media = node('cleanup-media').value;
    if (media === 'yes') { params.has_media = true; }
    if (media === 'no') { params.has_media = false; }
    var min = Number(node('cleanup-media-min').value);
    var max = Number(node('cleanup-media-max').value);
    if (node('cleanup-media-min').value) { params.media_min_bytes = min; }
    if (node('cleanup-media-max').value) { params.media_max_bytes = max; }
    params.include_favorited = node('cleanup-include-favorited').checked;
    return params;
  }

  function request(url, options) {
    return fetch(url, Object.assign({ credentials: 'include' }, options || {})).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || 'request_failed');
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }

  function runPreview() {
    setStatus('');
    busy = true;
    request('/api/admin/messages/cleanup/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filter: filterParams() })
    }).then(function (data) {
      preview = data;
      renderPreview(data);
    }).catch(function (error) {
      preview = null;
      node('cleanup-preview-card').hidden = true;
      setStatus(error.message === 'deletion_locked' ? t('delete.locked') : t('cleanup.previewFailed'), true);
    }).finally(function () { busy = false; });
  }

  function stat(labelKey, value) {
    return '<div class="preview-stat"><span>' + esc(t(labelKey)) + '</span><b>' + esc(value) + '</b></div>';
  }

  function renderPreview(data) {
    node('cleanup-preview-card').hidden = false;
    node('cleanup-preview-summary').textContent = data.summary;
    var parts = [];
    parts.push(stat('cleanup.statMatched', String(data.matched)));
    parts.push(stat('cleanup.statConversations', String(data.conversation_count)));
    parts.push(stat('cleanup.statMedia', fmtBytes(data.media_bytes)));
    parts.push(stat('cleanup.statReleasable', fmtBytes(data.releasable_bytes)));
    parts.push(stat('cleanup.statShared', fmtBytes(data.shared_media_bytes)));
    parts.push(stat('cleanup.statTextEstimate', fmtBytes(data.text_bytes_estimate)));
    if (data.latest_msgtime != null) {
      parts.push(stat('cleanup.statRange', fmt(data.earliest_msgtime) + ' ~ ' + fmt(data.latest_msgtime)));
    }
    node('cleanup-preview-stats').innerHTML = parts.join('');
    var note = [];
    note.push(t('cleanup.previewNote', {
      moved: fmtBytes(data.media_bytes),
      releasable: fmtBytes(data.releasable_bytes)
    }));
    if (data.favorited_count > 0) {
      note.push(t('cleanup.favoritedWarning', { n: String(data.favorited_count) }));
    }
    node('cleanup-preview-note').textContent = note.join(' ');
    node('cleanup-confirm').disabled = data.matched < 1 || busy;
  }

  function openConfirm() {
    if (!preview || preview.matched < 1 || busy) { return; }
    node('confirm-summary').textContent = preview.summary + ' — ' + t('cleanup.confirmCount', { n: String(preview.matched) });
    node('confirm-phrase').value = '';
    node('confirm-ok').disabled = true;
    node('confirm-modal').hidden = false;
    node('confirm-phrase').focus();
  }

  function closeConfirm() { node('confirm-modal').hidden = true; }

  function createTask() {
    if (!preview || busy) { return; }
    if (node('confirm-phrase').value.trim() !== '确认清理') { return; }
    busy = true;
    closeConfirm();
    request('/api/admin/messages/cleanup/tasks', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        filter: filterParams(),
        preview_version: preview.preview_version,
        confirmation: '确认清理'
      })
    }).then(function (task) {
      setStatus(t('cleanup.taskCreated'));
      preview = null;
      node('cleanup-preview-card').hidden = true;
      loadTasks();
    }).catch(function (error) {
      setStatus(error.status === 409 ? t('cleanup.previewStale') : t('cleanup.taskFailed'), true);
    }).finally(function () { busy = false; });
  }

  function taskStatusLabel(status) {
    return t('cleanup.taskStatus.' + status);
  }

  function renderTasks(data) {
    var container = node('cleanup-tasks');
    if (!data.items.length) {
      container.innerHTML = '<div class="muted" data-i18n="cleanup.tasksEmpty">暂无任务</div>';
      return;
    }
    container.innerHTML = data.items.map(function (task) {
      var progress = task.total_matched
        ? Math.min(100, Math.round((task.succeeded + task.skipped) * 100 / task.total_matched)) + '%'
        : '—';
      var actions = '';
      if (task.status === 'queued' || task.status === 'running') {
        actions += '<button type="button" class="btn btn-sm" data-cancel-task="' + esc(task.id) + '" data-i18n="cleanup.cancelTask">取消</button>';
      }
      if (task.status === 'completed' || task.status === 'partial') {
        actions += '<a class="btn btn-sm" href="/admin/recycle-bin?batch=' + esc(task.id) + '" data-i18n="cleanup.viewBatch">回收站查看本批次</a>';
      }
      return '<div class="task-row" data-task-id="' + esc(task.id) + '">'
        + '<span class="badge task-status badge-' + (task.status === 'completed' ? 'success' : task.status === 'failed' ? 'danger' : task.status === 'canceled' ? 'neutral' : 'info') + '">' + esc(taskStatusLabel(task.status)) + '</span>'
        + '<div class="task-meta"><div>' + esc(task.filter_summary) + '</div>'
        + '<div class="muted">' + t('cleanup.taskProgress', { done: String(task.succeeded), total: String(task.total_matched), skipped: String(task.skipped) }) + ' · ' + progress + ' · ' + t('cleanup.taskMoved', { value: fmtBytes(task.moved_bytes) }) + ' · ' + t('cleanup.taskCreatedAt', { time: fmt(task.created_at) }) + '</div>'
        + (task.error_message ? '<div class="field-error">' + esc(task.error_message) + '</div>' : '')
        + '</div><div class="task-actions">' + actions + '</div></div>';
    }).join('');
    applyStaticI18n();
  }

  function loadTasks() {
    request('/api/admin/messages/cleanup/tasks?limit=50').then(function (data) {
      renderTasks(data);
      var anyActive = data.items.some(function (task) {
        return task.status === 'queued' || task.status === 'running';
      });
      schedulePoll(anyActive);
    }).catch(function () {});
  }

  function schedulePoll(active) {
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    if (!active) { return; }
    pollTimer = setTimeout(function () {
      if (document.visibilityState === 'visible') { loadTasks(); } else { schedulePoll(true); }
    }, 5000);
  }

  function cancelTask(taskId) {
    request('/api/admin/messages/cleanup/tasks/' + encodeURIComponent(taskId) + '/cancel', { method: 'POST' })
      .then(function () { setStatus(t('cleanup.taskCanceled')); loadTasks(); })
      .catch(function () { setStatus(t('cleanup.taskFailed'), true); });
  }

  function applyStaticI18n() {
    document.querySelectorAll('[data-i18n]').forEach(function (el) {
      el.textContent = t(el.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (el) {
      el.placeholder = t(el.getAttribute('data-i18n-placeholder'));
    });
    document.querySelectorAll('[data-i18n-label]').forEach(function (el) {
      el.setAttribute('aria-label', t(el.getAttribute('data-i18n-label')));
    });
    document.title = t('cleanup.pageTitle');
  }

  function wire() {
    node('cleanup-preview').addEventListener('click', runPreview);
    node('cleanup-confirm').addEventListener('click', openConfirm);
    node('confirm-cancel').addEventListener('click', closeConfirm);
    node('confirm-phrase').addEventListener('input', function (event) {
      node('confirm-ok').disabled = event.target.value.trim() !== '确认清理';
    });
    node('confirm-ok').addEventListener('click', createTask);
    node('cleanup-tasks').addEventListener('click', function (event) {
      var button = event.target.closest('[data-cancel-task]');
      if (button) { cancelTask(button.dataset.cancelTask); }
    });
    ['cleanup-preset', 'cleanup-date-from', 'cleanup-date-to', 'cleanup-kind', 'cleanup-msgtype', 'cleanup-media'].forEach(function (id) {
      node(id).addEventListener('change', function () { preview = null; node('cleanup-preview-card').hidden = true; });
    });
  }

  window.doLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).finally(function () {
      window.location = '/admin/login';
    });
  };

  wire();
  applyStaticI18n();
  loadTasks();
}());
