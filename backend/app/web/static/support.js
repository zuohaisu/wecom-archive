(function () {
  'use strict';

  var sessionId = null;
  var lastUserMessage = null;
  var lastAssistantBubble = null;
  var currentAbort = null;
  var lastFocusedBeforeModal = null;

  function t(key) { return I18N.t(key); }
  function node(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    var div = document.createElement('div');
    div.textContent = value == null ? '' : String(value);
    return div.innerHTML;
  }

  function setStatus(message) {
    var el = node('support-status');
    if (el) { el.textContent = message || ''; }
  }

  function setSending(isSending) {
    node('support-send').disabled = isSending;
    node('support-stop').classList.toggle('hidden', !isSending);
  }

  function addBubble(role, text) {
    var messages = node('support-messages');
    var bubble = document.createElement('div');
    bubble.className = 'support-bubble support-bubble-' + role;
    var content = document.createElement('div');
    content.className = 'support-bubble-content';
    content.textContent = text;
    bubble.appendChild(content);
    if (role === 'assistant') {
      var notice = document.createElement('p');
      notice.className = 'support-ai-notice';
      notice.setAttribute('data-i18n', 'support.aiGeneratedNotice');
      notice.textContent = t('support.aiGeneratedNotice');
      bubble.appendChild(notice);
    }
    messages.appendChild(bubble);
    messages.scrollTop = messages.scrollHeight;
    return { root: bubble, content: content };
  }

  function renderAssistantMeta(bubble, meta) {
    var footer = document.createElement('div');
    footer.className = 'support-bubble-footer';

    if (meta.citations && meta.citations.length) {
      var details = document.createElement('details');
      details.className = 'support-citations';
      var summary = document.createElement('summary');
      summary.textContent = t('support.citationsLabel') + ' (' + meta.citations.length + ')';
      details.appendChild(summary);
      var list = document.createElement('ul');
      meta.citations.forEach(function (c) {
        var li = document.createElement('li');
        li.textContent = c.title + ' — ' + c.heading_path + ' (v' + c.doc_version + ')';
        list.appendChild(li);
      });
      details.appendChild(list);
      footer.appendChild(details);
    }

    if (meta.response_status === 'insufficient_evidence' || meta.escalation_reason) {
      var hint = document.createElement('p');
      hint.className = 'support-escalation-hint';
      hint.textContent = t('support.escalationHint');
      footer.appendChild(hint);
    }

    var feedback = document.createElement('div');
    feedback.className = 'support-feedback';
    var helpfulBtn = document.createElement('button');
    helpfulBtn.type = 'button';
    helpfulBtn.className = 'btn btn-ghost btn-sm';
    helpfulBtn.textContent = t('support.helpful');
    var unhelpfulBtn = document.createElement('button');
    unhelpfulBtn.type = 'button';
    unhelpfulBtn.className = 'btn btn-ghost btn-sm';
    unhelpfulBtn.textContent = t('support.unhelpful');
    function sendFeedback(helpful) {
      if (!meta.message_id) { return; }
      helpfulBtn.disabled = true;
      unhelpfulBtn.disabled = true;
      fetch('/api/ai/support/messages/' + meta.message_id + '/feedback', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ helpful: helpful })
      }).catch(function () {});
    }
    helpfulBtn.addEventListener('click', function () { sendFeedback(true); });
    unhelpfulBtn.addEventListener('click', function () { sendFeedback(false); });
    feedback.appendChild(helpfulBtn);
    feedback.appendChild(unhelpfulBtn);

    var handoffBtn = document.createElement('button');
    handoffBtn.type = 'button';
    handoffBtn.className = 'btn btn-ghost btn-sm';
    handoffBtn.textContent = t('support.escalate');
    handoffBtn.addEventListener('click', openHandoffModal);
    feedback.appendChild(handoffBtn);

    footer.appendChild(feedback);
    bubble.root.appendChild(footer);
  }

  function ensureSession() {
    if (sessionId) { return Promise.resolve(sessionId); }
    return fetch('/api/ai/support/sessions', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) { sessionId = data.id; return sessionId; });
  }

  function parseSseChunk(buffer, onEvent) {
    var parts = buffer.split('\n\n');
    var remainder = parts.pop();
    parts.forEach(function (part) {
      var lines = part.split('\n');
      var eventType = 'message';
      var data = '';
      lines.forEach(function (line) {
        if (line.indexOf('event: ') === 0) { eventType = line.slice(7); }
        if (line.indexOf('data: ') === 0) { data = line.slice(6); }
      });
      if (data) {
        try { onEvent(eventType, JSON.parse(data)); } catch (e) { /* ignore malformed chunk */ }
      }
    });
    return remainder;
  }

  function sendMessage(text) {
    if (!text || !text.trim()) { return; }
    lastUserMessage = text;
    node('support-retry').disabled = false;
    addBubble('user', text);
    var assistant = addBubble('assistant', '');
    lastAssistantBubble = assistant;
    setSending(true);
    setStatus('');

    var controller = new AbortController();
    currentAbort = controller;

    ensureSession()
      .then(function (id) {
        return fetch('/api/ai/support/sessions/' + id + '/messages', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({
            message: text,
            include_diagnostics: node('support-include-diagnostics').checked,
            page_id: document.body.getAttribute('data-page-id') || null
          })
        });
      })
      .then(function (response) {
        if (response.status === 429) {
          setStatus(t('support.rateLimited'));
          setSending(false);
          return null;
        }
        if (!response.ok || !response.body) {
          setStatus(t('support.networkError'));
          setSending(false);
          return null;
        }
        var reader = response.body.getReader();
        var decoder = new TextDecoder();
        var buffer = '';
        var accumulatedText = '';

        function pump() {
          return reader.read().then(function (result) {
            if (result.done) { setSending(false); return; }
            buffer += decoder.decode(result.value, { stream: true });
            buffer = parseSseChunk(buffer, function (eventType, payload) {
              if (eventType === 'token') {
                accumulatedText += payload;
                assistant.content.textContent = accumulatedText;
                node('support-messages').scrollTop = node('support-messages').scrollHeight;
              } else if (eventType === 'done') {
                renderAssistantMeta(assistant, payload);
              }
            });
            return pump();
          });
        }
        return pump();
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') {
          setStatus(t('support.stopped'));
        } else {
          setStatus(t('support.networkError'));
        }
        setSending(false);
      });
  }

  function openHandoffModal() {
    if (!sessionId) { return; }
    lastFocusedBeforeModal = document.activeElement;
    var modal = node('handoff-modal');
    modal.classList.remove('hidden');
    node('handoff-status').textContent = t('support.handoffLoading');
    fetch('/api/ai/support/sessions/' + sessionId + '/handoff/preview', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        node('handoff-summary').value = data.summary;
        node('handoff-status').textContent = '';
        node('handoff-summary').focus();
      })
      .catch(function () { node('handoff-status').textContent = t('support.networkError'); });
  }

  function closeHandoffModal() {
    node('handoff-modal').classList.add('hidden');
    if (lastFocusedBeforeModal && lastFocusedBeforeModal.focus) { lastFocusedBeforeModal.focus(); }
  }

  function confirmHandoff() {
    if (!sessionId) { return; }
    var summary = node('handoff-summary').value;
    var contact = node('handoff-contact').value;
    node('handoff-status').textContent = t('support.handoffSubmitting');
    fetch('/api/ai/support/sessions/' + sessionId + '/handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary: summary, contact: contact || null })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error('handoff_failed'); }
        return r.json();
      })
      .then(function () {
        node('handoff-status').textContent = t('support.handoffSubmitted');
        setTimeout(closeHandoffModal, 1200);
      })
      .catch(function () { node('handoff-status').textContent = t('support.networkError'); });
  }

  function clearSession() {
    var previous = sessionId;
    sessionId = null;
    lastUserMessage = null;
    node('support-messages').innerHTML = '';
    node('support-retry').disabled = true;
    setStatus('');
    if (previous) {
      fetch('/api/ai/support/sessions/' + previous, { method: 'DELETE' }).catch(function () {});
    }
  }

  function switchTab(target) {
    var isChat = target === 'chat';
    node('tab-chat').classList.toggle('active', isChat);
    node('tab-chat').setAttribute('aria-selected', String(isChat));
    node('tab-feedback').classList.toggle('active', !isChat);
    node('tab-feedback').setAttribute('aria-selected', String(!isChat));
    node('support-chat-panel').classList.toggle('hidden', !isChat);
    node('support-feedback-panel').classList.toggle('hidden', isChat);
  }

  function submitFeedback(evt) {
    evt.preventDefault();
    var statusEl = node('feedback-status');
    var body = node('feedback-body').value;
    if (!body || !body.trim()) { return; }
    statusEl.textContent = t('support.feedbackSubmitting');
    fetch('/api/ai/support/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // GH-102 follow-up (Haisu): the diagnostics consent checkbox is gone —
      // submissions carry only the feedback itself, no page/version/runtime
      // attachments.
      body: JSON.stringify({
        feedback_type: node('feedback-type').value,
        body: body,
        contact: node('feedback-contact').value || null
      })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error('feedback_failed'); }
        return r.json();
      })
      .then(function () {
        statusEl.textContent = t('support.feedbackSubmitted');
        node('feedback-form').reset();
      })
      .catch(function () { statusEl.textContent = t('support.networkError'); });
  }

  function applyI18n() {
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = t(element.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-placeholder]').forEach(function (element) {
      element.placeholder = t(element.getAttribute('data-i18n-placeholder'));
    });
    document.title = t('support.pageTitle');
  }

  function renderLangMenu() {
    var menu = node('lang-menu');
    if (!menu) { return; }
    menu.textContent = '';
    I18N.availableLocales().forEach(function (locale) {
      var option = document.createElement('div');
      option.className = 'lang-option' + (locale.code === I18N.getLocale() ? ' active' : '');
      option.dataset.locale = locale.code;
      option.textContent = locale.nativeName;
      menu.appendChild(option);
    });
  }

  function loadCurrentUser() {
    fetch('/api/auth/me', { credentials: 'include' })
      .then(function (r) {
        if (!r.ok) { throw new Error('auth_failed'); }
        return r.json();
      })
      .then(function (user) {
        var current = node('current-user');
        if (current) { current.textContent = user.email || user.username || ''; }
      })
      .catch(function () {});
  }

  window.toggleLangMenu = function () {
    var menu = node('lang-menu');
    if (!menu) { return; }
    menu.style.display = menu.style.display === 'none' ? 'block' : 'none';
    renderLangMenu();
  };

  window.doLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).finally(function () {
      window.location = '/admin/login';
    });
  };

  document.addEventListener('click', function (event) {
    var option = event.target.closest('.lang-option');
    if (option) {
      I18N.setLocale(option.dataset.locale);
      node('lang-menu').style.display = 'none';
      return;
    }
    var sw = node('lang-switch');
    var menu = node('lang-menu');
    if (sw && menu && !sw.contains(event.target)) { menu.style.display = 'none'; }
  });

  function init() {
    applyI18n();
    renderLangMenu();
    loadCurrentUser();
    I18N.onChange(function () { applyI18n(); renderLangMenu(); });

    fetch('/api/ai/support/status')
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.enabled) {
          node('support-disabled').classList.remove('hidden');
          node('support-chat').classList.add('hidden');
        }
      })
      .catch(function () {});

    node('support-composer').addEventListener('submit', function (evt) {
      evt.preventDefault();
      var input = node('support-input');
      var text = input.value;
      input.value = '';
      sendMessage(text);
    });

    node('support-stop').addEventListener('click', function () {
      if (currentAbort) { currentAbort.abort(); }
    });

    node('support-retry').addEventListener('click', function () {
      if (lastUserMessage) { sendMessage(lastUserMessage); }
    });

    node('support-clear').addEventListener('click', clearSession);

    node('handoff-cancel').addEventListener('click', closeHandoffModal);
    node('handoff-confirm').addEventListener('click', confirmHandoff);
    document.addEventListener('keydown', function (evt) {
      if (evt.key === 'Escape' && !node('handoff-modal').classList.contains('hidden')) {
        closeHandoffModal();
      }
    });

    node('tab-chat').addEventListener('click', function () { switchTab('chat'); });
    node('tab-feedback').addEventListener('click', function () { switchTab('feedback'); });
    node('feedback-form').addEventListener('submit', submitFeedback);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
