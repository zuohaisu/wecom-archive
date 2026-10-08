/* RND-414: /platform/tenants/{id} (客户详情, 5 tabs). */
(function () {
  'use strict';
  var PC = window.PC;
  var tenantId = decodeURIComponent(window.location.pathname.split('/').filter(Boolean).pop());
  var current = null; // last-loaded TenantOperationsDetailOut

  var STATUS_LABEL = { trial: '试用', active: '有效', grace: '宽限期', expired: '已过期', canceled: '已取消', not_subscribed: '未订阅' };
  function statusLabel(status) { return STATUS_LABEL[status] || status; }

  function tenantRef() { return { tenant_id: current.tenant_id, tenant_name: current.tenant_name, tenant_slug: current.tenant_slug, lifecycle_status: current.lifecycle_status }; }

  function detailMetric(grid, labelText, valueText) {
    var cell = document.createElement('div'), label = document.createElement('span'), value = document.createElement('strong');
    label.textContent = labelText; value.textContent = valueText; cell.appendChild(label); cell.appendChild(value); grid.appendChild(cell);
  }

  function renderBadges(item) {
    var wrap = PC.el('detail-badges'); PC.clear(wrap);
    wrap.appendChild(PC.badge(item.lifecycle_status, item.lifecycle_status === 'active' ? 'success' : (item.lifecycle_status === 'suspended' || item.lifecycle_status === 'frozen' ? 'failed' : 'pending')));
    var excCount = item.payment_exception_count + item.refund_abnormal_count + item.refund_manual_recovery_count + item.refund_closed_count;
    if (excCount) { wrap.appendChild(PC.badge('异常 ' + PC.number(excCount), 'failed')); }
    if (item.storage_utilization_basis_points >= 10000) { wrap.appendChild(PC.badge('存储超配额', 'failed')); }
    else if (item.storage_utilization_basis_points >= 8000) { wrap.appendChild(PC.badge('存储接近配额', 'pending')); }
  }

  function renderCommercial(item) {
    var grid = PC.el('commercial-grid'); PC.clear(grid);
    detailMetric(grid, '服务状态', item.lifecycle_status + (item.suspension_reason ? ' · ' + item.suspension_reason : ''));
    detailMetric(grid, '订阅状态', (item.subscription_stored_status || '无') + ' / ' + statusLabel(item.subscription_status));
    detailMetric(grid, '服务期', PC.date(item.subscription_starts_at) + ' 至 ' + PC.date(item.subscription_ends_at));
    detailMetric(grid, '宽限期结束', PC.date(item.subscription_grace_ends_at));
    detailMetric(grid, '渠道收款', PC.money(item.provider_receipt_cents));
    detailMetric(grid, '渠道成功退款', PC.money(item.provider_refund_cents));
    detailMetric(grid, '渠道净收入', PC.money(item.provider_net_revenue_cents));
    detailMetric(grid, '手工账本收 / 退', PC.money(item.manual_receipt_cents) + ' / ' + PC.money(item.manual_refund_cents) + '（合同定制，单独）');
    var excCount = item.payment_exception_count + item.refund_abnormal_count + item.refund_manual_recovery_count + item.refund_closed_count;
    detailMetric(grid, '异常 / 退款处理中', PC.number(excCount) + ' / ' + PC.number(item.refund_pending_count));
    detailMetric(grid, '媒体 / 存储', PC.number(item.media_file_count) + ' / ' + PC.bytes(item.storage_bytes) + '（' + (item.storage_utilization_basis_points / 100).toFixed(2) + '% · 配额 ' + PC.bytes(item.storage_quota_bytes) + '）');

    var quotaAlert = PC.el('quota-alert');
    if (item.storage_utilization_basis_points >= 10000) {
      quotaAlert.hidden = false;
      quotaAlert.textContent = '存储超出配额 ' + ((item.storage_utilization_basis_points - 10000) / 100).toFixed(2) + '%。年度基础套餐固定 5 GiB 存储，超出部分按 1 元/GB/月 展示、v1 不自动计费。持续超限的客户通常通过手工账本登记定制合同费用（见「手工账本」页签）。';
    } else { quotaAlert.hidden = true; }

    PC.list(PC.el('usage-summary'), [
      { label: '消息', value: PC.number(item.message_count) },
      { label: '存储', value: PC.bytes(item.storage_bytes) + ' / ' + PC.bytes(item.storage_quota_bytes) + ' 配额' },
      { label: '员工席位', value: PC.number(item.admin_account_count) + '（活跃 ' + PC.number(item.active_admin_account_count) + '）' }
    ], function (row) { return PC.textPair(row.label, row.value); }, '暂无用量数据');
  }

  function renderConnectivityPlaceholder() {
    // Connectivity is a real (non-idempotent-in-effect-but-live) probe
    // against the tenant's actual WeCom credentials — Spec §3 marks it
    // "直接执行，不需要确认" (no confirmation needed once triggered), but
    // that's not the same as auto-firing it on every page view. Wait for
    // an explicit "重新检查" click instead of pinging WeCom on every open.
    PC.list(PC.el('connectivity-summary'), [
      { label: 'WeCom 凭据连通性', value: '尚未检查（点击"重新检查"）' },
      { label: '凭据轮换', value: '不展示密钥' }
    ], function (row) { return PC.textPair(row.label, row.value); }, '暂无连通性数据');
  }
  function runConnectivityCheck() {
    PC.request('/api/platform/tenants/' + encodeURIComponent(tenantId) + '/connectivity-check', { method: 'POST' }).then(function (result) {
      PC.list(PC.el('connectivity-summary'), [
        { label: 'WeCom 凭据连通性', value: result.ok ? 'OK' : ('FAIL' + (result.reason ? ' · ' + result.reason : '')) },
        { label: '最近检查', value: PC.date(new Date()) },
        { label: '凭据轮换', value: '不展示密钥' }
      ], function (row) { return PC.textPair(row.label, row.value); }, '暂无连通性数据');
    }).catch(function (err) { PC.empty(PC.el('connectivity-summary'), err.message || '连通性检查失败'); });
  }

  function renderSubscription(item) {
    PC.el('subscription-revision').textContent = 'revision ' + (item.subscription_revision || 1);
    var kv = PC.ops.kv([
      ['套餐', '年度基础套餐 · annual_base_cny_99（唯一套餐）· ¥99/年 · 5 GiB 存储 · 席位不限'],
      ['状态', statusLabel(item.subscription_status)],
      ['服务期起', PC.date(item.subscription_starts_at)],
      ['服务期止', PC.date(item.subscription_ends_at)],
      ['续订次数', PC.number(item.subscription_renewal_count || 0)],
      ['修订号', item.subscription_revision || 1]
    ]);
    var wrap = PC.el('subscription-kv'); PC.clear(wrap); wrap.appendChild(kv);
    // Spec §6 gap: no subscription-revisions-history endpoint exists yet.
    PC.emptyRow(PC.el('subscription-history'), 7, '订阅修订历史依赖的接口尚未交付，暂仅展示当前 revision。');
  }

  function renderLedger(item) {
    // PlatformManualFinancialOut has no state/voided field yet (append-only
    // schema, no state machine — Spec §4 gap). Every entry therefore shows
    // 编辑/作废; those two actions hit the assumed gap endpoints and surface
    // a 404 inline via the modal until a companion backend ticket ships.
    var entries = item.manual_financial_transactions || [];
    var receipt = 0, refund = 0;
    entries.forEach(function (e) { if (e.kind === 'receipt') { receipt += e.amount_cents; } else { refund += e.amount_cents; } });
    PC.el('ledger-summary').textContent = '收 ' + PC.money(receipt) + ' · 退 ' + PC.money(refund);
    var body = PC.el('ledger-rows'); PC.clear(body);
    if (!entries.length) { PC.emptyRow(body, 6, '暂无手工账本记录'); }
    entries.forEach(function (entry) {
      var kindBadge = PC.badge(entry.kind === 'receipt' ? '收款' : '退款', entry.kind === 'receipt' ? 'success' : 'failed');
      var actions = document.createElement('div'); actions.className = 'action-row';
      actions.appendChild(PC.actionButton('编辑', function () {
        PC.ops.ledgerEntry({ mode: 'edit', tenant: tenantRef(), entry: entry }, { onDone: load });
      }));
      actions.appendChild(PC.actionButton('作废', function () {
        PC.ops.voidLedgerEntry(entry, tenantRef(), { onDone: load });
      }, 'btn-danger'));
      body.appendChild(PC.rowFrom([PC.date(entry.occurred_at), kindBadge, PC.money(entry.amount_cents), entry.reference_masked || '—', entry.note || '—', actions]));
    });
  }

  function renderPayments(item) {
    var refundedPayments = {};
    (item.refund_orders || []).forEach(function (r) { refundedPayments[r.payment_order_id] = true; });
    var payBody = PC.el('payment-rows'); PC.clear(payBody);
    if (!item.payment_orders.length) { PC.emptyRow(payBody, 5, '暂无渠道支付订单'); }
    item.payment_orders.forEach(function (order) {
      var actions = document.createElement('div'); actions.className = 'action-row';
      if (order.status === 'succeeded' && refundedPayments[order.order_id]) {
        var doneText = document.createElement('span'); doneText.className = 'muted'; doneText.textContent = '已全额退款'; actions.appendChild(doneText);
      } else if (order.status === 'succeeded') {
        actions.appendChild(PC.actionButton('全额退款', function () { PC.ops.submitRefund(tenantRef(), order, { onDone: load }); }, 'btn-danger'));
      } else if (order.status === 'paid_activation_pending' || order.recovery_state === 'manual_recovery') {
        actions.appendChild(PC.actionButton('查询 / 恢复', function () { PC.ops.queryPayment(tenantRef(), order, { onDone: load }); }));
      }
      var refText = (order.provider_transaction_ref_masked || '—') + ' / ' + (order.provider_order_ref_masked || '—');
      var recovery = order.recovery_state && order.recovery_state !== 'not_required' ? ' · 恢复 ' + order.recovery_state + (order.recovery_reason_code ? ' · ' + order.recovery_reason_code : '') : '';
      payBody.appendChild(PC.rowFrom([PC.date(order.created_at), order.plan_name + ' · ' + PC.money(order.amount_cents), order.status + (order.failure_code ? ' · ' + order.failure_code : '') + recovery, refText, actions]));
    });

    var refundBody = PC.el('refund-rows'); PC.clear(refundBody);
    if (!item.refund_orders.length) { PC.emptyRow(refundBody, 5, '暂无渠道退款'); }
    item.refund_orders.forEach(function (refund) {
      var actions = document.createElement('div'); actions.className = 'action-row';
      if (refund.status !== 'succeeded' && refund.status !== 'closed') {
        actions.appendChild(PC.actionButton('查询状态', function () { PC.ops.queryRefund(tenantRef(), refund, { onDone: load }); }));
      } else {
        var dash = document.createElement('span'); dash.textContent = '—'; actions.appendChild(dash);
      }
      refundBody.appendChild(PC.rowFrom([PC.date(refund.requested_at), PC.money(refund.amount_cents), refund.status + (refund.failure_code ? ' · ' + refund.failure_code : ''), refund.provider_ref_masked || '—', actions]));
    });
  }

  function renderAudit(item) {
    var body = PC.el('audit-rows'); PC.clear(body);
    if (!item.audit_events.length) { PC.emptyRow(body, 4, '暂无审计事件'); }
    item.audit_events.forEach(function (entry) {
      var idCell = document.createElement('span'); idCell.className = 'cell-id'; idCell.textContent = entry.audit_id;
      body.appendChild(PC.rowFrom([PC.date(entry.created_at), idCell, entry.action, (entry.object_type || '—') + (entry.object_id ? ' · ' + entry.object_id : '')]));
    });
  }

  function render(item) {
    current = item;
    PC.el('detail-title').textContent = item.tenant_name;
    PC.el('detail-sub').textContent = item.tenant_slug + ' · tenant_id ' + item.tenant_id + ' · 创建于 ' + PC.date(item.created_at) + ' · corp_id 已配置（不展示）';
    renderBadges(item);
    renderCommercial(item);
    renderSubscription(item);
    renderLedger(item);
    renderPayments(item);
    renderAudit(item);
    PC.el('btn-suspend').textContent = item.lifecycle_status === 'suspended' ? '恢复服务' : '人工暂停服务';
    PC.el('btn-suspend').classList.toggle('btn-danger', item.lifecycle_status !== 'suspended');
  }

  function load() {
    PC.error('');
    return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenantId)).then(render).catch(function (err) { PC.error(err.message || '详情加载失败'); });
  }

  function wireTabs() {
    var tabs = document.querySelectorAll('.tab');
    function activate(name) {
      tabs.forEach(function (tab) { tab.classList.toggle('active', tab.dataset.tab === name); });
      document.querySelectorAll('[data-tabpanel]').forEach(function (panel) { panel.hidden = panel.dataset.tabpanel !== name; });
    }
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        activate(tab.dataset.tab);
        window.location.hash = tab.dataset.tab;
      });
    });
    var initial = (window.location.hash || '').replace('#', '');
    if (initial) { activate(initial); }
  }

  PC.el('btn-suspend').addEventListener('click', function () { PC.ops.suspendResume(tenantRef(), { onDone: load }); });
  PC.el('btn-adjust-subscription').addEventListener('click', function () {
    PC.ops.adjustSubscription(tenantRef(), {
      status: current.subscription_status, starts_at: current.subscription_starts_at, ends_at: current.subscription_ends_at,
      plan_code: 'annual_base_cny_99', plan_name: current.subscription_plan_name,
      renewal_count: current.subscription_renewal_count || 0, revision: current.subscription_revision || 1
    }, { onDone: load });
  });
  PC.el('btn-new-ledger').addEventListener('click', function () {
    PC.ops.ledgerEntry({ mode: 'create', tenant: tenantRef() }, { onDone: load });
  });
  PC.el('btn-recheck-connectivity').addEventListener('click', runConnectivityCheck);

  wireTabs();
  PC.wireRefreshStamp(load);
  renderConnectivityPlaceholder();
  load();
}());
