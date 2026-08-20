/* RND-414: /platform/usage. Backed by the real GET /api/platform/tenants/usage
 * (TenantUsageListOut) — no pagination, no quota_bytes, no server-side sort
 * (Spec §6 gap "用量列表的配额与分页"). Sort + summary totals are computed
 * client-side over the one full response; no quota percentage is shown
 * since there is no quota_bytes to compute it against. */
(function () {
  'use strict';
  var PC = window.PC;
  var items = [];
  var sortKey = 'storage_bytes';

  var SYNC_BADGE = { healthy: 'badge-success', delayed: 'badge-pending', failed: 'badge-failed', paused: 'badge-plain' };
  function syncBadge(health) {
    var status = (health && health.status) || 'unknown';
    var span = document.createElement('span');
    span.className = 'badge ' + (SYNC_BADGE[status] || 'badge-plain');
    span.textContent = status;
    return span;
  }

  function renderSummary() {
    var messageTotal = 0, storageTotal = 0, employeeTotal = 0, syncIssues = 0;
    items.forEach(function (item) {
      messageTotal += Number(item.message_count || 0);
      storageTotal += Number(item.storage_bytes || 0);
      employeeTotal += Number(item.employee_count || 0);
      if (!item.sync_health || item.sync_health.status !== 'healthy') { syncIssues += 1; }
    });
    PC.el('usage-messages').textContent = PC.number(messageTotal);
    PC.el('usage-storage').textContent = PC.bytes(storageTotal);
    PC.el('usage-employees').textContent = PC.number(employeeTotal);
    PC.el('usage-sync-issues').textContent = PC.number(syncIssues);
    PC.el('usage-count').textContent = '共 ' + PC.number(items.length) + ' 个租户';
  }

  function sortedItems() {
    var copy = items.slice();
    if (sortKey === 'sync_health') {
      copy.sort(function (a, b) {
        var sa = (a.sync_health && a.sync_health.status) || '', sb = (b.sync_health && b.sync_health.status) || '';
        return sa.localeCompare(sb);
      });
      return copy;
    }
    copy.sort(function (a, b) { return Number(b[sortKey] || 0) - Number(a[sortKey] || 0); });
    return copy;
  }

  function renderRows() {
    var body = PC.el('usage-rows');
    PC.clear(body);
    var sorted = sortedItems();
    if (!sorted.length) { PC.emptyRow(body, 7, '暂无租户用量数据'); return; }
    sorted.forEach(function (item) {
      var messageCell = document.createElement('td'); messageCell.className = 'tnum'; messageCell.textContent = PC.number(item.message_count);
      var storageCell = document.createElement('td'); storageCell.className = 'tnum'; storageCell.textContent = PC.bytes(item.storage_bytes);
      var employeeCell = document.createElement('td'); employeeCell.className = 'tnum'; employeeCell.textContent = PC.number(item.employee_count);
      var syncCell = document.createElement('td'); syncCell.appendChild(syncBadge(item.sync_health));
      var lastSyncCell = document.createElement('td'); lastSyncCell.className = 'muted'; lastSyncCell.textContent = PC.date(item.sync_health && item.sync_health.updated_at);
      var actionsCell = document.createElement('td');
      actionsCell.appendChild(PC.actionButton('详情', function () { window.location.href = '/platform/tenants/' + encodeURIComponent(item.tenant_id); }));
      var nameCell = document.createElement('td');
      var nameMain = document.createElement('span'); nameMain.className = 'tenant-name'; nameMain.textContent = item.tenant_name;
      nameCell.appendChild(nameMain);
      var row = document.createElement('tr');
      [nameCell, messageCell, storageCell, employeeCell, syncCell, lastSyncCell, actionsCell].forEach(function (cell) { row.appendChild(cell); });
      body.appendChild(row);
    });
  }

  function wireSort() {
    var group = PC.el('usage-sort');
    group.querySelectorAll('button').forEach(function (button) {
      button.addEventListener('click', function () {
        group.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        button.classList.add('active');
        sortKey = button.dataset.sort;
        renderRows();
      });
    });
  }

  function refresh() {
    PC.error('');
    return PC.request('/api/platform/tenants/usage').then(function (data) {
      items = data.tenants || [];
      renderSummary();
      renderRows();
    }).catch(function (err) { PC.error(err.message || '用量数据加载失败'); });
  }

  wireSort();
  PC.wireRefreshStamp(refresh);
  refresh();
}());
