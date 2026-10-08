/* /platform/finance/orders — super-admin 订单明细 page.
 * Client-side rendering over the platform-admin read API; PC.* helpers
 * from platform-console.js; createElement/textContent only (no innerHTML,
 * matching the other platform pages' XSS discipline). All timestamps are
 * rendered UTC+8 via PC.date. Empty states render "—" (never 0). */
(function () {
  'use strict';
  var PC = window.PC;

  var STATUS_TEXT = {
    creating: '创建中',
    pending: '待支付',
    paid_activation_pending: '已支付 · 待激活',
    succeeded: '已完成',
    closed: '已关闭',
    failed: '失败',
    created: '已创建',
    processing: '处理中',
    abnormal: '异常',
    manual_recovery_required: '需人工恢复'
  };
  var STATUS_TONE = {
    succeeded: 'success',
    failed: 'failed',
    abnormal: 'failed',
    manual_recovery_required: 'failed',
    closed: 'silent'
  };
  var RECEIPT_STATUS_OPTIONS = ['creating', 'pending', 'paid_activation_pending', 'succeeded', 'closed', 'failed'];
  var REFUND_STATUS_OPTIONS = ['created', 'processing', 'succeeded', 'closed', 'abnormal', 'manual_recovery_required'];

  var state = { page: 1, totalPages: 1, pageSize: 25 };

  function showError(error) {
    var box = PC.el('error');
    box.textContent = error && error.message ? error.message : '加载失败';
    box.hidden = false;
  }
  function clearError() { PC.el('error').hidden = true; }

  function statusOptions(kind) {
    if (kind === 'receipt') { return RECEIPT_STATUS_OPTIONS; }
    if (kind === 'refund') { return REFUND_STATUS_OPTIONS; }
    return RECEIPT_STATUS_OPTIONS.concat(REFUND_STATUS_OPTIONS.filter(function (s) { return RECEIPT_STATUS_OPTIONS.indexOf(s) < 0; }));
  }
  function rebuildStatusOptions() {
    var kind = PC.el('fin-kind').value;
    var select = PC.el('fin-status');
    var previous = select.value;
    PC.clear(select);
    var all = document.createElement('option');
    all.value = ''; all.textContent = '全部状态';
    select.appendChild(all);
    statusOptions(kind).forEach(function (value) {
      var option = document.createElement('option');
      option.value = value;
      option.textContent = STATUS_TEXT[value] || value;
      select.appendChild(option);
    });
    if (previous && statusOptions(kind).indexOf(previous) >= 0) { select.value = previous; }
  }

  function kindBadge(kind) {
    return PC.badge(kind === 'receipt' ? '入账' : '退款', kind === 'refund' ? 'info' : '');
  }
  function statusBadge(status) {
    return PC.badge(STATUS_TEXT[status] || status, STATUS_TONE[status] || 'pending');
  }

  function renderSummary(summary) {
    PC.el('fin-receipt').textContent = PC.money(summary.receipt_cents);
    PC.el('fin-receipt-count').textContent = PC.number(summary.receipt_count) + ' 笔渠道确认';
    PC.el('fin-refund').textContent = PC.money(summary.refund_cents);
    PC.el('fin-refund-count').textContent = PC.number(summary.refund_count) + ' 笔退款成功';
    PC.el('fin-net').textContent = PC.money(summary.net_cents);
  }

  function renderRows(items) {
    var body = PC.el('fin-rows');
    PC.clear(body);
    if (!items.length) {
      PC.emptyRow(body, 7, '当前筛选范围内没有订单。');
      return;
    }
    items.forEach(function (item) {
      var row = document.createElement('tr');
      var kindCell = document.createElement('td');
      kindCell.appendChild(kindBadge(item.kind));
      var tenantCell = document.createElement('td');
      tenantCell.textContent = item.tenant_name || item.tenant_id;
      var detailCell = document.createElement('td');
      detailCell.textContent = item.kind === 'receipt' ? (item.plan_name || '—') : (item.reason_code || '—');
      var amountCell = document.createElement('td');
      amountCell.className = 'num';
      amountCell.textContent = PC.money(item.amount_cents);
      var statusCell = document.createElement('td');
      statusCell.appendChild(statusBadge(item.status));
      var providerCell = document.createElement('td');
      providerCell.textContent = item.provider;
      var timeCell = document.createElement('td');
      timeCell.textContent = PC.date(item.occurred_at);
      [kindCell, tenantCell, detailCell, amountCell, statusCell, providerCell, timeCell].forEach(function (cell) {
        row.appendChild(cell);
      });
      body.appendChild(row);
    });
  }

  function renderPagination(data) {
    state.totalPages = Math.max(1, Math.ceil(data.total / state.pageSize));
    PC.el('fin-total').textContent = '共 ' + PC.number(data.total) + ' 条';
    PC.el('fin-page-info').textContent = '第 ' + data.page + ' / ' + state.totalPages + ' 页';
    PC.el('fin-page-prev').disabled = data.page <= 1;
    PC.el('fin-page-next').disabled = data.page >= state.totalPages;
  }

  function load() {
    clearError();
    var params = new URLSearchParams({
      kind: PC.el('fin-kind').value,
      page: String(state.page),
      page_size: String(state.pageSize)
    });
    var status = PC.el('fin-status').value;
    if (status) { params.set('status', status); }
    var q = PC.el('fin-q').value.trim();
    if (q) { params.set('q', q); }
    PC.request('/api/platform/finance/orders?' + params.toString()).then(function (data) {
      renderSummary(data.summary);
      renderRows(data.items);
      renderPagination(data);
      PC.el('fin-refreshed').textContent = '刷新于 ' + PC.date(new Date().toISOString());
    }).catch(showError);
  }

  PC.el('fin-kind').addEventListener('change', function () {
    rebuildStatusOptions();
    state.page = 1;
  });
  PC.el('fin-filters').addEventListener('submit', function (event) {
    event.preventDefault();
    state.page = 1;
    load();
  });
  PC.el('fin-page-prev').addEventListener('click', function () {
    if (state.page > 1) { state.page -= 1; load(); }
  });
  PC.el('fin-page-next').addEventListener('click', function () {
    if (state.page < state.totalPages) { state.page += 1; load(); }
  });

  rebuildStatusOptions();
  load();
}());
