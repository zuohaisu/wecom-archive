(function () {
  'use strict';

  var state = { page: 1, pageSize: 25, total: 0 };

  function el(id) { return document.getElementById(id); }
  function clear(node) { node.textContent = ''; }
  function number(value) { return new Intl.NumberFormat('zh-CN').format(value || 0); }
  function money(value) { return '¥' + (Number(value || 0) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function date(value) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'; }
  function bytes(value) {
    var amount = Number(value || 0), units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'], index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return (index ? amount.toFixed(amount >= 10 ? 1 : 2) : amount) + ' ' + units[index];
  }
  function error(message) {
    var box = el('error');
    box.hidden = !message;
    box.textContent = message || '';
  }
  function request(url, options) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, options || {})).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.detail || '请求失败');
        });
      }
      return response.json();
    });
  }
  function empty(node, label) {
    clear(node);
    var item = document.createElement('li');
    item.className = 'empty';
    item.textContent = label;
    node.appendChild(item);
  }
  function list(node, items, render, label) {
    clear(node);
    if (!items || !items.length) { empty(node, label); return; }
    items.forEach(function (item) { node.appendChild(render(item)); });
  }
  function textPair(primary, secondary) {
    var li = document.createElement('li'), left = document.createElement('span'), right = document.createElement('small');
    left.textContent = primary;
    right.textContent = secondary;
    li.appendChild(left);
    li.appendChild(right);
    return li;
  }
  function actionButton(label, handler, tone) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm' + (tone ? ' ' + tone : '');
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }
  function operationKey(prefix) {
    var suffix = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now()) + '-' + String(Math.random()).slice(2);
    return prefix + '-' + suffix;
  }
  function controlProof(item, label, defaultReason) {
    if (!window.confirm('确认执行“' + label + '”：' + item.tenant_name + '？')) { return null; }
    var reason = window.prompt('请输入原因代码（小写字母开头，可含数字、点、下划线或连字符）', defaultReason);
    if (reason === null) { return null; }
    reason = reason.trim().toLowerCase();
    if (!/^[a-z][a-z0-9._-]{0,63}$/.test(reason)) { error('原因代码格式不正确'); return null; }
    var confirmation = window.prompt('高风险操作确认：请输入租户标识 ' + item.tenant_slug);
    if (confirmation !== item.tenant_slug) { error('租户确认不匹配，操作已取消'); return null; }
    return { reason_code: reason, confirmation: confirmation };
  }
  function controlledRequest(url, item, label, defaultReason, prefix, extra) {
    var proof = controlProof(item, label, defaultReason);
    if (!proof) { return Promise.resolve(null); }
    return request(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': operationKey(prefix)
      },
      body: JSON.stringify(Object.assign(proof, extra || {}))
    });
  }

  function renderDashboard(data) {
    var counts = data.tenant_counts, accounts = data.account_counts, usage = data.usage_totals;
    var revenue = data.revenue, manual = data.manual_financial, exceptions = data.exceptions;
    var exceptionTotal = exceptions.payment_activation_pending + exceptions.payment_failed + exceptions.refund_abnormal + exceptions.refund_closed + exceptions.refund_manual_recovery;
    el('tenant-total').textContent = number(counts.total);
    el('tenant-states').textContent = '试用 ' + number(counts.trial) + ' · 有效 ' + number(counts.active) + ' · 宽限 ' + number(counts.grace) + ' · 冻结 ' + number(counts.frozen) + ' · 暂停 ' + number(counts.suspended);
    el('admin-accounts').textContent = number(accounts.admin_accounts);
    el('observed-users').textContent = '活跃管理员 ' + number(accounts.active_admin_accounts) + ' · 已观测用户 ' + number(accounts.observed_users);
    el('message-total').textContent = number(usage.message_count);
    el('storage-total').textContent = bytes(usage.storage_bytes);
    el('media-total').textContent = number(usage.media_file_count) + ' 个媒体文件';
    el('receipt-total').textContent = money(revenue.receipt_cents);
    el('refund-total').textContent = money(revenue.refund_cents);
    el('net-revenue').textContent = '渠道净收入 ' + money(revenue.net_revenue_cents);
    el('manual-financial').textContent = '手工收 / 退 ' + money(manual.receipt_cents) + ' / ' + money(manual.refund_cents);
    el('exception-total').textContent = number(exceptionTotal);
    el('exception-states').textContent = '支付/激活 ' + number(exceptions.payment_activation_pending + exceptions.payment_failed) + ' · 退款 ' + number(exceptions.refund_abnormal + exceptions.refund_closed + exceptions.refund_manual_recovery) + ' · 退款处理中 ' + number(exceptions.refund_pending);
    list(el('subscriptions'), data.subscription_distribution, function (item) { return textPair((item.plan_name || '未订阅') + ' · ' + item.status, number(item.tenant_count) + ' 个租户'); }, '暂无订阅数据');
    list(el('quota-risks'), data.storage_quota_risks, function (item) {
      var li = textPair(item.tenant_name, bytes(item.used_bytes) + ' / ' + bytes(item.quota_bytes) + ' · ' + (item.utilization_basis_points / 100).toFixed(2) + '%');
      li.className = item.state === 'over_quota' || item.state === 'at_quota' ? 'quota-danger' : 'quota-warning';
      return li;
    }, '暂无接近配额的租户');
    list(el('recent-tenants'), data.recent_tenants, function (item) { return textPair(item.tenant_name, date(item.occurred_at)); }, '暂无租户');
    list(el('recent-active'), data.recently_active_tenants, function (item) { return textPair(item.tenant_name, date(item.occurred_at)); }, '暂无活跃记录');
    var periods = el('revenue-periods');
    clear(periods);
    revenue.periods.forEach(function (item) {
      var row = document.createElement('tr');
      [item.period, money(item.receipt_cents), money(item.refund_cents), money(item.net_revenue_cents)].forEach(function (value) {
        var cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
      });
      periods.appendChild(row);
    });
  }

  function subscriptionLabel(item) {
    return (item.subscription_plan_name || '未订阅') + ' · ' + item.subscription_status + ' · ' + date(item.subscription_ends_at);
  }
  function tenantExceptionCount(item) {
    return item.payment_exception_count + item.refund_abnormal_count + item.refund_manual_recovery_count + item.refund_closed_count;
  }
  function renderTenants(data) {
    state.total = data.total;
    var body = el('tenant-rows');
    clear(body);
    if (!data.items.length) {
      var emptyRow = document.createElement('tr'), emptyCell = document.createElement('td');
      emptyCell.colSpan = 7; emptyCell.className = 'muted'; emptyCell.textContent = '没有符合筛选条件的租户'; emptyRow.appendChild(emptyCell); body.appendChild(emptyRow);
    }
    data.items.forEach(function (item) {
      var row = document.createElement('tr'), cells = [];
      var name = document.createElement('td'), nameMain = document.createElement('span'), slug = document.createElement('span');
      nameMain.className = 'tenant-name'; nameMain.textContent = item.tenant_name;
      slug.className = 'tenant-slug'; slug.textContent = item.tenant_slug;
      name.appendChild(nameMain); name.appendChild(slug); cells.push(name);
      [item.lifecycle_status, subscriptionLabel(item), money(item.provider_net_revenue_cents), number(tenantExceptionCount(item)) + ' · 退款处理中 ' + number(item.refund_pending_count), number(item.media_file_count) + ' / ' + bytes(item.storage_bytes)].forEach(function (value) {
        var cell = document.createElement('td'); cell.textContent = value; cells.push(cell);
      });
      var actions = document.createElement('td'), wrap = document.createElement('div');
      wrap.className = 'action-row';
      wrap.appendChild(actionButton('详情', function () { loadDetail(item.tenant_id); }));
      wrap.appendChild(actionButton(item.lifecycle_status === 'suspended' ? '恢复' : '暂停', function () { changeService(item); }, item.lifecycle_status === 'suspended' ? '' : 'btn-danger'));
      actions.appendChild(wrap); cells.push(actions);
      cells.forEach(function (cell) { row.appendChild(cell); }); body.appendChild(row);
    });
    var last = Math.max(1, Math.ceil(state.total / state.pageSize));
    el('tenant-pagination').textContent = '第 ' + state.page + ' / ' + last + ' 页，共 ' + number(state.total) + ' 个';
    el('previous-page').disabled = state.page <= 1;
    el('next-page').disabled = state.page >= last;
  }

  function filterQuery() {
    var params = new URLSearchParams({ page: String(state.page), page_size: String(state.pageSize), sort: el('filter-sort').value });
    [['lifecycle_status', 'filter-service'], ['subscription_status', 'filter-subscription'], ['refund_status', 'filter-refund'], ['exception_type', 'filter-exception'], ['ends_from', 'filter-ends-from'], ['ends_to', 'filter-ends-to']].forEach(function (pair) {
      var value = el(pair[1]).value;
      if (value && pair[0] === 'ends_from') { value += 'T00:00:00Z'; }
      if (value && pair[0] === 'ends_to') { value += 'T23:59:59Z'; }
      if (value) { params.set(pair[0], value); }
    });
    return params.toString();
  }
  function loadDashboard() { return request('/api/platform/operations/dashboard').then(renderDashboard); }
  function loadTenants() { return request('/api/platform/operations/tenants?' + filterQuery()).then(renderTenants); }
  function refresh() { error(''); return Promise.all([loadDashboard(), loadTenants()]).catch(function (err) { error(err.message || '加载失败'); }); }

  function changeService(item) {
    var next = item.lifecycle_status === 'suspended' ? 'active' : 'suspended';
    var label = next === 'suspended' ? '人工暂停服务' : '恢复服务';
    var proof = controlProof(item, label, next === 'suspended' ? 'risk_review' : 'risk_cleared');
    if (!proof) { return; }
    request('/api/platform/operations/tenants/' + encodeURIComponent(item.tenant_id) + '/service', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': operationKey('service-control') },
      body: JSON.stringify({ lifecycle_status: next, reason_code: proof.reason_code, confirmation: proof.confirmation })
    }).then(refresh).then(function () { return loadDetail(item.tenant_id); }).catch(function (err) { error(err.message || '操作失败'); });
  }
  function queryPayment(item, order) {
    controlledRequest('/api/platform/operations/tenants/' + encodeURIComponent(item.tenant_id) + '/payments/' + encodeURIComponent(order.order_id) + '/query', item, '查询并恢复支付/激活', 'activation_recovery', 'payment-query').then(function (result) {
      if (result) { return refresh().then(function () { return loadDetail(item.tenant_id); }); }
      return null;
    }).catch(function (err) { error(err.message || '支付恢复失败'); });
  }
  function submitRefund(item, order) {
    controlledRequest('/api/platform/operations/tenants/' + encodeURIComponent(item.tenant_id) + '/refunds', item, '提交已批准的全额退款 ' + money(order.amount_cents), 'customer_request', 'refund-submit', { payment_order_id: order.order_id }).then(function (result) {
      if (result) { return refresh().then(function () { return loadDetail(item.tenant_id); }); }
      return null;
    }).catch(function (err) { error(err.message || '退款提交失败'); });
  }
  function queryRefund(item, refund) {
    controlledRequest('/api/platform/operations/tenants/' + encodeURIComponent(item.tenant_id) + '/refunds/' + encodeURIComponent(refund.refund_id) + '/query', item, '查询退款状态', 'provider_reconciliation', 'refund-query').then(function (result) {
      if (result) { return refresh().then(function () { return loadDetail(item.tenant_id); }); }
      return null;
    }).catch(function (err) { error(err.message || '退款查询失败'); });
  }

  function addDetailMetric(grid, labelText, valueText) {
    var cell = document.createElement('div'), label = document.createElement('span'), value = document.createElement('strong');
    label.textContent = labelText; value.textContent = valueText; cell.appendChild(label); cell.appendChild(value); grid.appendChild(cell);
  }
  function evidenceSection(panel, titleText, headers, rows, renderer, emptyText) {
    var section = document.createElement('section'), heading = document.createElement('h3');
    section.className = 'evidence-section'; heading.textContent = titleText; section.appendChild(heading);
    if (!rows.length) { var none = document.createElement('p'); none.className = 'muted'; none.textContent = emptyText; section.appendChild(none); panel.appendChild(section); return; }
    var wrap = document.createElement('div'), table = document.createElement('table'), head = document.createElement('thead'), headRow = document.createElement('tr'), body = document.createElement('tbody');
    wrap.className = 'table-wrap';
    headers.forEach(function (header) { var th = document.createElement('th'); th.textContent = header; headRow.appendChild(th); });
    head.appendChild(headRow); table.appendChild(head); table.appendChild(body); wrap.appendChild(table); section.appendChild(wrap);
    rows.forEach(function (row) { body.appendChild(renderer(row)); }); panel.appendChild(section);
  }
  function rowFrom(values) {
    var row = document.createElement('tr');
    values.forEach(function (value) {
      var cell = document.createElement('td');
      if (value && value.nodeType) { cell.appendChild(value); } else { cell.textContent = value === null || value === undefined ? '—' : String(value); }
      row.appendChild(cell);
    });
    return row;
  }
  function loadDetail(tenantId) {
    return request('/api/platform/operations/tenants/' + encodeURIComponent(tenantId)).then(function (item) {
      var panel = el('tenant-detail'); panel.hidden = false; clear(panel);
      var title = document.createElement('div'), h2 = document.createElement('h2'), close = actionButton('关闭', function () { panel.hidden = true; });
      title.className = 'card-hd'; h2.textContent = item.tenant_name + ' · 商业与运营详情'; title.appendChild(h2); title.appendChild(close); panel.appendChild(title);
      var grid = document.createElement('div'); grid.className = 'detail-grid';
      addDetailMetric(grid, '服务状态', item.lifecycle_status + (item.suspension_reason ? ' · ' + item.suspension_reason : ''));
      addDetailMetric(grid, '订阅状态', (item.subscription_stored_status || '无') + ' / 有效 ' + item.subscription_status);
      addDetailMetric(grid, '服务期', date(item.subscription_starts_at) + ' 至 ' + date(item.subscription_ends_at));
      addDetailMetric(grid, '宽限期结束', date(item.subscription_grace_ends_at));
      addDetailMetric(grid, '渠道收款 / 成功退款', money(item.provider_receipt_cents) + ' / ' + money(item.provider_refund_cents));
      addDetailMetric(grid, '渠道净收入', money(item.provider_net_revenue_cents));
      addDetailMetric(grid, '手工账本收 / 退', money(item.manual_receipt_cents) + ' / ' + money(item.manual_refund_cents));
      addDetailMetric(grid, '异常 / 退款处理中', number(tenantExceptionCount(item)) + ' / ' + number(item.refund_pending_count));
      addDetailMetric(grid, '媒体 / 存储', number(item.media_file_count) + ' / ' + bytes(item.storage_bytes));
      panel.appendChild(grid);

      var refundedPayments = {};
      item.refund_orders.forEach(function (refund) { refundedPayments[refund.payment_order_id] = true; });
      evidenceSection(panel, '渠道支付订单', ['时间', '套餐 / 金额', '状态', '脱敏参考号', '操作'], item.payment_orders, function (order) {
        var actions = document.createElement('div'); actions.className = 'action-row';
        if (order.status === 'paid_activation_pending' || order.status === 'failed') { actions.appendChild(actionButton('查询 / 恢复', function () { queryPayment(item, order); })); }
        if (order.status === 'succeeded' && !refundedPayments[order.order_id]) { actions.appendChild(actionButton('全额退款', function () { submitRefund(item, order); }, 'btn-danger')); }
        return rowFrom([date(order.created_at), order.plan_name + ' · ' + money(order.amount_cents), order.status + (order.failure_code ? ' · ' + order.failure_code : ''), order.provider_order_ref_masked + ' / ' + (order.provider_transaction_ref_masked || '—'), actions]);
      }, '暂无渠道支付订单');
      evidenceSection(panel, '渠道退款', ['申请时间', '金额', '状态', '脱敏参考号', '操作'], item.refund_orders, function (refund) {
        var actions = document.createElement('div'); actions.className = 'action-row';
        if (refund.status !== 'succeeded') { actions.appendChild(actionButton('查询状态', function () { queryRefund(item, refund); })); }
        return rowFrom([date(refund.requested_at), money(refund.amount_cents), refund.status + (refund.failure_code ? ' · ' + refund.failure_code : ''), (refund.provider_ref_masked || '—') + ' / ' + (refund.provider_refund_id_masked || '—'), actions]);
      }, '暂无渠道退款');
      evidenceSection(panel, '手工账本（不计入渠道净收入）', ['发生时间', '类型', '金额', '脱敏参考号'], item.manual_financial_transactions, function (entry) {
        return rowFrom([date(entry.occurred_at), entry.kind, money(entry.amount_cents), entry.reference_masked || '—']);
      }, '暂无手工账本记录');
      evidenceSection(panel, '最近异常', ['时间', '类型', '状态', '故障代码'], item.recent_exceptions, function (entry) {
        return rowFrom([date(entry.occurred_at), entry.kind, entry.status, entry.failure_code || '—']);
      }, '暂无支付、激活或退款异常');
      evidenceSection(panel, '最近审计事件', ['时间', '事件 ID', '动作', '对象'], item.audit_events, function (entry) {
        return rowFrom([date(entry.created_at), entry.audit_id, entry.action, entry.object_type + ' · ' + (entry.object_id || '—')]);
      }, '暂无审计事件');
      return item;
    }).catch(function (err) { error(err.message || '详情加载失败'); });
  }

  var PRODUCT_EVENT_LABELS = {
    'product.auth.login_succeeded.v1': '登录成功',
    'product.auth.login_failed.v1': '登录失败',
    'product.conversation.review_opened.v1': '打开会话查阅',
    'product.directory.view_selected.v1': '切换员工 / 联系人视图',
    'product.directory.subject_selected.v1': '选择员工 / 联系人',
    'product.conversation.detail_opened.v1': '打开会话',
    'product.conversation.older_messages_loaded.v1': '加载更早消息',
    'product.search.executed.v1': '执行搜索',
    'product.search.filter_applied.v1': '使用筛选器',
    'product.media.preview_opened.v1': '打开媒体预览',
    'product.settings.opened.v1': '打开设置',
    'product.settings.language_changed.v1': '修改语言',
    'product.feedback.submitted.v1': '提交反馈'
  };

  function productEventLabel(eventName) { return PRODUCT_EVENT_LABELS[eventName] || eventName; }
  function productDateInput(value) { return value.toISOString().slice(0, 10); }
  function initialiseProductAnalyticsDates() {
    var end = new Date(), start = new Date(end.getTime() - 29 * 24 * 60 * 60 * 1000);
    el('product-start').value = productDateInput(start);
    el('product-end').value = productDateInput(end);
  }
  function productQuery() {
    var params = new URLSearchParams();
    var start = el('product-start').value, end = el('product-end').value;
    if (start) { params.set('starts_at', start + 'T00:00:00+08:00'); }
    if (end) { params.set('ends_at', end + 'T23:59:59.999+08:00'); }
    if (el('product-tenant').value) { params.set('tenant_id', el('product-tenant').value); }
    if (el('product-event').value) { params.set('event_name', el('product-event').value); }
    return params;
  }
  function productRequestPath(path, includeActivity) {
    var params = productQuery();
    if (includeActivity) { params.set('activity_status', el('product-activity').value); params.set('page_size', '100'); }
    return path + '?' + params.toString();
  }
  function appendProductTenantOptions(items) {
    var select = el('product-tenant'), selected = select.value, known = {};
    Array.prototype.forEach.call(select.options, function (option) { known[option.value] = true; });
    (items || []).forEach(function (item) {
      if (known[item.tenant_id]) { return; }
      var option = document.createElement('option');
      option.value = item.tenant_id;
      option.textContent = item.tenant_name;
      select.appendChild(option);
    });
    select.value = selected;
  }
  function renderProductOverview(data) {
    el('product-provisioned').textContent = number(data.provisioned_tenant_count);
    el('product-active').textContent = '近期活跃 ' + number(data.active_tenant_count) + ' 个';
    el('product-inactive').textContent = number(data.inactive_tenant_count);
    el('product-active-admins').textContent = number(data.active_admin_count);
    el('product-login-counts').textContent = number(data.login.success_count) + ' / ' + number(data.login.failure_count);
    el('product-failure-rate').textContent = data.login.failure_rate === null ? '无登录尝试' : '失败率 ' + (data.login.failure_rate * 100).toFixed(2) + '%';
    list(el('product-adoption'), data.function_adoption, function (item) {
      return textPair(productEventLabel(item.event_name), number(item.tenant_count) + ' 个租户 · ' + number(item.admin_count) + ' 位管理员 · ' + number(item.event_count) + ' 次');
    }, '当前筛选范围内暂无功能使用事件');
    var body = el('product-trends');
    clear(body);
    data.trends.forEach(function (item) {
      var eventTotal = Object.keys(item.event_counts || {}).filter(function (name) {
        return name !== 'product.auth.login_succeeded.v1' && name !== 'product.auth.login_failed.v1';
      }).reduce(function (total, name) { return total + Number(item.event_counts[name] || 0); }, 0);
      var row = document.createElement('tr');
      [item.date, number(item.login_success_count), number(item.login_failure_count), number(item.active_tenant_count), number(eventTotal)].forEach(function (value) {
        var cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
      });
      body.appendChild(row);
    });
    if (!data.trends.length) {
      var emptyRow = document.createElement('tr'), emptyCell = document.createElement('td');
      emptyCell.colSpan = 5; emptyCell.className = 'muted'; emptyCell.textContent = '当前筛选范围内暂无趋势数据'; emptyRow.appendChild(emptyCell); body.appendChild(emptyRow);
    }
  }
  function renderProductTenants(data) {
    appendProductTenantOptions(data.items);
    var body = el('product-tenant-rows');
    clear(body);
    if (!data.items.length) {
      var emptyRow = document.createElement('tr'), emptyCell = document.createElement('td');
      emptyCell.colSpan = 7; emptyCell.className = 'muted'; emptyCell.textContent = '没有符合筛选条件的租户'; emptyRow.appendChild(emptyCell); body.appendChild(emptyRow);
    }
    data.items.forEach(function (item) {
      var row = document.createElement('tr');
      [item.tenant_name + ' · ' + item.tenant_slug, item.activity_status === 'active' ? '近期活跃' : '近期未使用', date(item.last_login_at), date(item.last_product_event_at), number(item.active_admin_count), number(item.event_count)].forEach(function (value) {
        var cell = document.createElement('td'); cell.textContent = value; row.appendChild(cell);
      });
      var actionCell = document.createElement('td');
      actionCell.appendChild(actionButton('使用详情', function () { loadProductTenantDetail(item.tenant_id); }));
      row.appendChild(actionCell); body.appendChild(row);
    });
    el('product-pagination').textContent = '显示 ' + number(data.items.length) + ' / ' + number(data.total) + ' 个租户';
  }
  function productDetailMetric(grid, labelText, valueText) { addDetailMetric(grid, labelText, valueText); }
  function renderProductTenantDetail(item) {
    var panel = el('product-analytics-detail'); panel.hidden = false; clear(panel);
    var title = document.createElement('div'), heading = document.createElement('h2'), close = actionButton('关闭', function () { panel.hidden = true; });
    title.className = 'card-hd'; heading.textContent = item.tenant_name + ' · 产品使用详情'; title.appendChild(heading); title.appendChild(close); panel.appendChild(title);
    var grid = document.createElement('div'); grid.className = 'detail-grid';
    productDetailMetric(grid, '产品活跃状态', item.activity_status === 'active' ? '近期活跃' : '近期未使用');
    productDetailMetric(grid, '最近登录', date(item.last_login_at));
    productDetailMetric(grid, '最近产品行为', date(item.last_product_event_at));
    productDetailMetric(grid, '活跃管理员', number(item.active_admin_count));
    productDetailMetric(grid, '登录成功 / 失败', number(item.login.success_count) + ' / ' + number(item.login.failure_count));
    productDetailMetric(grid, '登录失败率', item.login.failure_rate === null ? '无登录尝试' : (item.login.failure_rate * 100).toFixed(2) + '%');
    panel.appendChild(grid);
    evidenceSection(panel, '功能采用情况', ['功能', '租户数', '管理员数', '事件量'], item.function_adoption, function (entry) {
      return rowFrom([productEventLabel(entry.event_name), number(entry.tenant_count), number(entry.admin_count), number(entry.event_count)]);
    }, '当前筛选范围内暂无功能使用事件');
    evidenceSection(panel, '登录与使用趋势', ['日期', '登录成功', '登录失败', '活跃租户'], item.trends, function (entry) {
      return rowFrom([entry.date, number(entry.login_success_count), number(entry.login_failure_count), number(entry.active_tenant_count)]);
    }, '当前筛选范围内暂无趋势数据');
  }
  function loadProductTenantDetail(tenantId) {
    return request(productRequestPath('/api/platform/operations/product-analytics/tenants/' + encodeURIComponent(tenantId), false)).then(renderProductTenantDetail).catch(function (err) { error(err.message || '产品使用详情加载失败'); });
  }
  function loadProductAnalytics() {
    return Promise.all([
      request(productRequestPath('/api/platform/operations/product-analytics/overview', false)),
      request(productRequestPath('/api/platform/operations/product-analytics/tenants', true))
    ]).then(function (results) { renderProductOverview(results[0]); renderProductTenants(results[1]); });
  }

  var originalRefresh = refresh;
  refresh = function () {
    error('');
    return Promise.all([originalRefresh(), loadProductAnalytics()]).catch(function (err) { error(err.message || '加载失败'); });
  };
  initialiseProductAnalyticsDates();
  el('refresh').addEventListener('click', refresh);
  el('tenant-filters').addEventListener('submit', function (event) { event.preventDefault(); state.page = 1; loadTenants().catch(function (err) { error(err.message); }); });
  el('reset-filters').addEventListener('click', function () { el('tenant-filters').reset(); state.page = 1; loadTenants().catch(function (err) { error(err.message); }); });
  el('previous-page').addEventListener('click', function () { if (state.page > 1) { state.page -= 1; loadTenants().catch(function (err) { error(err.message); }); } });
  el('next-page').addEventListener('click', function () { if (state.page * state.pageSize < state.total) { state.page += 1; loadTenants().catch(function (err) { error(err.message); }); } });
  el('product-analytics-filters').addEventListener('submit', function (event) { event.preventDefault(); loadProductAnalytics().catch(function (err) { error(err.message || '产品使用分析加载失败'); }); });
  refresh();
}());
