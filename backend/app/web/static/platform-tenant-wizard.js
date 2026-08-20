/* RND-414: /platform/tenants/new — 4-step manual tenant-provisioning
 * wizard. Submits to the real POST /api/platform/tenants endpoint
 * (backend/app/routers/platform.py, TenantProvisionIn/Out) — this is NOT a
 * gap-backed placeholder, tenant creation already works end to end.
 * secret/private_key_pem never leave step 2's inputs except in the one
 * POST body; step 3's review shows only "已录入 · 不展示" for both. */
(function () {
  'use strict';
  var PC = window.PC;
  var current = 1;

  function panel(step) { return PC.el('wizard-panel-' + step); }
  function goto(step) {
    for (var i = 1; i <= 4; i += 1) { panel(i).hidden = i !== step; }
    var items = PC.el('wizard-steps').querySelectorAll('li');
    items.forEach(function (item) {
      var n = Number(item.getAttribute('data-step'));
      item.classList.toggle('current', n === step);
      item.classList.toggle('done', n < step);
    });
    current = step;
    PC.error('');
  }

  function validateStep1() {
    var required = ['w-name', 'w-slug', 'w-admin-email', 'w-owner-email', 'w-reason'];
    for (var i = 0; i < required.length; i += 1) {
      if (!PC.el(required[i]).value.trim()) { PC.error('请完整填写第 1 步的必填字段。'); return false; }
    }
    if (!/^[a-z][a-z0-9-]*$/.test(PC.el('w-slug').value.trim())) { PC.error('slug 仅支持小写字母、数字与连字符，且以字母开头。'); return false; }
    return true;
  }
  function validateStep2() {
    if (!PC.el('w-corp-id').value.trim() || !PC.el('w-agent-id').value.trim()) { PC.error('请填写 corp_id 与 agent_id。'); return false; }
    if (!PC.el('w-secret').value) { PC.error('请粘贴 secret。'); return false; }
    if (!PC.el('w-private-key').value.trim()) { PC.error('请粘贴 private_key_pem。'); return false; }
    if (!PC.el('w-credential-confirm').checked) { PC.error('请确认凭据来源与操作环境后再继续。'); return false; }
    return true;
  }

  function fieldValues() {
    return {
      name: PC.el('w-name').value.trim(),
      slug: PC.el('w-slug').value.trim(),
      admin_email: PC.el('w-admin-email').value.trim(),
      owner_email: PC.el('w-owner-email').value.trim(),
      callback_domain: PC.el('w-callback-domain').value.trim(),
      reason: PC.el('w-reason').value,
      corp_id: PC.el('w-corp-id').value.trim(),
      agent_id: PC.el('w-agent-id').value.trim(),
      secret: PC.el('w-secret').value,
      private_key_pem: PC.el('w-private-key').value.trim()
    };
  }

  var REASON_LABEL = { manual_provisioning: '自助开通失败', contract_direct: '合同直签', migration: '存量迁移' };

  function renderReview() {
    var values = fieldValues();
    var dl = PC.el('w-review');
    PC.clear(dl);
    [
      ['租户名称', values.name], ['slug', values.slug], ['管理员邮箱', values.admin_email],
      ['Owner 邮箱', values.owner_email], ['回调域名', values.callback_domain || '（使用平台默认域名）'],
      ['开通原因', REASON_LABEL[values.reason] || values.reason],
      ['corp_id', values.corp_id], ['agent_id', values.agent_id],
      ['secret', '已录入 · 不展示'], ['private_key_pem', '已录入 · 不展示'],
      ['幂等键前缀', 'tenant-create-…']
    ].forEach(function (pair) {
      var dt = document.createElement('dt'); dt.textContent = pair[0];
      var dd = document.createElement('dd'); dd.textContent = pair[1];
      dl.appendChild(dt); dl.appendChild(dd);
    });
  }

  function submitCreate() {
    var values = fieldValues();
    var submit = PC.el('w-step3-submit');
    submit.disabled = true;
    PC.error('');
    PC.request('/api/platform/tenants', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': PC.operationKey('tenant-create') },
      body: JSON.stringify({
        name: values.name,
        slug: values.slug,
        admin_email: values.admin_email,
        owner_email: values.owner_email,
        corp_id: values.corp_id,
        agent_id: values.agent_id,
        secret: values.secret,
        private_key_pem: values.private_key_pem,
        callback_domain: values.callback_domain,
        // Not part of TenantProvisionIn yet — sent per the assumed future
        // audit-reason contract (same pattern as the subscription/ledger
        // modals); the create itself succeeds regardless.
        provisioning_reason: values.reason
      })
    }).then(function (result) {
      renderResult(result);
      goto(4);
    }).catch(function (err) {
      submit.disabled = false;
      PC.error(err.message || '创建失败，请检查后重试。');
    });
  }

  function renderResult(result) {
    PC.el('w-result-summary').textContent = (result.tenant_name || '租户') + ' 已创建。凭据已加密存储，本页不再展示。';
    var dl = PC.el('w-result');
    PC.clear(dl);
    [
      ['tenant_id', result.tenant_id],
      ['is_active', result.is_active ? '是' : '否'],
      ['owner_invite_sent', (result.owner_invite_sent ? '是' : '否') + '（' + (result.owner_email || PC.el('w-owner-email').value) + '）'],
      ['config_id', result.config_id]
    ].forEach(function (pair) {
      var dt = document.createElement('dt'); dt.textContent = pair[0];
      var dd = document.createElement('dd'); dd.className = 'mono'; dd.textContent = pair[1] === undefined || pair[1] === null ? '—' : String(pair[1]);
      dl.appendChild(dt); dl.appendChild(dd);
    });
    var link = PC.el('w-goto-tenant');
    link.href = '/platform/tenants/' + encodeURIComponent(result.tenant_id);
    PC.el('w-run-connectivity').addEventListener('click', function () {
      var button = PC.el('w-run-connectivity');
      button.disabled = true;
      PC.request('/api/platform/tenants/' + encodeURIComponent(result.tenant_id) + '/connectivity-check', { method: 'POST' })
        .then(function (check) {
          button.disabled = false;
          button.textContent = check.ok ? '连通性检查通过 ✓' : '连通性检查失败' + (check.reason ? '（' + check.reason + '）' : '');
        })
        .catch(function (err) { button.disabled = false; PC.error(err.message || '连通性检查失败。'); });
    });
  }

  PC.el('w-step1-next').addEventListener('click', function () { if (validateStep1()) { goto(2); } });
  PC.el('w-step2-back').addEventListener('click', function () { goto(1); });
  PC.el('w-step2-next').addEventListener('click', function () { if (validateStep2()) { renderReview(); goto(3); } });
  PC.el('w-step3-back').addEventListener('click', function () { goto(2); });
  PC.el('w-step3-submit').addEventListener('click', submitCreate);

  goto(1);
}());
