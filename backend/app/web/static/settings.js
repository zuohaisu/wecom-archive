(function () {
  'use strict';

  function applyI18n() {
    if (!window.I18N) return;
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = I18N.t(element.getAttribute('data-i18n'));
    });
  }

  function initSettingsNavigation() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-settings-section]'));
    var panels = Array.prototype.slice.call(document.querySelectorAll('.settings-section'));

    function selectSection(name) {
      var panelId = 'settings-section-' + name;
      tabs.forEach(function (tab) {
        var selected = tab.getAttribute('data-settings-section') === name;
        tab.classList.toggle('active', selected);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.id !== panelId;
      });
    }

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        selectSection(tab.getAttribute('data-settings-section'));
      });
    });

    var activeTab = tabs.filter(function (tab) {
      return tab.getAttribute('aria-selected') === 'true';
    })[0];
    if (activeTab) selectSection(activeTab.getAttribute('data-settings-section'));
    applyI18n();
    if (window.I18N) I18N.onChange(applyI18n);
  }

  var CONFIG_GROUPS = {
    general: 'general',
    third_party: 'third-party',
    storage: 'storage',
    wecom: 'wecom',
    advanced: 'advanced'
  };
  var SECRET_KEYS = {
    smtp_password: true,
    qiniu_access_key: true,
    qiniu_secret_key: true,
    wecom_oauth_secret: true,
    wecom_callback_token: true,
    wecom_callback_encoding_aes_key: true
  };
  var INTEGER_KEYS = {
    smtp_port: true,
    media_thumbnail_max_edge: true,
    media_thumbnail_jpeg_quality: true,
    event_media_download_batch_limit: true,
    event_media_download_recent_window_hours: true,
    event_media_download_retry_count: true,
    event_media_download_backoff_seconds: true,
    event_media_download_sweep_interval_seconds: true
  };
  var BOOLEAN_KEYS = {
    media_thumbnail_enabled: true,
    voice_transcode_enabled: true,
    event_media_download_enabled: true
  };
  var REQUIRED_KEYS = {
    wecom_corp_id: true,
    wecom_agent_id: true,
    wecom_oauth_secret: true,
    wecom_callback_token: true,
    wecom_callback_encoding_aes_key: true
  };
  var CONNECTION_TARGETS = {general: 'domain', storage: 'qiniu', wecom: 'wecom'};

  function t(key, fallback) {
    if (!window.I18N) return fallback;
    var translated = I18N.t(key);
    return translated === key && fallback !== undefined ? fallback : translated;
  }

  function makeElement(tag, className, text) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function fieldLabel(key) {
    return t('settings.field.' + key, key.replace(/_/g, ' '));
  }

  function SourceBadge(source) {
    var className = 'badge';
    if (source === 'env') className += ' badge-info';
    if (source === 'db') className += ' badge-success';
    var badge = makeElement('span', className, t('settings.source.' + source, source));
    badge.setAttribute('data-settings-source', source);
    return badge;
  }

  function SecretField(field) {
    var control = makeElement('div', 'form-row');
    var input = makeElement('input', 'input');
    input.type = 'password';
    input.name = field.key;
    input.autocomplete = 'new-password';
    input.value = field.value || '';
    input.setAttribute('data-settings-key', field.key);
    input.setAttribute('data-secret-field', 'true');
    input.dataset.secretDirty = 'false';
    input.addEventListener('input', function () {
      input.dataset.secretDirty = 'true';
    });

    var reveal = makeElement('button', 'btn btn-sm', t('settings.secret.showDetails', 'Show masked details'));
    reveal.type = 'button';
    reveal.setAttribute('data-secret-toggle', field.key);
    reveal.setAttribute('aria-pressed', 'false');
    reveal.addEventListener('click', function () {
      var visible = input.type === 'text';
      // GET never contains plaintext: this only reveals the server-provided mask
      // ("****" plus, for long values, its final four characters).
      input.type = visible ? 'password' : 'text';
      reveal.setAttribute('aria-pressed', String(!visible));
      reveal.textContent = t(
        visible ? 'settings.secret.showDetails' : 'settings.secret.hideDetails',
        visible ? 'Show masked details' : 'Hide masked details'
      );
    });

    var copy = makeElement('button', 'btn btn-sm', t('settings.secret.copy', 'Copy'));
    copy.type = 'button';
    copy.setAttribute('data-secret-copy', field.key);
    copy.addEventListener('click', function () {
      var done = function () {
        copy.textContent = t('settings.secret.copied', 'Copied');
        window.setTimeout(function () {
          copy.textContent = t('settings.secret.copy', 'Copy');
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).then(done).catch(function () {});
        return;
      }
      input.select();
      try {
        document.execCommand('copy');
        done();
      } catch (error) {}
    });

    control.appendChild(input);
    control.appendChild(reveal);
    control.appendChild(copy);
    return control;
  }

  function settingInput(field) {
    if (SECRET_KEYS[field.key]) return SecretField(field);

    if (BOOLEAN_KEYS[field.key]) {
      var label = makeElement('label', 'checkbox');
      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.name = field.key;
      checkbox.checked = String(field.value).toLowerCase() === 'true';
      checkbox.setAttribute('data-settings-key', field.key);
      checkbox.dataset.dirty = 'false';
      checkbox.addEventListener('change', function () { checkbox.dataset.dirty = 'true'; });
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(t('settings.enabled', 'Enabled')));
      return label;
    }

    var input = makeElement('input', 'input');
    input.type = INTEGER_KEYS[field.key] ? 'number' : 'text';
    input.name = field.key;
    input.value = field.value === null || field.value === undefined ? '' : field.value;
    input.setAttribute('data-settings-key', field.key);
    input.dataset.dirty = 'false';
    if (INTEGER_KEYS[field.key]) input.step = '1';
    input.addEventListener('input', function () { input.dataset.dirty = 'true'; });
    return input;
  }

  function renderField(field) {
    var wrapper = makeElement('div', 'field');
    wrapper.setAttribute('data-settings-field', field.key);
    var label = makeElement('label', 'field-label', fieldLabel(field.key));
    label.htmlFor = 'settings-' + field.key;
    if (REQUIRED_KEYS[field.key]) {
      label.appendChild(makeElement('span', 'req', ' *'));
    }
    label.appendChild(document.createTextNode(' '));
    label.appendChild(SourceBadge(field.source));
    wrapper.appendChild(label);

    var control = settingInput(field);
    var input = control.matches && control.matches('[data-settings-key]')
      ? control
      : control.querySelector('[data-settings-key]');
    if (input) input.id = 'settings-' + field.key;
    wrapper.appendChild(control);
    if (field.requires_restart) {
      wrapper.appendChild(makeElement('span', 'field-help', t('settings.restartField', 'Requires restart after saving')));
    }
    var error = makeElement('span', 'field-error');
    error.hidden = true;
    error.setAttribute('data-settings-error', field.key);
    error.setAttribute('role', 'alert');
    wrapper.appendChild(error);
    return wrapper;
  }

  function clearErrors(form) {
    form.querySelectorAll('[data-settings-error]').forEach(function (error) {
      error.hidden = true;
      error.textContent = '';
    });
    form.querySelectorAll('[data-settings-key]').forEach(function (input) {
      input.classList.remove('has-error');
    });
  }

  function showErrors(form, errors) {
    (errors || []).forEach(function (item) {
      var error = form.querySelector('[data-settings-error="' + item.key + '"]');
      var input = form.querySelector('[data-settings-key="' + item.key + '"]');
      if (!error) return;
      error.textContent = item.message;
      error.hidden = false;
      if (input) input.classList.add('has-error');
    });
  }

  function collectUpdates(form) {
    var updates = {};
    var errors = [];
    form.querySelectorAll('[data-settings-key]').forEach(function (input) {
      var key = input.getAttribute('data-settings-key');
      // A displayed mask is never a replacement secret. Only a user input event
      // marks a secret dirty, so untouched secrets are deliberately omitted.
      if (input.hasAttribute('data-secret-field') && input.dataset.secretDirty !== 'true') return;
      if (!input.hasAttribute('data-secret-field') && input.dataset.dirty !== 'true') return;

      var value = input.type === 'checkbox' ? input.checked : input.value;
      if (REQUIRED_KEYS[key] && String(value).trim() === '') {
        errors.push({key: key, message: t('settings.validation.required', 'This field is required')});
      } else if (INTEGER_KEYS[key] && !/^[+-]?\d+$/.test(String(value))) {
        errors.push({key: key, message: t('settings.validation.integer', 'Enter a whole number')});
      } else {
        updates[key] = value;
      }
    });
    return {updates: updates, errors: errors};
  }

  function RestartBanner(keys) {
    var sections = document.querySelector('.settings-sections');
    var banner = document.getElementById('settings-restart-banner');
    if (!banner && sections) {
      banner = makeElement('p', 'badge badge-pending');
      banner.id = 'settings-restart-banner';
      banner.setAttribute('role', 'status');
      sections.insertBefore(banner, sections.firstChild);
    }
    if (banner) {
      banner.textContent = t('settings.restartRequired', 'Restart required for: ') + keys.join(', ');
      banner.hidden = false;
    }
  }

  function TestConnectionButton(target) {
    var control = makeElement('div', 'form-row');
    var button = makeElement('button', 'btn btn-sm', t('settings.testConnection', 'Test connection'));
    var result = makeElement('span', 'field-help');
    button.type = 'button';
    button.setAttribute('data-settings-test-connection', target);
    result.setAttribute('data-settings-test-result', target);
    button.addEventListener('click', function () {
      button.disabled = true;
      result.className = 'field-help';
      result.textContent = t('settings.testingConnection', 'Testing connection…');
      fetch('/api/admin/settings/test-connection', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify({target: target})
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) throw body;
          return body;
        });
      }).then(function (body) {
        result.className = body.ok ? 'badge badge-success' : 'badge badge-failed';
        result.textContent = body.ok
          ? t('settings.connectionOk', 'Connection successful')
          : t('settings.connectionFailed', 'Connection failed') + (body.reason ? ': ' + body.reason : '');
      }).catch(function (body) {
        result.className = 'badge badge-failed';
        result.textContent = t('settings.connectionFailed', 'Connection failed') +
          (body && body.reason ? ': ' + body.reason : '');
      }).finally(function () {
        button.disabled = false;
      });
    });
    control.appendChild(button);
    control.appendChild(result);
    return control;
  }

  function renderGroup(group, fields) {
    var sectionName = CONFIG_GROUPS[group];
    var body = document.querySelector('#settings-section-' + sectionName + ' .card-bd');
    if (!body) return;
    body.textContent = '';
    var form = makeElement('form');
    form.noValidate = true;
    form.setAttribute('data-settings-config-form', group);
    fields.forEach(function (field) { form.appendChild(renderField(field)); });

    var actions = makeElement('div', 'form-row');
    var save = makeElement('button', 'btn btn-primary', t('settings.save', 'Save settings'));
    save.type = 'submit';
    save.setAttribute('data-settings-save', group);
    var status = makeElement('span', 'field-help');
    status.setAttribute('role', 'status');
    status.setAttribute('data-settings-status', group);
    actions.appendChild(save);
    actions.appendChild(status);
    form.appendChild(actions);
    if (CONNECTION_TARGETS[sectionName]) form.appendChild(TestConnectionButton(CONNECTION_TARGETS[sectionName]));

    form.addEventListener('submit', function (event) {
      event.preventDefault();
      clearErrors(form);
      var collected = collectUpdates(form);
      if (collected.errors.length) {
        showErrors(form, collected.errors);
        return;
      }
      if (!Object.keys(collected.updates).length) {
        status.textContent = t('settings.noChanges', 'No changes to save');
        return;
      }
      save.disabled = true;
      status.className = 'field-help';
      status.textContent = t('settings.saving', 'Saving…');
      fetch('/api/admin/settings', {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        credentials: 'include',
        body: JSON.stringify({updates: collected.updates})
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          return {response: response, body: body};
        });
      }).then(function (result) {
        if (result.response.status === 400 && result.body.errors) {
          showErrors(form, result.body.errors);
          status.textContent = '';
          return;
        }
        if (!result.response.ok) throw new Error('settings_save_failed');
        status.textContent = t('settings.saved', 'Settings saved');
        if (result.body.restart_required_keys && result.body.restart_required_keys.length) {
          RestartBanner(result.body.restart_required_keys);
        }
        loadSettings();
      }).catch(function () {
        status.className = 'field-error';
        status.textContent = t('settings.saveFailed', 'Unable to save settings');
      }).finally(function () {
        save.disabled = false;
      });
    });
    body.appendChild(form);
  }

  function ExportSettingsButton() {
    var sections = document.querySelector('.settings-sections');
    if (!sections || document.querySelector('[data-settings-export]')) return;

    var actions = makeElement('div', 'form-row');
    var button = makeElement('button', 'btn btn-sm', t('settings.exportEnv', '复制为 .env'));
    var status = makeElement('span', 'field-help');
    button.type = 'button';
    button.setAttribute('data-settings-export', 'env');
    button.addEventListener('click', function () {
      button.disabled = true;
      status.textContent = t('settings.exporting', '正在复制…');
      fetch('/api/admin/settings/export', {credentials: 'include'}).then(function (response) {
        if (!response.ok) throw new Error('settings_export_failed');
        return response.text();
      }).then(function (text) {
        return navigator.clipboard.writeText(text);
      }).then(function () {
        status.textContent = t('settings.exportCopied', '已复制');
      }).catch(function () {
        status.className = 'field-error';
        status.textContent = t('settings.exportFailed', '无法复制配置');
      }).finally(function () {
        button.disabled = false;
      });
    });
    actions.appendChild(button);
    actions.appendChild(status);
    sections.insertBefore(actions, sections.firstChild);
  }

  function renderSettings(data) {
    Object.keys(CONFIG_GROUPS).forEach(function (group) {
      renderGroup(group, (data.groups && data.groups[group]) || []);
    });
  }

  function loadSettings() {
    return fetch('/api/admin/settings', {credentials: 'include'}).then(function (response) {
      if (!response.ok) throw new Error('settings_load_failed');
      return response.json();
    }).then(renderSettings).catch(function () {
      Object.keys(CONFIG_GROUPS).forEach(function (group) {
        var body = document.querySelector('#settings-section-' + CONFIG_GROUPS[group] + ' .card-bd');
        if (body) body.textContent = t('settings.loadFailed', 'Unable to load settings');
      });
    });
  }

  function initSettingsConfiguration() {
    ExportSettingsButton();
    loadSettings();
  }

  function initSettingsPage() {
    initSettingsNavigation();
    initSettingsConfiguration();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSettingsPage);
  } else {
    initSettingsPage();
  }
}());
