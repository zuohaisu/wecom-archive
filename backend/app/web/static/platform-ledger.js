/* RND-414: /platform/ledger (cross-tenant manual ledger). The cross-tenant
 * list endpoint (GET /api/platform/operations/financial-transactions) does
 * NOT exist yet (Spec §6 gap "跨租户账本列表") — call it per the assumed
 * contract and degrade the table to a gap row on failure. Entry CREATION
 * still works: it posts to the real per-tenant endpoint. The summary metric
 * cards have no real cross-tenant aggregate source either, so they render
 * as gap placeholders rather than a fabricated total. */
(function () {
  'use strict';
  var PC = window.PC;
  var state = { page: 1, pageSize: 25, total: 0 };
  var tenantOptions = [];

  function filterQuery() {
    var params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize) });
    var kind = PC.el('filter-kind').value, stateFilter = PC.el('filter-state').value;
    var from = PC.el('filter-from').value, to = PC.el('filter-to').value;
    if (kind) { params.set('kind', kind); }
    if (stateFilter) { params.set('state', stateFilter); }
    if (from) { params.set('occurred_from', from + 'T00:00:00+08:00'); }
    if (to) { params.set('occurred_to', to + 'T23:59:59+08:00'); }
    return params.toString();
  }

  function kindBadge(kind) {
    var span = document.createElement('span');
    span.className = 'badge ' + (kind === 'receipt' ? 'badge-success' : 'badge-failed');
    span.textContent = kind;
    return span;
  }
  function stateBadge(entryState) {
    var span = document.createElement('span');
    span.className = 'badge ' + (entryState === 'voided' ? 'badge-plain' : 'badge-info');
    span.textContent = entryState;
    if (entryState === 'voided') { span.style.textDecoration = 'line-through'; }
    return span;
  }

  function reload() {
    return loadSummary().then(loadEntries);
  }

  function renderGapSummary() {
    ['ledger-receipts', 'ledger-refunds', 'ledger-net', 'ledger-provider-net'].forEach(function (id) {
      PC.el(id).textContent = '—';
    });
    PC.el('ledger-receipts').title = '跨租户汇总依赖的接口尚未交付。';
  }
  function loadSummary() {
    // No cross-tenant financial-summary endpoint exists yet either — this
    // shares the same gap as the entry list below.
    renderGapSummary();
    return Promise.resolve();
  }

  function renderRows(data) {
    var body = PC.el('ledger-rows');
    PC.clear(body);
    var items = data.items || [];
    if (!items.length) { PC.emptyRow(body, 9, '没有符合筛选条件的账本条目'); return; }
    items.forEach(function (entry) {
      var tenantCell = document.createElement('td');
      var nameMain = document.createElement('span'); nameMain.className = 'tenant-name'; nameMain.textContent = entry.tenant_name || entry.tenant_id;
      var slug = document.createElement('span'); slug.className = 'tenant-slug'; slug.textContent = entry.tenant_slug || '';
      tenantCell.appendChild(nameMain); tenantCell.appendChild(slug);

      var kindCell = document.createElement('td'); kindCell.appendChild(kindBadge(entry.kind));
      var amountCell = document.createElement('td'); amountCell.className = 'tnum'; amountCell.textContent = PC.money(entry.amount_cents);
      var stateCell = document.createElement('td'); stateCell.appendChild(stateBadge(entry.state || 'posted'));
      var actionsCell = document.createElement('td');
      var actionRow = document.createElement('div'); actionRow.className = 'action-row';
      var voided = (entry.state || 'posted') === 'voided';
      if (!voided) {
        actionRow.appendChild(PC.actionButton('编辑', function () {
          PC.ops.ledgerEntry({ mode: 'edit', tenant: { tenant_id: entry.tenant_id, tenant_name: entry.tenant_name }, entry: entry }, { onDone: reload });
        }));
        actionRow.appendChild(PC.actionButton('作废', function () {
          PC.ops.voidLedgerEntry(entry, { tenant_id: entry.tenant_id, tenant_name: entry.tenant_name }, { onDone: reload });
        }, 'btn-danger'));
      }
      actionsCell.appendChild(actionRow);

      var timeCell = document.createElement('td'); timeCell.className = 'muted'; timeCell.textContent = PC.date(entry.occurred_at);
      var refCell = document.createElement('td'); refCell.className = 'mono'; refCell.textContent = entry.reference_masked || '—';
      var noteCell = document.createElement('td'); noteCell.className = 'muted'; noteCell.textContent = entry.note || '—';
      var operatorCell = document.createElement('td'); operatorCell.className = 'muted'; operatorCell.textContent = entry.recorded_by || '—';

      var row = document.createElement('tr');
      [timeCell, tenantCell, kindCell, amountCell, refCell, noteCell, stateCell, operatorCell, actionsCell].forEach(function (cell) { row.appendChild(cell); });
      body.appendChild(row);
    });
  }

  function loadEntries() {
    PC.el('ledger-pagination').textContent = '';
    return PC.request('/api/platform/operations/financial-transactions?' + filterQuery()).then(function (data) {
      
      state.total = data.total || (data.items || []).length;
      renderRows(data);
      var last = Math.max(1, Math.ceil(state.total / state.pageSize));
      PC.el('ledger-pagination').textContent = '第 ' + state.page + ' / ' + last + ' 页，共 ' + PC.number(state.total) + ' 条';
      PC.el('previous-page').disabled = state.page <= 1;
      PC.el('next-page').disabled = state.page >= last;
    }).catch(function () {
      
      PC.gapRow(PC.el('ledger-rows'), 9, '跨租户账本列表依赖的接口尚未交付，暂无数据可展示。可通过「新增条目」在租户详情页登记，或在租户详情页的「手工账本」页签查看单个租户的条目。');
      PC.el('previous-page').disabled = true;
      PC.el('next-page').disabled = true;
    });
  }

  function loadTenantOptions() {
    return PC.request('/api/platform/operations/tenants?page_size=100').then(function (data) {
      tenantOptions = (data.items || []).map(function (item) { return { tenant_id: item.tenant_id, tenant_name: item.tenant_name }; });
    }).catch(function () { tenantOptions = []; });
  }

  PC.el('ledger-create').addEventListener('click', function () {
    PC.ops.ledgerEntry({ mode: 'create', tenant: null, tenantOptions: tenantOptions }, { onDone: reload });
  });
  PC.el('ledger-filters').addEventListener('submit', function (event) { event.preventDefault(); state.page = 1; loadEntries().catch(function (err) { PC.error(err.message); }); });
  PC.el('reset-filters').addEventListener('click', function () { PC.el('ledger-filters').reset(); state.page = 1; loadEntries().catch(function (err) { PC.error(err.message); }); });
  PC.el('previous-page').addEventListener('click', function () { if (state.page > 1) { state.page -= 1; loadEntries().catch(function (err) { PC.error(err.message); }); } });
  PC.el('next-page').addEventListener('click', function () { if (state.page * state.pageSize < state.total) { state.page += 1; loadEntries().catch(function (err) { PC.error(err.message); }); } });

  PC.wireRefreshStamp(reload);
  loadTenantOptions();
  reload().catch(function (err) { PC.error(err.message || '加载失败'); });
}());
