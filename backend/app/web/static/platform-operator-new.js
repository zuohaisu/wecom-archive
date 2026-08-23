/* RND-415: controlled platform-operator invitation wizard. */
(function () {
  'use strict';
  var PC = window.PC;
  var form = PC.el('operator-invite-form');
  var MESSAGES = {
    invalid_operator_name: '请填写有效的操作员姓名。',
    invalid_operator_email: '请填写有效的企业邮箱。',
    invalid_reason_code: '请选择原因代码。',
    invalid_audit_note: '请填写不超过 1000 字的备注。',
    operator_email_already_exists: '该邮箱已存在平台操作员账号或待激活邀请。',
    platform_invite_base_url_unavailable: '邀请链接域名未配置，未创建邀请。',
    platform_invitation_delivery_failed: '邀请邮件发送失败，未创建邀请。',
    platform_invitation_failed: '创建邀请失败，请稍后重试。',
    platform_audit_unavailable: '审计服务暂不可用，未创建邀请。'
  };

  function message(error) { return MESSAGES[error && error.message] || (error && error.message) || '请求失败，请稍后重试。'; }

  function appendReview(container, label, value) {
    var row = document.createElement('div');
    row.className = 'detail-grid';
    var cell = document.createElement('div');
    var title = document.createElement('span');
    var text = document.createElement('strong');
    title.textContent = label;
    text.textContent = value;
    cell.appendChild(title);
    cell.appendChild(text);
    row.appendChild(cell);
    container.appendChild(row);
  }

  function renderResult(result) {
    var details = PC.el('invite-result');
    PC.clear(details);
    [
      ['邮箱', result.email],
      ['角色', result.role],
      ['状态', result.status === 'pending' ? '待激活' : result.status],
      ['审计 ID', result.audit_id]
    ].forEach(function (pair) {
      var dt = document.createElement('dt');
      var dd = document.createElement('dd');
      dt.textContent = pair[0];
      dd.textContent = pair[1];
      if (pair[0] === '审计 ID') { dd.className = 'mono'; }
      details.appendChild(dt);
      details.appendChild(dd);
    });
    PC.el('invite-form-card').hidden = true;
    PC.el('invite-result-card').hidden = false;
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    PC.error('');
    var name = PC.el('operator-name').value.trim();
    var email = PC.el('operator-email').value.trim();
    if (!name || !email) {
      PC.error('请填写姓名和企业邮箱。');
      return;
    }
    PC.openModal({
      title: '复核新增操作员',
      danger: true,
      continueLabel: '已复核权限影响，继续',
      reasonCodes: [
        { value: 'staffing_change', label: '人员变动' },
        { value: 'access_expansion', label: '工作职责扩展' },
        { value: 'operator_replacement', label: '操作员替换' }
      ],
      noteLabel: '备注',
      notePlaceholder: '说明邀请原因，写入不可修改的审计记录。',
      idempotencyPrefix: 'platform-operator-invite',
      auditAction: 'platform.operator.invited',
      submitLabel: '发送激活邀请',
      renderImpact: function (container) {
        appendReview(container, '姓名', name);
        appendReview(container, '企业邮箱', email);
        appendReview(container, '权限', 'super_admin（完整平台读写权限）');
        appendReview(container, '激活方式', '邮件一次性链接，24 小时有效；管理员不设置或接触密码。');
      },
      onSubmit: function (review) {
        return PC.request('/api/platform/operators/invitations', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: name,
            email: email,
            reason_code: review.reason_code,
            note: review.note
          })
        }).then(renderResult).catch(function (error) { throw new Error(message(error)); });
      }
    });
  });

  PC.wireRefreshStamp();
}());
