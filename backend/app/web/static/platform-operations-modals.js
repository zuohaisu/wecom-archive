/* RND-414: the six controlled-operation modal definitions (Spec §3 table +
 * §10a–f of the prototype inventory), built on the shared engine in
 * platform-console.js. Loaded after platform-console.js, before any page
 * script — tenant-list, tenant-detail and the cross-tenant ledger page all
 * open these same functions so the copy/fields/reason-code enums live in
 * exactly one place. No innerHTML; createElement/textContent only. */
(function () {
  'use strict';
  var PC = window.PC;

  function kv(pairs) {
    var dl = document.createElement('dl'); dl.className = 'kv';
    pairs.forEach(function (pair) {
      var dt = document.createElement('dt'); dt.textContent = pair[0];
      var dd = document.createElement('dd');
      if (pair[1] && pair[1].nodeType) { dd.appendChild(pair[1]); } else { dd.textContent = pair[1] === null || pair[1] === undefined ? '—' : String(pair[1]); }
      dl.appendChild(dt); dl.appendChild(dd);
    });
    return dl;
  }

  function diffTable(rows) {
    var wrap = document.createElement('div'); wrap.className = 'table-wrap';
    var table = document.createElement('table'); table.className = 'diff-table';
    var thead = document.createElement('thead'), hrow = document.createElement('tr');
    ['字段', '当前', '操作后'].forEach(function (h) { var th = document.createElement('th'); th.textContent = h; hrow.appendChild(th); });
    thead.appendChild(hrow); table.appendChild(thead);
    var tbody = document.createElement('tbody');
    rows.forEach(function (row) {
      var tr = document.createElement('tr');
      [row[0], row[1], row[2]].forEach(function (value, index) {
        var td = document.createElement('td');
        td.textContent = value;
        if (index === 2 && row[3]) { td.className = 'diff-changed'; }
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody); wrap.appendChild(table);
    return wrap;
  }

  function alertBox(tone, strongText, bodyText) {
    var box = document.createElement('div'); box.className = 'alert alert-' + tone;
    var ico = document.createElement('span'); ico.className = 'alert-ico'; ico.textContent = tone === 'danger' ? '!' : (tone === 'warning' ? '!' : 'i');
    var text = document.createElement('div');
    if (strongText) { var strong = document.createElement('strong'); strong.textContent = strongText; text.appendChild(strong); }
    text.appendChild(document.createTextNode(bodyText || ''));
    box.appendChild(ico); box.appendChild(text);
    return box;
  }

  // ---------------------------------------------------------- 10a suspend
  function suspendResume(tenant, opts) {
    var suspending = tenant.lifecycle_status !== 'suspended';
    var next = suspending ? 'suspended' : 'active';
    var title = suspending ? '人工暂停服务' : '恢复服务';
    PC.openModal({
      title: title + ' · ' + tenant.tenant_name,
      danger: suspending,
      renderImpact: function (body) {
        var h3 = document.createElement('h3'); h3.textContent = tenant.tenant_name + '（' + tenant.tenant_slug + '）';
        body.appendChild(h3);
        body.appendChild(diffTable([
          ['服务状态 lifecycle_status', tenant.lifecycle_status, next, true],
          ['客户端登录', suspending ? '可用' : '拒绝', suspending ? '立即拒绝' : '恢复可用', true],
          ['会话同步', suspending ? '运行中' : '已暂停', suspending ? '暂停，归档数据保留' : '恢复运行', true],
          ['订阅与账期', '不受影响', '不受影响', false]
        ]));
        body.appendChild(alertBox('neutral', null, suspending ? '暂停可通过「恢复服务」还原，不影响进行中的退款流程。' : '恢复后客户端登录立即可用。'));
      },
      reasonCodes: [
        { value: 'risk_review', label: '风险复核' },
        { value: 'payment_dispute', label: '支付争议' },
        { value: 'contract_breach', label: '合同违约' },
        { value: 'customer_request', label: '客户要求' },
        { value: 'data_correction', label: '数据更正' }
      ],
      notePlaceholder: '工单号、审批人、判断依据。写入审计，不可修改。',
      unlock: suspending ? { label: '客户 slug', match: tenant.tenant_slug } : null,
      idempotencyPrefix: 'service-control',
      auditAction: suspending ? 'platform.service.suspended' : 'platform.service.resumed',
      submitLabel: suspending ? '确认暂停服务' : '确认恢复服务',
      onSubmit: function (result) {
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenant.tenant_id) + '/service', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({ lifecycle_status: next, reason_code: result.reason_code, confirmation: suspending ? result.confirmation : tenant.tenant_slug })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  // -------------------------------------------------- 10b adjust subscription
  function adjustSubscription(tenant, subscription, opts) {
    var STATUS_LABEL = { trial: '试用', active: '有效', grace: '宽限期', expired: '已过期', canceled: '已取消' };
    PC.openModal({
      title: '调整订阅 · ' + tenant.tenant_name,
      danger: false,
      renderImpact: function (body) {
        var planField = document.createElement('input');
        planField.className = 'input'; planField.disabled = true;
        planField.value = '年度基础套餐 · annual_base_cny_99（唯一套餐，不可调整）';
        body.appendChild(PC.fieldWrap('套餐', false, planField));

        var statusSelect = document.createElement('select'); statusSelect.className = 'select'; statusSelect.id = 'adjust-status';
        Object.keys(STATUS_LABEL).forEach(function (value) {
          var opt = document.createElement('option'); opt.value = value; opt.textContent = STATUS_LABEL[value];
          if (value === subscription.status) { opt.selected = true; }
          statusSelect.appendChild(opt);
        });
        var grid = document.createElement('div'); grid.className = 'form-grid';
        grid.appendChild(PC.fieldWrap('状态', true, statusSelect));
        var endsInput = document.createElement('input'); endsInput.className = 'input'; endsInput.type = 'date'; endsInput.id = 'adjust-ends';
        endsInput.value = subscription.ends_at ? PC.dateOnly(subscription.ends_at) : '';
        grid.appendChild(PC.fieldWrap('服务期止', true, endsInput));
        body.appendChild(grid);

        var updateDiff = function () {
          var existing = body.querySelector('.diff-table-wrap');
          if (existing) { existing.remove(); }
          var wrap = diffTable([
            ['plan_code', 'annual_base_cny_99', 'annual_base_cny_99', false],
            ['状态', STATUS_LABEL[subscription.status], STATUS_LABEL[statusSelect.value] || statusSelect.value, statusSelect.value !== subscription.status],
            ['revision', subscription.revision, subscription.revision + 1, true],
            ['服务期止', PC.date(subscription.ends_at), endsInput.value ? PC.date(endsInput.value + 'T23:59:59+08:00') : '—', true],
            ['renewal_count', subscription.renewal_count, subscription.renewal_count, false],
            ['存储配额', '5 GiB（随套餐固定，不可调整）', '5 GiB（随套餐固定，不可调整）', false]
          ]);
          wrap.classList.add('diff-table-wrap');
          body.appendChild(wrap);
        };
        statusSelect.addEventListener('change', updateDiff);
        endsInput.addEventListener('change', updateDiff);
        updateDiff();
        body.appendChild(alertBox('warning', null, '此处调整不产生计费、不改变存储配额；超额或定制合同费用一律通过手工账本登记。'));
      },
      continueLabel: '复核差异，继续',
      reasonCodes: [
        { value: 'goodwill_extension', label: '服务补偿' },
        { value: 'contract_amendment', label: '合同变更' },
        { value: 'data_correction', label: '数据更正' }
      ],
      idempotencyPrefix: 'subscription-adjust',
      auditAction: 'platform.subscription.adjusted',
      submitLabel: '提交调整',
      onSubmit: function (result) {
        var status = PC.el('adjust-status').value;
        var endsValue = PC.el('adjust-ends').value;
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenant.tenant_id) + '/subscription', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({
            plan_code: 'annual_base_cny_99',
            status: status,
            // ManualSubscriptionUpdateIn requires starts_at even though
            // this modal never lets the operator change it — resend the
            // existing value unchanged.
            starts_at: subscription.starts_at,
            ends_at: endsValue ? endsValue + 'T23:59:59+08:00' : subscription.ends_at,
            // reason_code/note: sent per the assumed future contract (Spec
            // §6 gap "受控操作的原因字段") — the current schema doesn't
            // persist them yet, the mutation itself still applies.
            reason_code: result.reason_code,
            reason_note: result.note
          })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  // --------------------------------------------------- 10c ledger create/edit
  function ledgerEntry(context, opts) {
    // context: { mode: 'create'|'edit', tenant: {tenant_id,tenant_name} | null
    //            (null => cross-tenant page, needs a tenant picker),
    //            tenantOptions: [...] (only for create+null tenant),
    //            entry: existing entry when mode === 'edit' }
    var editing = context.mode === 'edit';
    PC.openModal({
      title: (editing ? '编辑账本条目' : '新增账本条目') + (context.tenant ? ' · ' + context.tenant.tenant_name : ''),
      danger: false,
      renderImpact: function (body) {
        var grid = document.createElement('div'); grid.className = 'form-grid';
        var tenantSelect = null;
        if (!context.tenant) {
          tenantSelect = document.createElement('select'); tenantSelect.className = 'select'; tenantSelect.id = 'ledger-tenant';
          (context.tenantOptions || []).forEach(function (t) {
            var opt = document.createElement('option'); opt.value = t.tenant_id; opt.textContent = t.tenant_name; tenantSelect.appendChild(opt);
          });
          grid.appendChild(PC.fieldWrap('客户', true, tenantSelect));
        }
        var kindSelect = document.createElement('select'); kindSelect.className = 'select'; kindSelect.id = 'ledger-kind';
        kindSelect.disabled = editing;
        [['receipt', '收款'], ['refund', '退款']].forEach(function (pair) {
          var opt = document.createElement('option'); opt.value = pair[0]; opt.textContent = pair[1]; kindSelect.appendChild(opt);
        });
        if (editing) { kindSelect.value = context.entry.kind; }
        grid.appendChild(PC.fieldWrap('类型', true, kindSelect, editing ? '创建后不可编辑，需作废后重建。' : null));

        var amountWrap = document.createElement('div'); amountWrap.className = 'input-affix';
        var affix = document.createElement('span'); affix.className = 'affix'; affix.textContent = '¥';
        var amountInput = document.createElement('input'); amountInput.className = 'input mono'; amountInput.type = 'number'; amountInput.step = '0.01'; amountInput.min = '0.01'; amountInput.id = 'ledger-amount';
        amountInput.disabled = editing;
        if (editing) { amountInput.value = (context.entry.amount_cents / 100).toFixed(2); }
        amountWrap.appendChild(affix); amountWrap.appendChild(amountInput);
        grid.appendChild(PC.fieldWrap('金额（元）', true, amountWrap, editing ? '创建后不可编辑。' : '按分存储。'));

        var occurredInput = document.createElement('input'); occurredInput.className = 'input'; occurredInput.type = 'datetime-local'; occurredInput.id = 'ledger-occurred';
        if (editing && context.entry.occurred_at) { occurredInput.value = context.entry.occurred_at.slice(0, 16); }
        grid.appendChild(PC.fieldWrap('发生时间', true, occurredInput, '不晚于当前时间。'));
        body.appendChild(grid);

        var refInput = document.createElement('input'); refInput.className = 'input mono'; refInput.id = 'ledger-reference'; refInput.maxLength = 128;
        if (editing) { refInput.value = context.entry.reference || ''; }
        body.appendChild(PC.fieldWrap('参考号', false, refInput, '≤128 字符，展示时脱敏（如 PO-2026****4471）。'));

        var noteInput = document.createElement('textarea'); noteInput.className = 'textarea'; noteInput.id = 'ledger-note'; noteInput.maxLength = 1000;
        if (editing) { noteInput.value = context.entry.note || ''; }
        body.appendChild(PC.fieldWrap('备注', true, noteInput, '≤1000 字符，不要填写付款人个人信息。'));
      },
      continueLabel: '复核，继续',
      reasonCodes: [
        { value: 'offline_remittance', label: '线下汇款' },
        { value: 'contract_amendment', label: '合同变更' },
        { value: 'data_correction', label: '数据更正' }
      ],
      idempotencyPrefix: editing ? 'ledger-update' : 'ledger-create',
      auditAction: editing ? 'platform.ledger.entry.updated' : 'platform.ledger.entry.created',
      submitLabel: '提交条目',
      onSubmit: function (result) {
        var tenantId = context.tenant ? context.tenant.tenant_id : PC.el('ledger-tenant').value;
        var occurred = PC.el('ledger-occurred').value;
        var occurredIso = occurred ? occurred + ':00+08:00' : null;
        var headers = { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey };
        if (editing) {
          return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenantId) + '/financial-transactions/' + encodeURIComponent(context.entry.transaction_id), {
            method: 'PUT',
            headers: headers,
            body: JSON.stringify({
              occurred_at: occurredIso,
              reference: PC.el('ledger-reference').value || null,
              note: PC.el('ledger-note').value,
              reason_code: result.reason_code
            })
          }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
        }
        var amount = Math.round(Number(PC.el('ledger-amount').value || 0) * 100);
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenantId) + '/financial-transactions', {
          method: 'POST',
          headers: headers,
          body: JSON.stringify({
            kind: PC.el('ledger-kind').value,
            amount_cents: amount,
            occurred_at: occurredIso,
            reference: PC.el('ledger-reference').value || null,
            note: PC.el('ledger-note').value,
            reason_code: result.reason_code
          })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  // -------------------------------------------------------------- 10d void
  function voidLedgerEntry(entry, tenant, opts) {
    PC.openModal({
      title: '作废账本条目',
      danger: true,
      renderImpact: function (body) {
        body.appendChild(kv([
          ['条目', entry.transaction_id],
          ['客户', tenant ? tenant.tenant_name : entry.tenant_name],
          ['类型 / 金额', (entry.kind === 'receipt' ? '收款' : '退款') + ' · ' + PC.money(entry.amount_cents)],
          ['发生时间', PC.date(entry.occurred_at)],
          ['参考号', entry.reference_masked || '—']
        ]));
        body.appendChild(diffTable([
          ['条目状态', 'posted', 'voided', true],
          ['手工收款合计', '包含此条目', '扣减此条目金额', true],
          ['原条目记录', '保留', '保留（不可硬删除）', false],
          ['渠道净收入', '不受影响', '不受影响', false]
        ]));
        body.appendChild(alertBox('warning', null, '作废不会删除原条目——手工账本是不可变审计链，硬删除会破坏审计追溯。'));
      },
      reasonCodes: [
        { value: 'data_correction', label: '录入错误' },
        { value: 'duplicate_entry', label: '重复登记' },
        { value: 'contract_amendment', label: '合同变更' }
      ],
      unlock: { label: '条目 ID', match: entry.transaction_id },
      idempotencyPrefix: 'ledger-void',
      auditAction: 'platform.ledger.entry.voided',
      submitLabel: '确认作废',
      onSubmit: function (result) {
        var tenantId = tenant ? tenant.tenant_id : entry.tenant_id;
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenantId) + '/financial-transactions/' + encodeURIComponent(entry.transaction_id) + '/void', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({ reason_code: result.reason_code, note: result.note, confirmation: result.confirmation })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  // ------------------------------------------------------------- 10e refund
  function submitRefund(tenant, order, opts) {
    PC.openModal({
      title: '提交全额退款 · ' + tenant.tenant_name,
      danger: true,
      renderImpact: function (body) {
        body.appendChild(kv([
          ['客户', tenant.tenant_name],
          ['支付订单', (order.provider_transaction_ref_masked || order.provider_order_ref_masked) + ' · ' + PC.date(order.paid_at || order.created_at)],
          ['退款金额', PC.money(order.amount_cents) + '（全额，v1 不支持部分退款）'],
          ['渠道', '微信支付 · 原路退回'],
          ['预计到账', '1–3 个工作日']
        ]));
        body.appendChild(alertBox('danger', '不可撤销。', '提交后将减少渠道净收入，且不会自动调整该客户的订阅——如需同时调整订阅，请另行使用「调整订阅」。'));
      },
      reasonCodes: [
        { value: 'customer_request', label: '客户要求' },
        { value: 'service_failure', label: '服务未交付' },
        { value: 'duplicate_payment', label: '重复支付' },
        { value: 'contract_termination', label: '合同终止' }
      ],
      unlock: { label: '客户 slug', match: tenant.tenant_slug },
      idempotencyPrefix: 'refund-submit',
      auditAction: 'platform.refund.submitted',
      submitLabel: '确认提交退款',
      onSubmit: function (result) {
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenant.tenant_id) + '/refunds', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({ payment_order_id: order.order_id, reason_code: result.reason_code, confirmation: result.confirmation })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  // --------------------------------------------------- 10f query payment/refund
  function queryPayment(tenant, order, opts) {
    PC.openModal({
      title: '查询并恢复支付 / 激活',
      danger: false,
      singleStep: true,
      renderImpact: function (body) {
        body.appendChild(kv([
          ['支付订单', order.plan_name + ' · ' + PC.money(order.amount_cents)],
          ['当前状态', order.status],
          ['操作', '向微信支付重新查询订单，若已支付则补激活。']
        ]));
        body.appendChild(alertBox('neutral', null, '只读查询 + 幂等补激活，重复提交不会重复执行。'));
      },
      reasonCodes: [
        { value: 'activation_recovery', label: '激活补偿' },
        { value: 'provider_reconciliation', label: '渠道对账' }
      ],
      noteRequired: false,
      slugConfirm: { match: tenant.tenant_slug },
      idempotencyPrefix: 'payment-query',
      auditAction: 'platform.payment.queried',
      submitLabel: '查询并恢复',
      onSubmit: function (result) {
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenant.tenant_id) + '/payments/' + encodeURIComponent(order.order_id) + '/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({ reason_code: result.reason_code, confirmation: result.confirmation })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  function queryRefund(tenant, refund, opts) {
    PC.openModal({
      title: '查询退款状态',
      danger: false,
      singleStep: true,
      renderImpact: function (body) {
        body.appendChild(kv([
          ['退款', PC.money(refund.amount_cents) + ' · ' + PC.date(refund.requested_at)],
          ['当前状态', refund.status],
          ['操作', '向微信支付重新查询退款状态并同步。']
        ]));
        body.appendChild(alertBox('neutral', null, '只读查询 + 幂等同步，重复提交不会重复执行。'));
      },
      reasonCodes: [
        { value: 'provider_reconciliation', label: '渠道对账' },
        { value: 'customer_inquiry', label: '客户咨询' }
      ],
      noteRequired: false,
      slugConfirm: { match: tenant.tenant_slug },
      idempotencyPrefix: 'refund-query',
      auditAction: 'platform.refund.queried',
      submitLabel: '查询状态',
      onSubmit: function (result) {
        return PC.request('/api/platform/operations/tenants/' + encodeURIComponent(tenant.tenant_id) + '/refunds/' + encodeURIComponent(refund.refund_id) + '/query', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': result.idempotencyKey },
          body: JSON.stringify({ reason_code: result.reason_code, confirmation: result.confirmation })
        }).then(function (out) { if (opts && opts.onDone) { opts.onDone(out); } return out; });
      }
    });
  }

  window.PC.ops = {
    suspendResume: suspendResume,
    adjustSubscription: adjustSubscription,
    ledgerEntry: ledgerEntry,
    voidLedgerEntry: voidLedgerEntry,
    submitRefund: submitRefund,
    queryPayment: queryPayment,
    queryRefund: queryRefund,
    kv: kv,
    diffTable: diffTable,
    alertBox: alertBox
  };
}());
