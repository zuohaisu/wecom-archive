(function () {
  'use strict';

  window.doLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).finally(function () {
      window.location = '/admin/login';
    });
  };

  function t(key, fallback) {
    if (!window.I18N) return fallback;
    var translated = I18N.t(key);
    return translated === key && fallback !== undefined ? fallback : translated;
  }

  function applyI18n() {
    if (!window.I18N) return;
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = I18N.t(element.getAttribute('data-i18n'));
    });
  }

  function makeElement(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  var SECRET_KEYS = {
    archive_secret: true,
    private_key: true,
    callback_token: true,
    callback_encoding_aes_key: true
  };

  var state = {
    snapshot: null,
    testResult: null
  };

  function showError(key) {
    var box = document.getElementById('provisioning-error');
    box.textContent = t(key);
    box.hidden = false;
  }

  function hideError() {
    document.getElementById('provisioning-error').hidden = true;
  }

  function badgeForStatus(status) {
    var badge = makeElement('span', 'badge');
    if (status === 'set') badge.className += ' badge-success';
    if (status === 'missing') badge.className += ' badge-warning';
    if (status === 'unreadable') badge.className += ' badge-danger';
    badge.textContent = t('provisioning.status.' + status, status);
    return badge;
  }

  function secretControl(key, maskValue) {
    var control = makeElement('div', 'form-row');
    var input = makeElement('input', 'input');
    input.type = 'password';
    input.name = key;
    input.autocomplete = 'new-password';
    input.placeholder = t('provisioning.field.' + key + '.placeholder', '');
    input.value = maskValue || '';
    input.dataset.secretDirty = 'false';
    input.addEventListener('input', function () {
      input.dataset.secretDirty = 'true';
    });

    var reveal = makeElement('button', 'btn btn-sm', t('settings.secret.showDetails', 'Show masked details'));
    reveal.type = 'button';
    reveal.setAttribute('aria-pressed', 'false');
    reveal.addEventListener('click', function () {
      var visible = input.type === 'text';
      // GET never contains plaintext: this only reveals the server-provided mask.
      input.type = visible ? 'password' : 'text';
      reveal.setAttribute('aria-pressed', String(!visible));
      reveal.textContent = t(
        visible ? 'settings.secret.showDetails' : 'settings.secret.hideDetails',
        visible ? 'Show masked details' : 'Hide masked details'
      );
    });

    control.appendChild(input);
    control.appendChild(reveal);
    return control;
  }

  function numberControl(key, value) {
    var input = makeElement('input', 'input');
    input.type = 'number';
    input.min = '1';
    input.step = '1';
    input.name = key;
    input.value = value === null || value === undefined ? '' : String(value);
    input.dataset.dirty = 'false';
    input.addEventListener('input', function () {
      input.dataset.dirty = 'true';
    });
    return input;
  }

  function renderFields(fields) {
    Object.keys(fields).forEach(function (key) {
      var field = fields[key];
      var status = document.querySelector('[data-field-status="' + key + '"]');
      if (status) {
        status.textContent = '';
        status.className = 'badge';
        status.appendChild(badgeForStatus(field.status));
      }
      var controlSlot = document.querySelector('[data-field-control="' + key + '"]');
      if (!controlSlot) return;
      controlSlot.textContent = '';
      if (SECRET_KEYS[key]) {
        controlSlot.appendChild(secretControl(key, field.mask || ''));
      } else {
        controlSlot.appendChild(numberControl(key, field.value));
      }
    });
  }

  function renderMissing(missing) {
    var banner = document.getElementById('missing-banner');
    var list = document.getElementById('missing-list');
    list.textContent = '';
    (missing || []).forEach(function (key) {
      list.appendChild(makeElement('li', null, t('provisioning.field.' + key + '.label', key)));
    });
    banner.hidden = !missing || missing.length === 0;
  }

  function renderOrg(org) {
    document.getElementById('org-name').textContent = org.corp_name || '—';
    document.getElementById('org-corpid').textContent = org.corp_id || '—';
    document.getElementById('org-agentid').textContent = org.agent_id || '—';
  }

  function renderCallbackUrl(url) {
    var display = url || window.location.origin + '/api/wecom/archive/events';
    document.getElementById('callback-url').textContent = display;
  }

  function renderSnapshot(snapshot) {
    state.snapshot = snapshot;
    renderOrg(snapshot.org || {});
    renderCallbackUrl(snapshot.callback_url);
    renderFields(snapshot.fields || {});
    renderMissing(snapshot.missing || []);
  }

  function renderFieldErrors(errors) {
    var result = document.getElementById('wizard-result');
    result.hidden = false;
    result.className = 'alert alert-danger';
    result.textContent = '';
    (errors || []).forEach(function (error) {
      result.appendChild(
        makeElement(
          'p',
          null,
          t('provisioning.error.field', 'Invalid field') + ' ' + error.key + ' (' + error.code + ')'
        )
      );
    });
  }

  function collectUpdates() {
    var updates = {};
    var form = document.getElementById('fields-form');
    Array.prototype.slice.call(form.querySelectorAll('input')).forEach(function (input) {
      var dirty = input.dataset.secretDirty === 'true' || input.dataset.dirty === 'true';
      if (!dirty) return;
      var value = input.value;
      if (value === null || value === undefined || String(value).trim() === '') return;
      updates[input.name] = input.type === 'number' ? parseInt(value, 10) : value;
    });
    return updates;
  }

  function renderTestResult(result) {
    state.testResult = result;
    var box = document.getElementById('wizard-result');
    box.hidden = false;
    box.textContent = '';
    box.className = 'alert ' + (result.all_ok ? 'alert-success' : 'alert-warning');
    if (result.all_ok) {
      box.appendChild(makeElement('p', null, t('provisioning.testAllOk', 'All checks passed.')));
      return;
    }
    Object.keys(result.fields || {}).forEach(function (key) {
      var field = result.fields[key];
      if (field.ok) return;
      var label = t('provisioning.field.' + key + '.label', key);
      var code = field.safe_error_code || 'unknown';
      box.appendChild(
        makeElement('p', null, label + ': ' + t('provisioning.error.' + code, code))
      );
    });
  }

  function load() {
    hideError();
    fetch('/api/provisioning/config', { credentials: 'include' })
      .then(function (response) {
        if (response.status === 401) {
          window.location = '/admin/login';
          return null;
        }
        return response.json();
      })
      .then(function (data) {
        if (!data) return;
        renderSnapshot(data);
        applyI18n();
      })
      .catch(function () {
        showError('provisioning.error.load');
      });
  }

  document.getElementById('save-config').addEventListener('click', function () {
    hideError();
    var updates = collectUpdates();
    var button = document.getElementById('save-config');
    button.disabled = true;
    button.textContent = t('provisioning.saving', 'Saving…');
    fetch('/api/provisioning/config', {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(updates)
    })
      .then(function (response) {
        return response.json().then(function (data) {
          return { status: response.status, data: data };
        });
      })
      .then(function (result) {
        button.disabled = false;
        button.textContent = t('provisioning.save', 'Save');
        if (result.status === 200) {
          renderSnapshot(result.data);
          var box = document.getElementById('wizard-result');
          box.hidden = false;
          box.className = 'alert alert-success';
          box.textContent = t('provisioning.saved', 'Configuration saved.');
        } else if (result.status === 400) {
          renderFieldErrors(result.data.errors);
        } else {
          showError('provisioning.error.save');
        }
      })
      .catch(function () {
        button.disabled = false;
        button.textContent = t('provisioning.save', 'Save');
        showError('provisioning.error.save');
      });
  });

  document.getElementById('test-config').addEventListener('click', function () {
    hideError();
    var button = document.getElementById('test-config');
    button.disabled = true;
    button.textContent = t('provisioning.testing', 'Testing…');
    fetch('/api/provisioning/config/test', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    })
      .then(function (response) {
        return response.json();
      })
      .then(function (data) {
        button.disabled = false;
        button.textContent = t('provisioning.test', 'Test connection');
        renderTestResult(data);
      })
      .catch(function () {
        button.disabled = false;
        button.textContent = t('provisioning.test', 'Test connection');
        showError('provisioning.error.test');
      });
  });

  document.getElementById('copy-callback-url').addEventListener('click', function () {
    var button = document.getElementById('copy-callback-url');
    var value = document.getElementById('callback-url').textContent;
    var done = function () {
      button.textContent = t('provisioning.callbackUrl.copied', 'Copied');
      window.setTimeout(function () {
        button.textContent = t('provisioning.callbackUrl.copy', 'Copy');
      }, 1500);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done).catch(function () {});
      return;
    }
    done();
  });

  if (window.I18N) I18N.onChange(applyI18n);
  load();
})();
