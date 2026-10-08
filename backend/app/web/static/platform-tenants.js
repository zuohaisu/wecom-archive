/* RND-414: /platform/tenants (客户商业状态) list page. */
(function () {
  'use strict';
  var PC = window.PC;
  var state = { page: 1, pageSize: 25, total: 0 };

  function subscriptionLabel(item) {
    return (item.subscription_plan_name || '未订阅') + ' / ' + PC.dateOnly(item.subscription_ends_at);
  }
  function tenantExceptionCount(item) {
    return item.payment_exception_count + item.refund_abnormal_count + item.refund_manual_recovery_count + item.refund_closed_count;
  }
  function serviceBadgeTone(status) {
    if (status === 'active') { return 'success'; }
    if (status === 'suspended' || status === 'frozen') { return 'failed'; }
    return 'pending';
  }

  function renderTenants(data) {
    state.total = data.total;
    var body = PC.el('tenant-rows');
    PC.clear(body);
    if (!data.items.length) { PC.emptyRow(body, 8, '没有符合筛选条件的客户'); }
    data.items.forEach(function (item) {
      var name = document.createElement('td'), nameMain = document.createElement('span'), slug = document.createElement('span');
      nameMain.className = 'tenant-name'; nameMain.textContent = item.tenant_name;
      slug.className = 'tenant-slug'; slug.textContent = item.tenant_slug;
      name.appendChild(nameMain); name.appendChild(slug);

      var service = document.createElement('td');
      service.appendChild(PC.badge(item.lifecycle_status, serviceBadgeTone(item.lifecycle_status)));

      var plan = document.createElement('td'), planMain = document.createElement('span'), planEnds = document.createElement('span');
      planMain.style.display = 'block'; planMain.textContent = item.subscription_plan_name || '未订阅';
      planEnds.className = 'muted'; planEnds.style.fontSize = 'var(--text-xs)'; planEnds.textContent = PC.dateOnly(item.subscription_ends_at);
      plan.appendChild(planMain); plan.appendChild(planEnds);

      var net = document.createElement('td'); net.className = 'tnum'; net.textContent = PC.money(item.provider_net_revenue_cents);
      var manual = document.createElement('td'); manual.className = 'tnum'; manual.textContent = PC.money(item.manual_net_cents);
      var exc = document.createElement('td'); exc.textContent = PC.number(tenantExceptionCount(item)) + (item.refund_pending_count ? ' · 处理中 ' + PC.number(item.refund_pending_count) : '');
      var storage = document.createElement('td'); storage.textContent = PC.number(item.media_file_count) + ' / ' + PC.bytes(item.storage_bytes);

      var actions = document.createElement('td'), wrap = document.createElement('div');
      wrap.className = 'action-row';
      var detailLink = document.createElement('a'); detailLink.className = 'btn btn-sm'; detailLink.href = '/platform/tenants/' + encodeURIComponent(item.tenant_id); detailLink.textContent = '详情';
      wrap.appendChild(detailLink);
      wrap.appendChild(PC.actionButton(item.lifecycle_status === 'suspended' ? '恢复' : '暂停', function () {
        PC.ops.suspendResume(item, { onDone: function () { loadTenants(); } });
      }, item.lifecycle_status === 'suspended' ? '' : 'btn-danger'));
      actions.appendChild(wrap);

      var row = document.createElement('tr');
      [name, service, plan, net, manual, exc, storage, actions].forEach(function (cell) { row.appendChild(cell); });
      body.appendChild(row);
    });
    var last = Math.max(1, Math.ceil(state.total / state.pageSize));
    PC.el('tenant-pagination').textContent = '第 ' + state.page + ' / ' + last + ' 页，共 ' + PC.number(state.total) + ' 个';
    PC.el('previous-page').disabled = state.page <= 1;
    PC.el('next-page').disabled = state.page >= last;
  }

  function filterQuery() {
    var params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize), sort: PC.el('filter-sort').value });
    [['lifecycle_status', 'filter-service'], ['subscription_status', 'filter-subscription'], ['refund_status', 'filter-refund'], ['exception_type', 'filter-exception'], ['ends_from', 'filter-ends-from'], ['ends_to', 'filter-ends-to']].forEach(function (pair) {
      var value = PC.el(pair[1]).value;
      if (value && pair[0] === 'ends_from') { value += 'T00:00:00Z'; }
      if (value && pair[0] === 'ends_to') { value += 'T23:59:59Z'; }
      if (value) { params.set(pair[0], value); }
    });
    return params.toString();
  }
  function loadTenants() {
    return PC.request('/api/platform/operations/tenants?' + filterQuery()).then(renderTenants).catch(function (err) { PC.error(err.message || '加载失败'); });
  }

  PC.wireRefreshStamp(loadTenants);
  PC.el('tenant-filters').addEventListener('submit', function (event) { event.preventDefault(); state.page = 1; loadTenants(); });
  PC.el('reset-filters').addEventListener('click', function () { PC.el('tenant-filters').reset(); state.page = 1; loadTenants(); });
  PC.el('previous-page').addEventListener('click', function () { if (state.page > 1) { state.page -= 1; loadTenants(); } });
  PC.el('next-page').addEventListener('click', function () { if (state.page * state.pageSize < state.total) { state.page += 1; loadTenants(); } });
  loadTenants();
}());
