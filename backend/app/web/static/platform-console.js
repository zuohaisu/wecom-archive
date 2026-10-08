/* RND-414: shared foundation for every /platform/* page — helpers, the
 * reusable two-step controlled-operation modal (Spec §3), the global tenant
 * search box, and the <1024px side-nav collapse. Loaded before any
 * page-specific script (platform-tenants.js, platform-ledger.js, ...),
 * which read these off `window.PC`. No bundler in this repo (see
 * app/web/__init__.py) — plain global namespace, same discipline as the
 * existing platform-operations.js: no innerHTML, only
 * createElement/textContent. */
(function () {
  'use strict';

  function el(id) { return document.getElementById(id); }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }
  function number(value) { return new Intl.NumberFormat('zh-CN').format(value || 0); }
  function money(value) { return '¥' + (Number(value || 0) / 100).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function pad(n) { return n < 10 ? '0' + n : String(n); }
  function date(value) {
    // Spec §9: always UTC+8, always "YYYY-MM-DD HH:mm:ss" — independent of
    // the viewer's own browser/OS timezone (unlike toLocaleString).
    if (!value) { return '—'; }
    var input = new Date(value);
    if (isNaN(input.getTime())) { return '—'; }
    var shifted = new Date(input.getTime() + 8 * 3600 * 1000);
    return shifted.getUTCFullYear() + '-' + pad(shifted.getUTCMonth() + 1) + '-' + pad(shifted.getUTCDate()) +
      ' ' + pad(shifted.getUTCHours()) + ':' + pad(shifted.getUTCMinutes()) + ':' + pad(shifted.getUTCSeconds());
  }
  function dateOnly(value) {
    if (!value) { return '—'; }
    var input = new Date(value);
    if (isNaN(input.getTime())) { return '—'; }
    var shifted = new Date(input.getTime() + 8 * 3600 * 1000);
    return shifted.getUTCFullYear() + '-' + pad(shifted.getUTCMonth() + 1) + '-' + pad(shifted.getUTCDate());
  }
  function bytes(value) {
    var amount = Number(value || 0), units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'], index = 0;
    while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1; }
    return (index ? amount.toFixed(amount >= 10 ? 1 : 2) : amount) + ' ' + units[index];
  }
  function error(message) {
    var box = el('error');
    if (!box) { return; }
    box.hidden = !message;
    box.textContent = message || '';
  }
  function request(url, options) {
    return fetch(url, Object.assign({ credentials: 'same-origin' }, options || {})).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          var err = new Error(body.detail || ('请求失败（' + response.status + '）'));
          err.status = response.status;
          throw err;
        });
      }
      if (response.status === 204) { return null; }
      return response.json();
    });
  }
  function empty(node, label) {
    clear(node);
    var item = document.createElement('li');
    item.className = 'empty';
    item.textContent = label;
    node.appendChild(item);
  }
  function emptyRow(node, colspan, label) {
    clear(node);
    var row = document.createElement('tr'), cell = document.createElement('td');
    cell.colSpan = colspan; cell.className = 'muted'; cell.textContent = label;
    row.appendChild(cell); node.appendChild(row);
  }
  function gapRow(node, colspan, label) {
    clear(node);
    var row = document.createElement('tr'), cell = document.createElement('td');
    cell.colSpan = colspan; cell.className = 'muted'; cell.textContent = label || '此列表依赖的后端接口尚未交付，暂无数据可展示。';
    row.appendChild(cell); node.appendChild(row);
  }
  function list(node, items, render, label) {
    clear(node);
    if (!items || !items.length) { empty(node, label); return; }
    items.forEach(function (item) { node.appendChild(render(item)); });
  }
  function textPair(primary, secondary) {
    var li = document.createElement('li'), left = document.createElement('span'), right = document.createElement('small');
    left.textContent = primary;
    right.textContent = secondary;
    li.appendChild(left);
    li.appendChild(right);
    return li;
  }
  function rowFrom(values) {
    var row = document.createElement('tr');
    values.forEach(function (value) {
      var cell = document.createElement('td');
      if (value && value.nodeType) { cell.appendChild(value); } else { cell.textContent = value === null || value === undefined ? '—' : String(value); }
      row.appendChild(cell);
    });
    return row;
  }
  function actionButton(label, handler, tone) {
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm' + (tone ? ' ' + tone : '');
    button.textContent = label;
    button.addEventListener('click', handler);
    return button;
  }
  function badge(label, tone) {
    var span = document.createElement('span');
    span.className = 'badge' + (tone ? ' badge-' + tone : '');
    span.textContent = label;
    return span;
  }
  function operationKey(prefix) {
    var suffix = window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now()) + '-' + String(Math.random()).slice(2);
    return prefix + '-' + suffix;
  }
  function reasonCodeValid(code) { return /^[a-z][a-z0-9._-]{0,63}$/.test(code || ''); }

  // ------------------------------------------------------------------
  // Shared two-step controlled-operation modal (Spec §3 "两步模态框的固定
  // 结构"). One DOM instance, reused for every operation: suspend/resume,
  // subscription adjust, ledger create/edit/void, refund submit, payment
  // query. Replaces the window.confirm/window.prompt flow entirely.
  //
  // config:
  //   title            string
  //   danger           bool — theme the submit button btn-danger
  //   singleStep       bool — modalQuery has no impact-review step (Spec
  //                    marks it "幂等只读 + 补激活 · 单步确认")
  //   renderImpact(el) — build step-1 body into the given container
  //   continueLabel    string — step-1 primary button text
  //   reasonCodes      [{value,label}]
  //   noteLabel        string (default "原因说明")
  //   notePlaceholder  string
  //   noteRequired     bool (default true)
  //   unlock           null | {label, match} — destructive typed-match arm
  //   idempotencyPrefix string
  //   auditAction      string — shown in the idempotency-disclosure alert
  //   submitLabel      string
  //   onSubmit({reason_code, note, idempotencyKey}) -> Promise
  // ------------------------------------------------------------------
  var modalState = null;

  function modalRoot() {
    var root = el('platform-modal');
    if (root) { return root; }
    root = document.createElement('div');
    root.className = 'modal-backdrop hidden';
    root.id = 'platform-modal';
    var dialog = document.createElement('div');
    dialog.className = 'modal';
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    var hd = document.createElement('div'); hd.className = 'modal-hd';
    var h2 = document.createElement('h2'); h2.id = 'platform-modal-title';
    var close = document.createElement('button');
    close.type = 'button'; close.className = 'modal-close'; close.setAttribute('aria-label', 'Close'); close.textContent = '×';
    close.addEventListener('click', modalClose);
    hd.appendChild(h2); hd.appendChild(close);
    var bd = document.createElement('div'); bd.className = 'modal-bd'; bd.id = 'platform-modal-body';
    var ft = document.createElement('div'); ft.className = 'modal-ft'; ft.id = 'platform-modal-footer';
    dialog.appendChild(hd); dialog.appendChild(bd); dialog.appendChild(ft);
    root.appendChild(dialog);
    root.addEventListener('keydown', function (event) { if (event.key === 'Escape') { modalClose(); } });
    root.addEventListener('mousedown', function (event) { if (event.target === root) { modalClose(); } });
    document.body.appendChild(root);
    return root;
  }

  function modalClose() {
    var root = el('platform-modal');
    if (root) { root.classList.add('hidden'); }
    modalState = null;
  }

  function modalAlert(container, tone, message) {
    var box = container.querySelector('.modal-inline-alert');
    if (!message) { if (box) { box.hidden = true; box.textContent = ''; } return; }
    if (!box) {
      box = document.createElement('div');
      box.className = 'alert alert-' + tone + ' modal-inline-alert';
      container.insertBefore(box, container.firstChild);
    }
    box.className = 'alert alert-' + tone + ' modal-inline-alert';
    box.hidden = false;
    box.textContent = message;
  }

  function fieldWrap(labelText, required, inputNode, help) {
    var field = document.createElement('div'); field.className = 'field';
    var label = document.createElement('label'); label.className = 'field-label';
    label.textContent = labelText;
    if (required) { var req = document.createElement('span'); req.className = 'req'; req.textContent = '*'; label.appendChild(req); }
    field.appendChild(label); field.appendChild(inputNode);
    if (help) { var helpEl = document.createElement('p'); helpEl.className = 'field-help'; helpEl.textContent = help; field.appendChild(helpEl); }
    return field;
  }

  function renderStepTwo(config, body, footer) {
    clear(body); clear(footer);
    if (config.singleStep) { config.renderImpact(body); }
    var reasonSelect = document.createElement('select'); reasonSelect.className = 'select'; reasonSelect.id = 'platform-modal-reason';
    var placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = '请选择原因代码…';
    reasonSelect.appendChild(placeholder);
    config.reasonCodes.forEach(function (option) {
      var opt = document.createElement('option'); opt.value = option.value; opt.textContent = option.label + '（' + option.value + '）';
      reasonSelect.appendChild(opt);
    });
    body.appendChild(fieldWrap('原因代码', true, reasonSelect));

    var noteInput = document.createElement('textarea'); noteInput.className = 'textarea'; noteInput.id = 'platform-modal-note';
    noteInput.maxLength = 1000;
    noteInput.placeholder = config.notePlaceholder || '写入审计，不可修改。';
    body.appendChild(fieldWrap(config.noteLabel || '原因说明', config.noteRequired !== false, noteInput, '≤1000 字符，写入审计，不可修改。'));

    var unlockInput = null;
    if (config.unlock) {
      unlockInput = document.createElement('input'); unlockInput.className = 'input mono'; unlockInput.id = 'platform-modal-unlock';
      unlockInput.placeholder = config.unlock.match;
      body.appendChild(fieldWrap('输入' + config.unlock.label + '以解锁', true, unlockInput));
    }

    // The real /payments/{id}/query and /refunds/{id}/query endpoints
    // require confirmation === tenant.slug via authorize_platform_operation
    // even for this read-only/idempotent action, even though the approved
    // mockup shows it as a lightweight single-step modal with no
    // confirmation field at all. slugConfirm keeps that lightweight feel
    // (a plain required field, not the destructive unlock's live-arm
    // styling) while still satisfying the real API contract.
    var slugConfirmInput = null;
    if (config.slugConfirm) {
      slugConfirmInput = document.createElement('input'); slugConfirmInput.className = 'input mono'; slugConfirmInput.id = 'platform-modal-slug-confirm';
      slugConfirmInput.placeholder = config.slugConfirm.match;
      body.appendChild(fieldWrap('确认租户标识', true, slugConfirmInput, '只读/幂等操作仍需简要确认租户身份（接口要求），输入 ' + config.slugConfirm.match + '。'));
    }

    var disclosure = document.createElement('div'); disclosure.className = 'alert alert-neutral';
    var ico = document.createElement('span'); ico.className = 'alert-ico'; ico.textContent = 'i';
    var text = document.createElement('div');
    var idKeyPrefix = document.createElement('strong'); idKeyPrefix.textContent = '幂等键前缀 ';
    text.appendChild(idKeyPrefix);
    var idKeyCode = document.createElement('span'); idKeyCode.className = 'mono'; idKeyCode.textContent = config.idempotencyPrefix + '-…';
    text.appendChild(idKeyCode);
    text.appendChild(document.createTextNode('，重复提交不会重复执行。审计动作 '));
    var auditCode = document.createElement('span'); auditCode.className = 'mono'; auditCode.textContent = config.auditAction;
    text.appendChild(auditCode); text.appendChild(document.createTextNode('。'));
    disclosure.appendChild(ico); disclosure.appendChild(text);
    body.appendChild(disclosure);

    var submit = document.createElement('button');
    submit.type = 'button';
    submit.className = 'btn ' + (config.danger ? 'btn-danger' : 'btn-primary') + (config.unlock ? ' is-disabled' : '');
    submit.textContent = config.submitLabel;
    submit.disabled = !!config.unlock;

    function updateArm() {
      if (!config.unlock) { return; }
      var matched = unlockInput.value === config.unlock.match;
      submit.disabled = !matched;
      submit.classList.toggle('is-disabled', !matched);
    }
    if (unlockInput) { unlockInput.addEventListener('input', updateArm); unlockInput.focus(); }

    submit.addEventListener('click', function () {
      var reasonCode = reasonSelect.value;
      var note = noteInput.value.trim();
      if (!reasonCodeValid(reasonCode)) { modalAlert(body, 'danger', '请选择原因代码。'); return; }
      if (config.noteRequired !== false && !note) { modalAlert(body, 'danger', '请填写原因说明。'); return; }
      if (config.unlock && unlockInput.value !== config.unlock.match) { modalAlert(body, 'danger', '输入的标识不匹配，操作已取消。'); return; }
      if (config.slugConfirm && slugConfirmInput.value !== config.slugConfirm.match) { modalAlert(body, 'danger', '租户标识不匹配，操作已取消。'); return; }
      submit.disabled = true;
      modalAlert(body, 'neutral', null);
      var confirmation = config.unlock ? unlockInput.value : (config.slugConfirm ? slugConfirmInput.value : undefined);
      Promise.resolve(config.onSubmit({
        reason_code: reasonCode,
        note: note,
        confirmation: confirmation,
        idempotencyKey: operationKey(config.idempotencyPrefix)
      })).then(function () {
        modalClose();
      }).catch(function (err) {
        submit.disabled = !config.unlock || unlockInput.value === config.unlock.match;
        modalAlert(body, 'danger', (err && err.message) || '提交失败，请稍后重试。');
      });
    });

    var stepBack = document.createElement('button'); stepBack.type = 'button'; stepBack.className = 'btn';
    if (config.singleStep) {
      stepBack.textContent = '取消';
      stepBack.addEventListener('click', modalClose);
    } else {
      stepBack.textContent = '上一步';
      stepBack.addEventListener('click', function () { renderStepOne(config, body, footer); });
    }
    footer.appendChild(stepBack); footer.appendChild(submit);
  }

  function renderStepOne(config, body, footer) {
    clear(body); clear(footer);
    config.renderImpact(body);
    var cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'btn'; cancel.textContent = '取消';
    cancel.addEventListener('click', modalClose);
    var next = document.createElement('button');
    next.type = 'button';
    next.className = 'btn btn-primary';
    next.textContent = config.continueLabel || '已复核影响，继续';
    next.addEventListener('click', function () { renderStepTwo(config, body, footer); });
    footer.appendChild(cancel); footer.appendChild(next);
  }

  function openModal(config) {
    modalState = config;
    var root = modalRoot();
    el('platform-modal-title').textContent = config.title;
    var body = el('platform-modal-body'), footer = el('platform-modal-footer');
    if (config.singleStep) { renderStepTwo(Object.assign({}, config, { unlock: null }), body, footer); }
    else { renderStepOne(config, body, footer); }
    root.classList.remove('hidden');
  }

  // ------------------------------------------------------------------
  // Global tenant search (Spec §1 "全局搜索"): Enter jumps straight to the
  // tenant-detail page by slug/name/corp_id prefix, bypassing the list
  // filters. There is no dedicated search endpoint yet (Spec §6 gap) — this
  // calls the tenant list with a best-effort text filter client-side isn't
  // possible without an endpoint, so it degrades to submitting the raw
  // query as a filter on the assumed `GET .../tenants/search?q=` contract
  // and, on 404, falls back to the tenant list page with the query
  // preserved so staff can filter manually.
  function wireGlobalSearch() {
    var input = el('platform-search');
    if (!input) { return; }
    // GH-199 follow-up (Haisu): search lives on its own page now — the
    // topbar box just carries the query over to it.
    input.addEventListener('keydown', function (event) {
      if (event.key !== 'Enter') { return; }
      var q = input.value.trim();
      if (!q) { return; }
      window.location.href = '/platform/search?q=' + encodeURIComponent(q);
    });
  }

  function wireNavToggle() {
    var toggle = el('platform-nav-toggle'), nav = document.querySelector('.side-nav');
    if (!toggle || !nav) { return; }
    toggle.hidden = false;
    toggle.addEventListener('click', function () {
      var open = nav.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    nav.querySelectorAll('.side-nav-item').forEach(function (link) {
      link.addEventListener('click', function () { nav.classList.remove('is-open'); });
    });
  }

  function wireRefreshStamp(onRefresh) {
    var button = el('platform-refresh'), stamp = el('platform-last-refresh');
    function mark() { if (stamp) { stamp.textContent = '最近刷新 ' + date(new Date()).slice(11); } }
    if (button && typeof onRefresh === 'function') {
      button.addEventListener('click', function () { onRefresh(); mark(); });
    }
    mark();
  }

  document.addEventListener('DOMContentLoaded', function () {
    wireGlobalSearch();
    wireNavToggle();
  });

  window.PC = {
    el: el, clear: clear, number: number, money: money, date: date, dateOnly: dateOnly, bytes: bytes,
    error: error, request: request, empty: empty, emptyRow: emptyRow, gapRow: gapRow, list: list,
    textPair: textPair, rowFrom: rowFrom, actionButton: actionButton, badge: badge,
    operationKey: operationKey, reasonCodeValid: reasonCodeValid,
    openModal: openModal, closeModal: modalClose, fieldWrap: fieldWrap,
    wireRefreshStamp: wireRefreshStamp
  };
}());
