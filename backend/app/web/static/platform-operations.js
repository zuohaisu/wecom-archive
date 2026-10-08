/* RND-414: /platform (运营看板) only. Tenant list/detail, product analytics
 * and the window.confirm-based controlled operations that used to live here
 * moved to platform-tenants.js / platform-tenant-detail.js /
 * platform-analytics.js and the shared modal in platform-console.js. */
(function () {
  'use strict';
  var PC = window.PC;

  function tenantExceptionCount(item) {
    return item.payment_activation_pending + item.payment_failed + item.payment_pending_timeout + item.payment_channel_paid_local_pending + item.payment_query_failed + item.payment_reconciliation_mismatch + item.payment_callback_signature_failure + item.payment_callback_decrypt_failure + item.refund_abnormal + item.refund_closed + item.refund_manual_recovery;
  }

  function renderDashboard(data) {
    var counts = data.tenant_counts, accounts = data.account_counts, usage = data.usage_totals;
    var revenue = data.revenue, manual = data.manual_financial, exceptions = data.exceptions;
    var exceptionTotal = tenantExceptionCount(exceptions);
    PC.el('tenant-total').textContent = PC.number(counts.total);
    PC.el('tenant-states').textContent = '试用 ' + PC.number(counts.trial) + ' · 有效 ' + PC.number(counts.active) + ' · 宽限 ' + PC.number(counts.grace) + ' · 冻结 ' + PC.number(counts.frozen) + ' · 暂停 ' + PC.number(counts.suspended);
    PC.el('admin-accounts').textContent = PC.number(accounts.admin_accounts);
    PC.el('observed-users').textContent = '活跃管理员 ' + PC.number(accounts.active_admin_accounts) + ' · 已观测用户 ' + PC.number(accounts.observed_users);
    PC.el('message-total').textContent = PC.number(usage.message_count);
    PC.el('storage-total').textContent = PC.bytes(usage.storage_bytes);
    PC.el('media-total').textContent = PC.number(usage.media_file_count) + ' 个媒体文件';
    PC.el('receipt-total').textContent = PC.money(revenue.receipt_cents);
    PC.el('refund-total').textContent = PC.money(revenue.refund_cents);
    PC.el('net-revenue').textContent = '渠道净收入 ' + PC.money(revenue.net_revenue_cents);
    PC.el('manual-financial').textContent = '手工收 / 退 ' + PC.money(manual.receipt_cents) + ' / ' + PC.money(manual.refund_cents) + '（合同定制，不计入渠道）';
    PC.el('exception-total').textContent = PC.number(exceptionTotal);
    PC.el('exception-states').textContent = '支付/激活 ' + PC.number(exceptions.payment_activation_pending + exceptions.payment_failed) + ' · 恢复/对账 ' + PC.number(exceptions.payment_pending_timeout + exceptions.payment_channel_paid_local_pending + exceptions.payment_query_failed + exceptions.payment_reconciliation_mismatch + exceptions.payment_callback_signature_failure + exceptions.payment_callback_decrypt_failure) + ' · 退款 ' + PC.number(exceptions.refund_abnormal + exceptions.refund_closed + exceptions.refund_manual_recovery) + ' · 退款处理中 ' + PC.number(exceptions.refund_pending);
    PC.list(PC.el('payment-recovery-findings'), data.recent_payment_findings, function (item) {
      return PC.textPair(item.kind + ' · ' + item.severity + ' · ' + item.status, '次数 ' + PC.number(item.occurrence_count) + ' · ' + PC.date(item.last_detected_at));
    }, '暂无支付恢复或对账异常');
    PC.list(PC.el('subscriptions'), data.subscription_distribution, function (item) {
      return PC.textPair((item.plan_name || '未订阅') + ' · ' + item.status, PC.number(item.tenant_count) + ' 个租户');
    }, '暂无订阅数据');
    PC.list(PC.el('quota-risks'), data.storage_quota_risks, function (item) {
      var li = PC.textPair(item.tenant_name, PC.bytes(item.used_bytes) + ' / ' + PC.bytes(item.quota_bytes) + ' · ' + (item.utilization_basis_points / 100).toFixed(2) + '%');
      li.className = item.state === 'over_quota' || item.state === 'at_quota' ? 'quota-danger' : 'quota-warning';
      return li;
    }, '暂无接近配额的租户');
    PC.list(PC.el('recent-tenants'), data.recent_tenants, function (item) { return PC.textPair(item.tenant_name, PC.date(item.occurred_at)); }, '暂无租户');
    var periods = PC.el('revenue-periods');
    PC.clear(periods);
    revenue.periods.forEach(function (item) {
      periods.appendChild(PC.rowFrom([item.period, PC.money(item.receipt_cents), PC.money(item.refund_cents), PC.money(item.net_revenue_cents)]));
    });
  }

  // design-system.css badge modifiers don't mirror the --color-* token
  // names 1:1 (.badge-failed/.badge-pending, not .badge-danger/.badge-warning).
  var BADGE_CLASS = { success: 'badge-success', warning: 'badge-pending', danger: 'badge-failed' };
  function infraTile(container, tone, badgeText, label, headline, detail) {
    PC.clear(container);
    var box = document.createElement('div');
    box.style.cssText = 'border:1px solid var(--color-' + tone + '-border);background:var(--color-' + tone + '-bg);border-radius:var(--radius-lg);padding:var(--space-3);display:grid;gap:6px';
    var badge = document.createElement('span'); badge.className = 'badge ' + BADGE_CLASS[tone] + ' badge-dot'; badge.textContent = badgeText;
    var name = document.createElement('div'); name.className = 'muted'; name.style.fontSize = 'var(--text-xs)'; name.textContent = label;
    var strong = document.createElement('strong'); strong.style.color = 'var(--color-' + tone + '-fg)'; strong.textContent = headline;
    var small = document.createElement('small'); small.className = 'muted'; small.textContent = detail;
    box.appendChild(badge); box.appendChild(name); box.appendChild(strong); box.appendChild(small);
    container.appendChild(box);
  }
  function infraGapTile(container, label) {
    PC.clear(container);
    var box = document.createElement('div');
    box.style.cssText = 'border:1px dashed var(--color-border-strong);border-radius:var(--radius-lg);padding:var(--space-3);color:var(--color-text-4);font-size:var(--text-sm)';
    box.textContent = label + '：依赖的后端接口尚未交付。';
    container.appendChild(box);
  }
  function loadInfraStatus() {
    // Real endpoint (app/routers/wecom_provider_instructions.py) — not a
    // gap. Only exposes age/state, never the ticket value itself.
    PC.request('/api/platform/wecom/third-party/suite-ticket-status').then(function (data) {
      var fresh = data.state === 'fresh';
      infraTile(PC.el('infra-suite-ticket'), fresh ? 'success' : 'warning', data.state,
        'suite_ticket', fresh ? '渠道票据正常' : '票据状态异常',
        (data.age_seconds === null || data.age_seconds === undefined ? 'age 未知' : 'age ' + PC.number(data.age_seconds) + 's') + ' · 不展示票据值');
    }).catch(function (err) { infraGapTile(PC.el('infra-suite-ticket'), 'suite_ticket 状态'); PC.error(err.message || 'suite_ticket 状态加载失败'); });
    PC.request('/api/platform/branding/domain-metrics').then(function (data) {
      var expiring = data.certificates_expiring_30_days_count > 0;
      infraTile(PC.el('infra-domains'), expiring ? 'warning' : 'success', expiring ? (PC.number(data.certificates_expiring_30_days_count) + ' 即将过期') : 'OK', '托管 TLS 域名',
        expiring ? '证书 30 天内到期' : '证书状态正常',
        '待验证 ' + PC.number(data.pending_verification_count) + ' · 待签发 ' + PC.number(data.pending_certificate_count) + ' · 失败 ' + PC.number(data.certificate_failure_count));
    }).catch(function (err) { infraGapTile(PC.el('infra-domains'), '托管 TLS 域名'); PC.error(err.message || '域名状态加载失败'); });
  }

  function loadRecentOperations() {
    return PC.request('/api/platform/operations/audit-events?page_size=5').then(function (data) {
      PC.list(PC.el('recent-operations'), data.items, function (item) {
        return PC.textPair(item.action + (item.tenant_slug ? '.' + item.tenant_slug : ''), (item.reason_code || '—') + ' · ' + PC.date(item.created_at));
      }, '暂无受控操作');
    }).catch(function () { PC.empty(PC.el('recent-operations'), '此列表依赖跨租户审计接口，尚未交付。'); });
  }

  function loadDashboard() { return PC.request('/api/platform/operations/dashboard').then(renderDashboard); }
  function refresh() {
    PC.error('');
    return Promise.all([loadDashboard(), loadRecentOperations()]).then(loadInfraStatus).catch(function (err) { PC.error(err.message || '加载失败'); });
  }

  PC.wireRefreshStamp(refresh);
  refresh();
}());
