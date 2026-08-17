(function () {
  'use strict';

  var sessionId = null;
  var lastUserMessage = null;
  var lastAssistantBubble = null;
  var currentAbort = null;
  var lastFocusedBeforeModal = null;

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
      notice.textContent = 'AI 生成，重要配置请核对正式文档。';
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
      summary.textContent = '引用来源 (' + meta.citations.length + ')';
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

    if (meta.response_status === 'insufficient_evidence' || meta.escalation_reason || meta.response_status === 'budget_exceeded' || meta.response_status === 'error') {
      var hint = document.createElement('p');
      hint.className = 'support-escalation-hint';
      hint.textContent = '此问题可能需要人工确认，可点击下方“转人工”。';
      footer.appendChild(hint);
    }

    var actions = document.createElement('div');
    actions.className = 'support-feedback';

    var handoffBtn = document.createElement('button');
    handoffBtn.type = 'button';
    handoffBtn.className = 'btn btn-ghost btn-sm';
    handoffBtn.textContent = '转人工';
    handoffBtn.addEventListener('click', openHandoffModal);
    actions.appendChild(handoffBtn);

    footer.appendChild(actions);
    bubble.root.appendChild(footer);
  }

  function ensureSession() {
    if (sessionId) { return Promise.resolve(sessionId); }
    return fetch('/api/ai/public/support/sessions', { method: 'POST' })
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
        return fetch('/api/ai/public/support/sessions/' + id + '/messages', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: controller.signal,
          body: JSON.stringify({ message: text })
        });
      })
      .then(function (response) {
        if (response.status === 429) {
          setStatus('发送过于频繁，请稍后再试。');
          setSending(false);
          return null;
        }
        if (!response.ok || !response.body) {
          setStatus('网络或服务异常，请重试。');
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
          setStatus('已停止生成。');
        } else {
          setStatus('网络或服务异常，请重试。');
        }
        setSending(false);
      });
  }

  function openHandoffModal() {
    if (!sessionId) { return; }
    lastFocusedBeforeModal = document.activeElement;
    var modal = node('handoff-modal');
    modal.classList.remove('hidden');
    node('handoff-status').textContent = '正在生成摘要…';
    fetch('/api/ai/public/support/sessions/' + sessionId + '/handoff/preview', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        node('handoff-summary').value = data.summary;
        node('handoff-status').textContent = '';
        node('handoff-summary').focus();
      })
      .catch(function () { node('handoff-status').textContent = '网络或服务异常，请重试。'; });
  }

  function closeHandoffModal() {
    node('handoff-modal').classList.add('hidden');
    if (lastFocusedBeforeModal && lastFocusedBeforeModal.focus) { lastFocusedBeforeModal.focus(); }
  }

  function confirmHandoff() {
    if (!sessionId) { return; }
    var summary = node('handoff-summary').value;
    var contact = node('handoff-contact').value;
    node('handoff-status').textContent = '正在提交…';
    fetch('/api/ai/public/support/sessions/' + sessionId + '/handoff', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary: summary, contact: contact || null })
    })
      .then(function (r) {
        if (!r.ok) { throw new Error('handoff_failed'); }
        return r.json();
      })
      .then(function () {
        node('handoff-status').textContent = '已提交，商务将尽快跟进。';
        setTimeout(closeHandoffModal, 1200);
      })
      .catch(function () { node('handoff-status').textContent = '网络或服务异常，请重试。'; });
  }

  function clearSession() {
    var previous = sessionId;
    sessionId = null;
    lastUserMessage = null;
    node('support-messages').innerHTML = '';
    node('support-retry').disabled = true;
    setStatus('');
    if (previous) {
      fetch('/api/ai/public/support/sessions/' + previous, { method: 'DELETE' }).catch(function () {});
    }
  }

  function init() {
    fetch('/api/ai/public/support/status')
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
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
