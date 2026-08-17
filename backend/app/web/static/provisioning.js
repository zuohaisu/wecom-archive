(function () {
  'use strict';

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

  // ----------------------------------------------------------------------
  // RND-386 config wizard (only rendered on /admin/provisioning/settings)
  // ----------------------------------------------------------------------

  function initWizard() {
    var form = document.getElementById('fields-form');
    var saveButton = document.getElementById('save-config');
    var testButton = document.getElementById('test-config');
    var copyButton = document.getElementById('copy-callback-url');
    if (!form || !saveButton || !testButton || !copyButton) return;

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
      if (!box) return;
      box.textContent = t(key);
      box.hidden = false;
    }

    function hideError() {
      var box = document.getElementById('provisioning-error');
      if (box) box.hidden = true;
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

    saveButton.addEventListener('click', function () {
      hideError();
      var updates = collectUpdates();
      saveButton.disabled = true;
      saveButton.textContent = t('provisioning.saving', 'Saving…');
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
          saveButton.disabled = false;
          saveButton.textContent = t('provisioning.save', 'Save');
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
          saveButton.disabled = false;
          saveButton.textContent = t('provisioning.save', 'Save');
          showError('provisioning.error.save');
        });
    });

    testButton.addEventListener('click', function () {
      hideError();
      testButton.disabled = true;
      testButton.textContent = t('provisioning.testing', 'Testing…');
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
          testButton.disabled = false;
          testButton.textContent = t('provisioning.test', 'Test connection');
          renderTestResult(data);
        })
        .catch(function () {
          testButton.disabled = false;
          testButton.textContent = t('provisioning.test', 'Test connection');
          showError('provisioning.error.test');
        });
    });

    copyButton.addEventListener('click', function () {
      var value = document.getElementById('callback-url').textContent;
      var done = function () {
        copyButton.textContent = t('provisioning.callbackUrl.copied', 'Copied');
        window.setTimeout(function () {
          copyButton.textContent = t('provisioning.callbackUrl.copy', 'Copy');
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(value).then(done).catch(function () {});
        return;
      }
      done();
    });

    load();
  }

  // ----------------------------------------------------------------------
  // RND-388 activation status page (only on /admin/provisioning)
  // ----------------------------------------------------------------------

  function initActivation() {
    var root = document.getElementById('activation-root');
    if (!root) return;

    var GATE_IDS = ['binding', 'config', 'connectivity', 'subscription', 'runtime'];
    var SAFE_ERROR_KEYS = {
      missing_binding: 'activation.error.missing_binding',
      config_incomplete: 'activation.error.config_incomplete',
      config_not_decryptable: 'activation.error.config_not_decryptable',
      credentials_invalid: 'activation.error.credentials_invalid',
      connectivity_failed: 'activation.error.connectivity_failed',
      no_entitlement: 'activation.error.no_entitlement',
      runtime_unavailable: 'activation.error.runtime_unavailable',
      activation_conflict: 'activation.error.activation_conflict'
    };
    var POLL_DELAY = 5000;
    var pollTimer = null;
    var unloaded = false;
    var requestInFlight = false;
    var activating = false;

    function safeErrorText(code) {
      return I18N.t(SAFE_ERROR_KEYS[code] || 'activation.error.config_incomplete');
    }

    function ctaForCode(code) {
      if (code === 'no_entitlement') {
        return { href: '/admin/billing', label: 'activation.cta.purchase', primary: true };
      }
      if (code === 'config_incomplete' || code === 'config_not_decryptable' || code === 'credentials_invalid') {
        return { href: '/admin/provisioning/settings', label: 'activation.cta.configure', primary: true };
      }
      return null;
    }

    function stateInfo(snapshot) {
      if (snapshot.state === 'active') {
        return { icon: '✓', title: 'activation.state.active.title', copy: 'activation.state.active.copy' };
      }
      if (snapshot.state === 'ready') {
        return { icon: '⚡', title: 'activation.state.ready.title', copy: 'activation.state.ready.copy' };
      }
      if (snapshot.state === 'blocked') {
        return { icon: '!', title: 'activation.state.blocked.title', copy: 'activation.state.blocked.copy' };
      }
      return { icon: '○', title: 'activation.state.notStarted.title', copy: 'activation.state.notStarted.copy' };
    }

    function normalise(data) {
      data = data && typeof data === 'object' ? data : {};
      var state = data.state;
      if (state !== 'active' && state !== 'ready' && state !== 'blocked' && state !== 'not_started') {
        state = 'not_started';
      }
      var gates = data.gate_results && typeof data.gate_results === 'object' ? data.gate_results : {};
      var safeError = typeof data.safe_error_code === 'string' ? data.safe_error_code : null;
      var revision = typeof data.revision === 'number' ? data.revision : 0;
      return { state: state, gate_results: gates, safe_error_code: safeError, revision: revision };
    }

    function actionButton(labelKey, handler, primary) {
      var button = makeElement('button', 'btn' + (primary ? ' btn-primary' : ''), I18N.t(labelKey));
      button.type = 'button';
      button.addEventListener('click', handler);
      return button;
    }

    function render(snapshot) {
      applyI18n();
      root.replaceChildren();
      root.setAttribute('aria-busy', 'false');
      var info = stateInfo(snapshot);
      var card = makeElement('section', 'card');
      var hero = makeElement('div', 'activation-hero');
      hero.setAttribute('data-state', snapshot.state);
      hero.appendChild(makeElement('span', 'act-icon', info.icon)).setAttribute('aria-hidden', 'true');
      var copy = makeElement('div');
      copy.appendChild(makeElement('h2', null, I18N.t(info.title)));
      copy.appendChild(makeElement('p', null, I18N.t(info.copy)));
      hero.appendChild(copy);
      card.appendChild(hero);

      if (snapshot.state === 'blocked') {
        var gates = makeElement('div', 'activation-gates');
        GATE_IDS.forEach(function (gateId) {
          var result = snapshot.gate_results[gateId];
          var chip = makeElement('div', 'gate-chip');
          if (!result) {
            chip.setAttribute('data-skipped', 'true');
            chip.appendChild(makeElement('span', 'gate-name', I18N.t('activation.gate.' + gateId)));
            chip.appendChild(makeElement('span', 'gate-status badge', I18N.t('activation.gate.skipped')));
          } else {
            chip.setAttribute('data-ok', String(result.ok));
            chip.appendChild(makeElement('span', 'gate-name', I18N.t('activation.gate.' + gateId)));
            var statusBox = makeElement('span');
            statusBox.appendChild(makeElement('span', 'gate-status badge' + (result.ok ? ' badge-success' : ' badge-warning'),
              I18N.t(result.ok ? 'activation.gate.ok' : 'activation.gate.failed')));
            if (!result.ok && typeof result.safe_error_code === 'string') {
              statusBox.appendChild(makeElement('div', 'gate-safe-error', safeErrorText(result.safe_error_code)));
            }
            chip.appendChild(statusBox);
          }
          gates.appendChild(chip);
        });
        card.appendChild(gates);
      }

      var actions = makeElement('div', 'activation-actions');
      if (snapshot.state === 'active') {
        var dashboard = makeElement('a', 'btn btn-primary', I18N.t('activation.goDashboard'));
        dashboard.href = '/dashboard';
        actions.appendChild(dashboard);
      } else {
        var cta = snapshot.safe_error_code ? ctaForCode(snapshot.safe_error_code) : null;
        if (cta) {
          var link = makeElement('a', 'btn btn-primary', I18N.t(cta.label));
          link.href = cta.href;
          actions.appendChild(link);
        }
        var activateLabel = snapshot.state === 'ready' ? 'activation.cta.activate' : 'activation.cta.retry';
        actions.appendChild(actionButton(activateLabel, runActivate, !cta));
      }
      card.appendChild(actions);

      if (snapshot.revision > 0) {
        var details = makeElement('details', 'activation-foot');
        details.appendChild(makeElement('summary', null, I18N.t('activation.technicalDetails')));
        details.appendChild(makeElement('p', null, I18N.t('activation.revision') + ': ' + String(snapshot.revision)));
        card.appendChild(details);
      }
      root.appendChild(card);
    }

    function renderProblem(key) {
      applyI18n();
      root.replaceChildren();
      root.setAttribute('aria-busy', 'false');
      var card = makeElement('section', 'card');
      var hero = makeElement('div', 'activation-hero');
      hero.setAttribute('data-state', 'blocked');
      hero.appendChild(makeElement('span', 'act-icon', '×')).setAttribute('aria-hidden', 'true');
      var copy = makeElement('div');
      copy.appendChild(makeElement('h2', null, I18N.t('activation.state.blocked.title')));
      copy.appendChild(makeElement('p', null, I18N.t(key)));
      hero.appendChild(copy);
      card.appendChild(hero);
      root.appendChild(card);
    }

    function stopPolling() {
      if (pollTimer !== null) {
        window.clearTimeout(pollTimer);
        pollTimer = null;
      }
    }

    function schedulePoll() {
      if (unloaded) return;
      pollTimer = window.setTimeout(function () {
        pollTimer = null;
        loadStatus();
      }, POLL_DELAY);
    }

    function receive(data) {
      var snapshot = normalise(data);
      render(snapshot);
      if (snapshot.state === 'active') {
        stopPolling();
      } else {
        schedulePoll();
      }
    }

    function loadStatus() {
      if (requestInFlight || unloaded) return;
      requestInFlight = true;
      fetch('/api/provisioning/status', { credentials: 'include' })
        .then(function (response) {
          if (response.status === 401) {
            stopPolling();
            window.location = '/admin/login';
            return null;
          }
          // 403 after activation: the tenant became active and the session was
          // promoted out of provisioning scope — that is the success end-state.
          if (response.status === 403) {
            stopPolling();
            window.location = '/dashboard';
            return null;
          }
          if (!response.ok) throw new Error('status_failed');
          return response.json();
        })
        .then(function (data) {
          if (data) receive(data.activation || {});
        })
        .catch(function () {
          stopPolling();
          renderProblem('activation.loading');
        })
        .finally(function () {
          requestInFlight = false;
        });
    }

    function runActivate() {
      if (activating || requestInFlight) return;
      activating = true;
      stopPolling();
      applyI18n();
      root.setAttribute('aria-busy', 'true');
      root.replaceChildren();
      root.appendChild(makeElement('p', 'diag-loading', I18N.t('activation.activating')));
      fetch('/api/provisioning/activate', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}'
      })
        .then(function (response) {
          if (response.status === 401) {
            window.location = '/admin/login';
            return null;
          }
          if (!response.ok) throw new Error('activate_failed');
          return response.json();
        })
        .then(function (data) {
          if (!data) return;
          if (data.activated) {
            stopPolling();
            window.location = '/dashboard';
          } else {
            render(normalise(data.activation));
            schedulePoll();
          }
        })
        .catch(function () {
          stopPolling();
          renderProblem('activation.failed');
        })
        .finally(function () {
          activating = false;
        });
    }

    window.addEventListener('pagehide', function () {
      unloaded = true;
      stopPolling();
    });
    render({ state: 'not_started', gate_results: {}, safe_error_code: null, revision: 0 });
    loadStatus();
  }

  // ----------------------------------------------------------------------

  initWizard();
  initActivation();
  if (window.I18N) I18N.onChange(applyI18n);
})();
