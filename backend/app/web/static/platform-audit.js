/* RND-414: /platform/audit — cross-tenant audit trail. No backend endpoint
 * exists for this (the only real GET /audit-logs is hard-scoped to the
 * caller's own tenant-admin session — unusable here). This calls the
 * assumed GET /api/platform/operations/audit-events?action=&operator=&
 * tenant_id=&from=&to=&page=&page_size= contract and gap-degrades the
 * table on failure rather than fabricating rows. No innerHTML. */
(function () {
  'use strict';
  var PC = window.PC;
  var state = { page: 1, pageSize: 25, total: 0 };

  function filterQuery() {
    var params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    var action = PC.el('filter-action').value;
    var operator = PC.el('filter-operator').value.trim();
    var tenant = PC.el('filter-tenant').value.trim();
    var from = PC.el('filter-from').value;
    var to = PC.el('filter-to').value;
    if (action) { params.set('action', action); }
    if (operator) { params.set('operator', operator); }
    if (tenant) { params.set('tenant_id', tenant); }
    if (from) { params.set('from', from + 'T00:00:00Z'); }
    if (to) { params.set('to', to + 'T23:59:59Z'); }
    return params.toString();
  }

  function renderRows(data) {
    state.total = data.total || 0;
    var body = PC.el('audit-rows');
    PC.clear(body);
    if (!data.items || !data.items.length) { PC.emptyRow(body, 8, '没有符合筛选条件的审计事件'); }
    (data.items || []).forEach(function (item) {
      var idCell = document.createElement('td'); idCell.className = 'cell-id'; idCell.textContent = item.audit_id;
      var keyCell = document.createElement('td'); keyCell.className = 'cell-id'; keyCell.textContent = item.idempotency_key || '—';
      body.appendChild(PC.rowFrom([
        PC.date(item.created_at), idCell, item.action,
        item.tenant_name || item.tenant_id || '—',
        (item.object_type || '—') + (item.object_id ? ' · ' + item.object_id : ''),
        item.reason_code || '—', keyCell, item.operator || '—'
      ]));
    });
    var last = Math.max(1, Math.ceil(state.total / state.pageSize));
    PC.el('audit-pagination').textContent = '第 ' + state.page + ' / ' + last + ' 页，共 ' + PC.number(state.total) + ' 条';
    PC.el('previous-page').disabled = state.page <= 1;
    PC.el('next-page').disabled = state.page >= last;
  }

  function loadAudit() {
    PC.error('');
    return PC.request('/api/platform/operations/audit-events?' + filterQuery()).then(renderRows).catch(function () {
      state.total = 0;
      PC.gapRow(PC.el('audit-rows'), 8, '全局审计依赖的跨客户接口尚未交付，暂无数据可展示。客户详情页的「审计」页签仍可查看单客户最近事件。');
      PC.el('audit-pagination').textContent = '';
      PC.el('previous-page').disabled = true;
      PC.el('next-page').disabled = true;
    });
  }

  PC.el('audit-filters').addEventListener('submit', function (event) { event.preventDefault(); state.page = 1; loadAudit(); });
  PC.el('reset-filters').addEventListener('click', function () { PC.el('audit-filters').reset(); state.page = 1; loadAudit(); });
  PC.el('previous-page').addEventListener('click', function () { if (state.page > 1) { state.page -= 1; loadAudit(); } });
  PC.el('next-page').addEventListener('click', function () { if (state.page * state.pageSize < state.total) { state.page += 1; loadAudit(); } });
  PC.wireRefreshStamp(loadAudit);
  loadAudit();
}());
