/* RND-414: /platform/analytics — lift-and-shift of the product-analytics
 * block that used to live on the operations dashboard (pre-RND-414
 * platform_operations.html/platform-operations.js). Same endpoints, same
 * field names, same behavior — only the DOM ids' page moved and the local
 * helper copies (el/clear/number/...) are now the shared PC.* ones from
 * platform-console.js. No innerHTML — createElement/textContent only. */
(function () {
  'use strict';
  var PC = window.PC;

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
    PC.el('product-start').value = productDateInput(start);
    PC.el('product-end').value = productDateInput(end);
  }
  function productQuery() {
    var params = new URLSearchParams();
    var start = PC.el('product-start').value, end = PC.el('product-end').value;
    if (start) { params.set('starts_at', start + 'T00:00:00+08:00'); }
    if (end) { params.set('ends_at', end + 'T23:59:59.999+08:00'); }
    if (PC.el('product-tenant').value) { params.set('tenant_id', PC.el('product-tenant').value); }
    if (PC.el('product-event').value) { params.set('event_name', PC.el('product-event').value); }
    return params;
  }
  function productRequestPath(path, includeActivity) {
    var params = productQuery();
    if (includeActivity) { params.set('activity_status', PC.el('product-activity').value); params.set('page_size', '100'); }
    return path + '?' + params.toString();
  }
  function appendProductTenantOptions(items) {
    var select = PC.el('product-tenant'), selected = select.value, known = {};
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

  function renderProductOverview(data) {
    PC.el('product-provisioned').textContent = PC.number(data.provisioned_tenant_count);
    PC.el('product-active').textContent = '近期活跃 ' + PC.number(data.active_tenant_count) + ' 个';
    PC.el('product-inactive').textContent = PC.number(data.inactive_tenant_count);
    PC.el('product-active-admins').textContent = PC.number(data.active_admin_count);
    PC.el('product-login-counts').textContent = PC.number(data.login.success_count) + ' / ' + PC.number(data.login.failure_count);
    PC.el('product-failure-rate').textContent = data.login.failure_rate === null ? '无登录尝试' : '失败率 ' + (data.login.failure_rate * 100).toFixed(2) + '%';
    PC.list(PC.el('product-adoption'), data.function_adoption, function (item) {
      return PC.textPair(productEventLabel(item.event_name), PC.number(item.tenant_count) + ' 个租户 · ' + PC.number(item.admin_count) + ' 位管理员 · ' + PC.number(item.event_count) + ' 次');
    }, '当前筛选范围内暂无功能使用事件');
    var body = PC.el('product-trends');
    PC.clear(body);
    data.trends.forEach(function (item) {
      var eventTotal = Object.keys(item.event_counts || {}).filter(function (name) {
        return name !== 'product.auth.login_succeeded.v1' && name !== 'product.auth.login_failed.v1';
      }).reduce(function (total, name) { return total + Number(item.event_counts[name] || 0); }, 0);
      body.appendChild(PC.rowFrom([item.date, PC.number(item.login_success_count), PC.number(item.login_failure_count), PC.number(item.active_tenant_count), PC.number(eventTotal)]));
    });
    if (!data.trends.length) { PC.emptyRow(body, 5, '当前筛选范围内暂无趋势数据'); }
  }
  function renderProductTenants(data) {
    appendProductTenantOptions(data.items);
    var body = PC.el('product-tenant-rows');
    PC.clear(body);
    if (!data.items.length) { PC.emptyRow(body, 7, '没有符合筛选条件的租户'); }
    data.items.forEach(function (item) {
      var actionCell = document.createElement('td');
      actionCell.appendChild(PC.actionButton('使用详情', function () { loadProductTenantDetail(item.tenant_id); }));
      var row = PC.rowFrom([
        item.tenant_name + ' · ' + item.tenant_slug,
        item.activity_status === 'active' ? '近期活跃' : '近期未使用',
        PC.date(item.last_login_at),
        PC.date(item.last_product_event_at),
        PC.number(item.active_admin_count),
        PC.number(item.event_count)
      ]);
      row.appendChild(actionCell);
      body.appendChild(row);
    });
    PC.el('product-pagination').textContent = '显示 ' + PC.number(data.items.length) + ' / ' + PC.number(data.total) + ' 个租户';
  }
  function renderProductTenantDetail(item) {
    var panel = PC.el('product-analytics-detail'); panel.hidden = false; PC.clear(panel);
    var title = document.createElement('div'), heading = document.createElement('h2'), close = PC.actionButton('关闭', function () { panel.hidden = true; });
    title.className = 'card-hd'; heading.textContent = item.tenant_name + ' · 产品使用详情'; title.appendChild(heading); title.appendChild(close); panel.appendChild(title);
    var grid = document.createElement('div'); grid.className = 'detail-grid';
    addDetailMetric(grid, '产品活跃状态', item.activity_status === 'active' ? '近期活跃' : '近期未使用');
    addDetailMetric(grid, '最近登录', PC.date(item.last_login_at));
    addDetailMetric(grid, '最近产品行为', PC.date(item.last_product_event_at));
    addDetailMetric(grid, '活跃管理员', PC.number(item.active_admin_count));
    addDetailMetric(grid, '登录成功 / 失败', PC.number(item.login.success_count) + ' / ' + PC.number(item.login.failure_count));
    addDetailMetric(grid, '登录失败率', item.login.failure_rate === null ? '无登录尝试' : (item.login.failure_rate * 100).toFixed(2) + '%');
    panel.appendChild(grid);
    evidenceSection(panel, '功能采用情况', ['功能', '租户数', '管理员数', '事件量'], item.function_adoption, function (entry) {
      return PC.rowFrom([productEventLabel(entry.event_name), PC.number(entry.tenant_count), PC.number(entry.admin_count), PC.number(entry.event_count)]);
    }, '当前筛选范围内暂无功能使用事件');
    evidenceSection(panel, '登录与使用趋势', ['日期', '登录成功', '登录失败', '活跃租户'], item.trends, function (entry) {
      return PC.rowFrom([entry.date, PC.number(entry.login_success_count), PC.number(entry.login_failure_count), PC.number(entry.active_tenant_count)]);
    }, '当前筛选范围内暂无趋势数据');
  }
  function loadProductTenantDetail(tenantId) {
    return PC.request(productRequestPath('/api/platform/operations/product-analytics/tenants/' + encodeURIComponent(tenantId), false)).then(renderProductTenantDetail).catch(function (err) { PC.error(err.message || '产品使用详情加载失败'); });
  }
  function loadProductAnalytics() {
    PC.error('');
    return Promise.all([
      PC.request(productRequestPath('/api/platform/operations/product-analytics/overview', false)),
      PC.request(productRequestPath('/api/platform/operations/product-analytics/tenants', true))
    ]).then(function (results) { renderProductOverview(results[0]); renderProductTenants(results[1]); })
      .catch(function (err) { PC.error(err.message || '加载失败'); });
  }

  initialiseProductAnalyticsDates();
  PC.el('product-analytics-filters').addEventListener('submit', function (event) { event.preventDefault(); loadProductAnalytics(); });
  PC.wireRefreshStamp(loadProductAnalytics);
  loadProductAnalytics();
}());
