/* Paid tenant branding settings (RND-259). Server capability checks remain
 * authoritative; this file only presents the returned entitlement state. */
(function () {
  'use strict';

  var state = null;
  var message = document.getElementById('branding-message');
  var verification = document.getElementById('branding-verification');

  function node(id) { return document.getElementById(id); }
  function show(text, error) {
    if (!message) return;
    message.textContent = text || '';
    message.className = error ? 'field-error' : 'field-help';
  }
  function request(url, options) {
    options = options || {};
    options.credentials = 'include';
    return fetch(url, options).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || 'branding_request_failed');
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }
  function setDisabled(id, disabled) {
    var element = node(id);
    if (element) element.disabled = disabled;
  }
  function draw(data) {
    state = data;
    node('branding-logo-state').textContent = data.logo_configured ? '已配置' : '使用平台默认';
    node('branding-favicon-state').textContent = data.favicon_configured ? '已配置' : '未上传（使用平台默认）';
    node('branding-domain-state').textContent = data.custom_domain || '未配置';
    node('branding-certificate-state').textContent = data.certificate_status || '未开始';
    node('branding-upgrade').hidden = !(data.upgrade_required);
    node('branding-domain-input').value = data.custom_domain || '';

    var brandingLocked = !data.custom_branding_entitled;
    var domainLocked = !data.custom_domain_entitled;
    ['branding-logo-input', 'branding-logo-upload', 'branding-logo-reset',
      'branding-favicon-input', 'branding-favicon-upload', 'branding-favicon-reset'].forEach(function (id) {
      setDisabled(id, brandingLocked);
    });
    ['branding-domain-input', 'branding-domain-save', 'branding-domain-verify',
      'branding-domain-enable', 'branding-domain-disable', 'branding-domain-unbind'].forEach(function (id) {
      setDisabled(id, domainLocked);
    });
    var preview = node('branding-logo-preview');
    if (preview) {
      preview.style.display = '';
      preview.src = '/api/branding/logo?_=' + Date.now();
    }
  }
  function refresh(keepMessage) {
    return request('/api/branding').then(function (data) {
      draw(data);
      if (!keepMessage) show('', false);
      return data;
    }).catch(function () {
      show('无法读取品牌配置，请刷新页面后重试。', true);
    });
  }
  function showError(error) {
    var known = {
      custom_branding_upgrade_required: '当前套餐不包含自定义品牌能力，请升级后再试。',
      custom_domain_upgrade_required: '当前套餐不包含自有域名能力，请升级后再试。',
      custom_domain_already_bound: '该域名已被其他租户绑定。',
      custom_domain_certificate_not_ready: '域名尚未完成 TLS 签发或证书检查，暂不能启用。',
      custom_domain_not_verified: '请先完成 DNS 所有权验证。',
      dns_record_not_found: '未检测到匹配的 TXT 验证记录。',
      dns_unavailable: '暂时无法查询 DNS，请稍后重试。'
    };
    show(known[error.message] || '操作未完成，请检查配置后重试。', true);
  }
  function upload(kind) {
    var input = node('branding-' + kind + '-input');
    var file = input && input.files && input.files[0];
    if (!file) {
      show('请先选择图片文件。', true);
      return;
    }
    show('正在上传…', false);
    request('/api/branding/' + kind, {
      method: 'PUT', headers: {'Content-Type': file.type || 'application/octet-stream'}, body: file
    }).then(function (data) {
      input.value = '';
      draw(data);
      show(kind === 'favicon' ? 'Favicon 已保存。' : 'Logo 已保存。', false);
    }).catch(showError);
  }
  function restore(kind) {
    show('正在恢复平台默认…', false);
    request('/api/branding/' + kind, {method: 'DELETE'}).then(function (data) {
      draw(data);
      show(kind === 'favicon' ? '已恢复平台默认 Favicon。' : '已恢复平台默认 Logo。', false);
    }).catch(showError);
  }
  function bind(id, callback) {
    var element = node(id);
    if (element) element.addEventListener('click', callback);
  }
  function runDomain(path, method) {
    show('正在更新域名状态…', false);
    request('/api/branding/domain' + path, {method: method || 'POST'}).then(function (data) {
      draw(data);
      show('域名状态已更新。', false);
    }).catch(showError);
  }

  bind('branding-logo-upload', function () { upload('logo'); });
  bind('branding-logo-reset', function () { restore('logo'); });
  bind('branding-favicon-upload', function () { upload('favicon'); });
  bind('branding-favicon-reset', function () { restore('favicon'); });
  bind('branding-domain-save', function () {
    var hostname = node('branding-domain-input').value.trim();
    if (!hostname) { show('请输入完整主机名。', true); return; }
    show('正在生成 DNS 验证记录…', false);
    request('/api/branding/domain', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({hostname: hostname})
    }).then(function (data) {
      draw(data);
      // This is the one controlled display of the raw DNS value. It is never
      // persisted in browser storage, an audit record, or a subsequent GET.
      verification.hidden = false;
      verification.textContent = '请在 DNS 中添加 TXT 记录：\n名称：' + data.verification_record_name + '\n值：' + data.verification_token;
      show('请保存 TXT 记录后再检查 DNS 验证。', false);
    }).catch(showError);
  });
  bind('branding-domain-verify', function () { runDomain('/verify'); });
  bind('branding-domain-enable', function () { runDomain('/enable'); });
  bind('branding-domain-disable', function () { runDomain('/disable'); });
  bind('branding-domain-unbind', function () {
    if (!window.confirm('解绑后该自有域名将立即停止服务，是否继续？')) return;
    runDomain('', 'DELETE');
  });

  if (node('settings-section-branding')) refresh(false);
}());
