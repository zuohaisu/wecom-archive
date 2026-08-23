/* RND-415: consume a one-time platform operator activation link. */
(function () {
  'use strict';
  var token = new URLSearchParams(window.location.search).get('token');
  var form = document.getElementById('activation-form');
  var error = document.getElementById('activation-error');
  var success = document.getElementById('activation-success');
  var submit = document.getElementById('activation-submit');
  var MESSAGES = {
    invalid_or_expired_invitation: '邀请链接无效、已使用或已过期。请联系现有平台操作员重新邀请。',
    password_confirmation_mismatch: '两次输入的密码不一致。',
    weak_password: '密码至少 12 位，且必须包含大写字母、小写字母和数字。',
    platform_audit_unavailable: '审计服务暂不可用，账户未激活。',
    platform_invitation_accept_failed: '激活失败，请稍后重试。'
  };

  // Do not leave a bearer token in the browser history, referrer copied by a
  // user, or screenshot after the page has loaded. The server access logger
  // separately redacts this route's query string.
  window.history.replaceState({}, document.title, '/platform/accept-invite');

  function showError(message) {
    error.hidden = !message;
    error.textContent = message || '';
  }

  if (!token) {
    showError(MESSAGES.invalid_or_expired_invitation);
    submit.disabled = true;
    return;
  }

  form.addEventListener('submit', function (event) {
    event.preventDefault();
    showError('');
    var password = document.getElementById('activation-password').value;
    var confirmation = document.getElementById('activation-password-confirmation').value;
    if (password !== confirmation) {
      showError(MESSAGES.password_confirmation_mismatch);
      return;
    }
    submit.disabled = true;
    fetch('/api/platform/operators/accept-invite', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, password: password, password_confirmation: confirmation })
    }).then(function (response) {
      if (response.ok) { return response.json(); }
      return response.json().catch(function () { return {}; }).then(function (body) {
        throw new Error(body.detail || 'activation_failed');
      });
    }).then(function () {
      form.hidden = true;
      success.hidden = false;
      // Clear transient plaintext from live DOM as well as the history.
      document.getElementById('activation-password').value = '';
      document.getElementById('activation-password-confirmation').value = '';
      token = null;
    }).catch(function (requestError) {
      submit.disabled = false;
      showError(MESSAGES[requestError.message] || '激活失败，请稍后重试。');
    });
  });
}());
