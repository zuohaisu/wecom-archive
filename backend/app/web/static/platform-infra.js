/* RND-414: /platform/infra — 3 sub-tabs. 品牌域名/TLS is real data
 * (GET /api/platform/branding/domains + /domain-metrics). 连通性 and
 * 渠道（WeCom） have no cross-tenant backing endpoint yet (Spec §6 gap) —
 * both call the assumed contract and gap-degrade on failure rather than
 * fabricate numbers. No innerHTML — createElement/textContent only. */
(function () {
  'use strict';
  var PC = window.PC;
  var domainRows = [];
  var domainSort = 'expires';

  // ------------------------------------------------------------ tabs
  function activateTab(tabId) {
    document.querySelectorAll('.tab').forEach(function (btn) {
      var active = btn.getAttribute('data-tab') === tabId;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    ['conn', 'domains', 'provider'].forEach(function (id) {
      PC.el('panel-' + id).hidden = id !== tabId;
    });
  }
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () { activateTab(btn.getAttribute('data-tab')); });
  });

  // ------------------------------------------------------- 连通性 (gap)
  var CONN_REASON_CLASS = { invalid_credentials: 'quota-danger', network_error: 'quota-warning' };
  function renderConnectivity(data) {
    var items = (data && data.items) || [];
    var ok = items.filter(function (i) { return i.status === 'ok'; }).length;
    var fail = items.filter(function (i) { return i.status === 'fail'; }).length;
    var unchecked = items.length - ok - fail;
    PC.el('conn-summary').textContent = 'OK ' + PC.number(ok) + ' · FAIL ' + PC.number(fail) + ' · 未检查 ' + PC.number(unchecked);
    var body = PC.el('conn-rows');
    PC.clear(body);
    if (!items.length) { PC.emptyRow(body, 6, '暂无连通性检查结果'); return; }
    items.forEach(function (item) {
      var name = document.createElement('td'), main = document.createElement('span'), slug = document.createElement('span');
      main.className = 'tenant-name'; main.textContent = item.tenant_name;
      slug.className = 'tenant-slug'; slug.textContent = item.tenant_slug;
      name.appendChild(main); name.appendChild(slug);
      var badgeTone = item.status === 'ok' ? 'success' : (item.status === 'fail' ? 'failed' : 'plain');
      var resultBadge = document.createElement('span'); resultBadge.className = 'badge badge-' + badgeTone + ' badge-dot';
      resultBadge.textContent = item.status === 'ok' ? 'OK' : (item.status === 'fail' ? 'FAIL' : '未检查');
      var resultCell = document.createElement('td'); resultCell.appendChild(resultBadge);
      var reasonCell = document.createElement('td');
      reasonCell.textContent = item.reason || '—';
      reasonCell.className = CONN_REASON_CLASS[item.reason] || 'muted';
      var actions = document.createElement('div'); actions.className = 'action-row';
      actions.appendChild(PC.actionButton('重新检查', function () { recheckOne(item); }));
      actions.appendChild(PC.actionButton('详情', function () { window.location.href = '/platform/tenants/' + encodeURIComponent(item.tenant_id); }));
      var actionsCell = document.createElement('td'); actionsCell.appendChild(actions);
      body.appendChild(PC.rowFrom([name, resultCell, reasonCell, PC.date(item.checked_at), item.callback_domain || '—', actionsCell]));
    });
  }
  function recheckOne(item) {
    PC.request('/api/platform/tenants/' + encodeURIComponent(item.tenant_id) + '/connectivity-check', { method: 'POST' })
      .then(loadConnectivity)
      .catch(function (err) { PC.error(err.message || '连通性检查失败'); });
  }
  function loadConnectivity() {
    return PC.request('/api/platform/operations/connectivity').then(renderConnectivity).catch(function () {
      PC.el('conn-summary').textContent = '跨租户连通性结果依赖的接口尚未交付';
      PC.emptyRow(PC.el('conn-rows'), 6, '跨租户连通性结果依赖的接口尚未交付，暂无数据可展示。单租户凭据可在租户详情页单独检查。');
    });
  }

  // -------------------------------------------------- 品牌域名 / TLS (real)
  var CERT_BADGE = { issued: 'success', expiring: 'pending', validating: 'info', pending: 'info', failed: 'failed' };
  function sortDomains() {
    var rows = domainRows.slice();
    if (domainSort === 'hostname') { rows.sort(function (a, b) { return (a.hostname || '').localeCompare(b.hostname || ''); }); }
    else if (domainSort === 'status') { rows.sort(function (a, b) { return (a.domain_state || '').localeCompare(b.domain_state || ''); }); }
    else { rows.sort(function (a, b) { return new Date(a.certificate_expires_at || 0) - new Date(b.certificate_expires_at || 0); }); }
    return rows;
  }
  function renderDomainTable() {
    var body = PC.el('domain-rows');
    PC.clear(body);
    var rows = sortDomains();
    if (!rows.length) { PC.emptyRow(body, 7, '暂无托管域名'); return; }
    rows.forEach(function (item) {
      var certTone = CERT_BADGE[item.certificate_status] || 'plain';
      var certBadge = document.createElement('span'); certBadge.className = 'badge badge-' + certTone; certBadge.textContent = item.certificate_status || '—';
      var stateBadge = document.createElement('span'); stateBadge.className = 'badge'; stateBadge.textContent = item.domain_state || '—';
      var expiresCell = document.createElement('td'); expiresCell.textContent = PC.date(item.certificate_expires_at);
      var soon = item.certificate_expires_at && (new Date(item.certificate_expires_at) - Date.now()) < 30 * 24 * 3600 * 1000;
      if (soon) { expiresCell.className = 'quota-warning'; }
      var row = document.createElement('tr');
      var hostCell = document.createElement('td'); hostCell.className = 'mono'; hostCell.textContent = item.hostname;
      var tenantCell = document.createElement('td'); tenantCell.className = 'muted'; tenantCell.textContent = item.tenant_id;
      var stateCell = document.createElement('td'); stateCell.appendChild(stateBadge);
      var enabledCell = document.createElement('td'); enabledCell.textContent = item.domain_enabled ? '是' : '否';
      var certCell = document.createElement('td'); certCell.appendChild(certBadge);
      var checkedCell = document.createElement('td'); checkedCell.className = 'muted'; checkedCell.textContent = PC.date(item.certificate_last_checked_at);
      [hostCell, tenantCell, stateCell, enabledCell, certCell, expiresCell, checkedCell].forEach(function (cell) { row.appendChild(cell); });
      body.appendChild(row);
    });
  }
  PC.el('domain-sort').querySelectorAll('button').forEach(function (btn) {
    btn.addEventListener('click', function () {
      PC.el('domain-sort').querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      domainSort = btn.getAttribute('data-sort');
      renderDomainTable();
    });
  });
  function loadDomains() {
    return Promise.all([
      PC.request('/api/platform/branding/domains'),
      PC.request('/api/platform/branding/domain-metrics')
    ]).then(function (results) {
      var domains = results[0].domains || results[0].items || results[0] || [];
      var metrics = results[1];
      domainRows = domains;
      var enabled = domains.filter(function (d) { return d.domain_enabled; }).length;
      PC.el('domain-total').textContent = PC.number(domains.length);
      PC.el('domain-enabled-disabled').textContent = '已启用 ' + PC.number(enabled) + ' · 已停用 ' + PC.number(domains.length - enabled);
      var issued = domains.filter(function (d) { return d.certificate_status === 'issued'; }).length;
      PC.el('cert-issued').textContent = PC.number(issued);
      PC.el('cert-other').textContent = '验证中 ' + PC.number(metrics.pending_certificate_count) + ' · 失败 ' + PC.number(metrics.certificate_failure_count);
      PC.el('cert-expiring').textContent = PC.number(metrics.certificates_expiring_30_days_count);
      PC.el('cert-failed').textContent = PC.number(metrics.certificate_failure_count);
      PC.el('cert-invalid-binding').textContent = '待验证 ' + PC.number(metrics.pending_verification_count) + ' · 绑定异常 ' + PC.number(metrics.invalid_binding_count);
      renderDomainTable();
    });
  }

  // ------------------------------------------------- 渠道（WeCom） --
  // suite_ticket status is a REAL endpoint (app/routers/
  // wecom_provider_instructions.py provider_suite_ticket_status) — not a
  // gap. It exposes only {received, state, last_received_at, age_seconds,
  // requires_alert}, never the ticket value or a threshold constant.
  function loadProvider() {
    PC.request('/api/platform/wecom/third-party/suite-ticket-status').then(function (data) {
      var body = PC.el('suite-ticket-body');
      PC.clear(body);
      var stat = document.createElement('div'); stat.style.cssText = 'font-size:var(--text-2xl);font-weight:var(--weight-semibold)';
      stat.textContent = (data.age_seconds === null || data.age_seconds === undefined ? '—' : PC.number(data.age_seconds) + ' 秒');
      var label = document.createElement('div'); label.className = 'muted'; label.textContent = '距上次接收（age）';
      body.appendChild(stat); body.appendChild(label);
      var dl = document.createElement('dl'); dl.className = 'kv'; dl.style.marginTop = 'var(--space-4)';
      [['received', data.received ? 'true' : 'false'], ['state', data.state], ['需要告警', data.requires_alert ? '是' : '否'], ['票据值', '不展示、不记录、不导出']].forEach(function (pair) {
        var dt = document.createElement('dt'); dt.textContent = pair[0];
        var dd = document.createElement('dd'); dd.textContent = pair[1];
        dl.appendChild(dt); dl.appendChild(dd);
      });
      body.appendChild(dl);
    }).catch(function (err) {
      var body = PC.el('suite-ticket-body'); PC.clear(body);
      var msg = document.createElement('p'); msg.className = 'muted'; msg.textContent = err.message || 'suite_ticket 状态加载失败。';
      body.appendChild(msg);
    });
    PC.request('/api/platform/operations/suite-ticket/config').then(function (data) {
      var body = PC.el('provider-config-body');
      PC.clear(body);
      var ul = document.createElement('ul'); ul.className = 'summary-list';
      ul.appendChild(PC.textPair('第三方回调', data.callback_reachable ? '可达' : '不可达'));
      ul.appendChild(PC.textPair('最近票据接收', PC.date(data.last_received_at)));
      ul.appendChild(PC.textPair('24 小时接收次数', PC.number(data.received_count_24h)));
      ul.appendChild(PC.textPair('suite_id', data.suite_id_masked || '—'));
      ul.appendChild(PC.textPair('API v3 密钥', '已配置 · 不展示'));
      body.appendChild(ul);
    }).catch(function () {
      var body = PC.el('provider-config-body'); PC.clear(body);
      var msg = document.createElement('p'); msg.className = 'muted'; msg.textContent = '渠道回调与配置状态依赖的接口尚未交付。';
      body.appendChild(msg);
    });
  }

  function refresh() {
    PC.error('');
    return Promise.all([loadConnectivity(), loadDomains()]).then(loadProvider).catch(function (err) { PC.error(err.message || '加载失败'); });
  }
  PC.el('conn-recheck-all').addEventListener('click', loadConnectivity);
  PC.wireRefreshStamp(refresh);
  refresh();
}());
