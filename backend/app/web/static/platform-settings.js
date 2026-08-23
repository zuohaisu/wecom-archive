/* RND-415: /platform/settings password rotation and operator directory. */
(function () {
  'use strict';
  var PC = window.PC;
  var passwordTab = PC.el('password-tab');
  var operatorsTab = PC.el('operators-tab');
  var passwordPanel = PC.el('password-panel');
  var operatorsPanel = PC.el('operators-panel');

  var MESSAGES = {
    invalid_current_password: '当前密码不正确。',
    password_confirmation_mismatch: '两次输入的新密码不一致。',
    weak_password: '新密码至少 12 位，且必须包含大写字母、小写字母和数字。',
    recent_password_reused: '新密码不能与最近 5 次使用过的密码相同。',
    platform_audit_unavailable: '审计服务暂不可用，未更新密码。',
    platform_password_change_failed: '密码更新失败，请稍后重试。'
  };

  function message(error) { return MESSAGES[error && error.message] || (error && error.message) || '请求失败，请稍后重试。'; }

  function activateTab(tab) {
    var passwords = tab === 'password';
    passwordTab.classList.toggle('active', passwords);
    passwordTab.setAttribute('aria-selected', passwords ? 'true' : 'false');
    operatorsTab.classList.toggle('active', !passwords);
    operatorsTab.setAttribute('aria-selected', passwords ? 'false' : 'true');
    passwordPanel.hidden = !passwords;
    operatorsPanel.hidden = passwords;
    PC.error('');
    if (!passwords) { loadOperators(); }
  }

  function labelForStatus(status) {
    return status === 'active' ? '已激活' : (status === 'pending' ? '待激活' : '已停用');
  }

  function loadOperators() {
    var rows = PC.el('operator-rows');
    PC.request('/api/platform/operators').then(function (data) {
      PC.clear(rows);
      if (!data.operators || !data.operators.length) {
        PC.emptyRow(rows, 5, '暂无平台操作员');
        return;
      }
      data.operators.forEach(function (operator) {
        var status = PC.badge(labelForStatus(operator.status), operator.status === 'active' ? 'success' : (operator.status === 'pending' ? 'pending' : 'danger'));
        rows.appendChild(PC.rowFrom([
          operator.name || '—', operator.email, operator.role, status, PC.date(operator.last_login_at)
        ]));
      });
    }).catch(function (error) { PC.error(message(error)); PC.emptyRow(rows, 5, '无法加载操作员列表'); });
  }

  function impactLine(container, title, description) {
    var section = document.createElement('div');
    var heading = document.createElement('strong');
    heading.textContent = title;
    var body = document.createElement('p');
    body.className = 'muted';
    body.textContent = description;
    section.appendChild(heading);
    section.appendChild(body);
    container.appendChild(section);
  }

  passwordTab.addEventListener('click', function () { activateTab('password'); });
  operatorsTab.addEventListener('click', function () { activateTab('operators'); });

  PC.el('password-form').addEventListener('submit', function (event) {
    event.preventDefault();
    PC.error('');
    PC.el('password-success').hidden = true;
    var currentPassword = PC.el('current-password').value;
    var newPassword = PC.el('new-password').value;
    var confirmation = PC.el('new-password-confirmation').value;
    var revoke = PC.el('revoke-other-sessions').checked;
    if (!currentPassword || !newPassword || !confirmation) {
      PC.error('请填写全部密码字段。');
      return;
    }
    if (newPassword !== confirmation) {
      PC.error('两次输入的新密码不一致。');
      return;
    }
    PC.openModal({
      title: '复核密码更新',
      danger: false,
      continueLabel: '继续安全复核',
      reasonCodes: [
        { value: 'routine_rotation', label: '例行轮换' },
        { value: 'suspected_exposure', label: '疑似泄露' },
        { value: 'security_maintenance', label: '安全维护' }
      ],
      notePlaceholder: '说明改密原因，写入审计。',
      noteLabel: '改密备注',
      idempotencyPrefix: 'platform-password',
      auditAction: 'platform.operator.password_changed',
      submitLabel: '确认更新密码',
      renderImpact: function (container) {
        impactLine(container, '密码', '新密码会按强度和最近 5 次密码历史校验，当前密码不会显示或写入审计。');
        impactLine(container, '会话', revoke ? '当前浏览器会话将保留，其他设备上的全部平台会话将被吊销。' : '其他设备上的平台会话将保持登录状态。');
      },
      onSubmit: function (review) {
        return PC.request('/api/platform/account/password', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_password: currentPassword,
            new_password: newPassword,
            new_password_confirmation: confirmation,
            revoke_other_sessions: revoke,
            reason_code: review.reason_code,
            note: review.note
          })
        }).then(function () {
          PC.el('password-form').reset();
          PC.el('revoke-other-sessions').checked = true;
          PC.el('password-success').hidden = false;
        }).catch(function (error) { throw new Error(message(error)); });
      }
    });
  });

  PC.wireRefreshStamp(function () {
    if (!operatorsPanel.hidden) { loadOperators(); }
  });
}());
