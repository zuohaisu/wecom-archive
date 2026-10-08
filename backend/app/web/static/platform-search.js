/* GH-199 follow-up: standalone tenant search page (Haisu request).
 * Fetches the authoritative tenant list once (RND-311 endpoint) and
 * filters client-side by 名称 / slug / corp_id — server-side search can
 * replace this when tenant volume makes it necessary. Clicking a result
 * opens that tenant's detail page. Built with DOM APIs only: no
 * innerHTML anywhere (XSS discipline shared by all platform JS). */
(function () {
  'use strict';

  var input = document.getElementById('search-input');
  var metaBox = document.getElementById('results-meta');
  var listBox = document.getElementById('results-list');
  var tenants = [];
  var query = '';

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function matches(t) {
    if (!query) { return true; }
    var needle = query.toLowerCase();
    return (t.tenant_name || '').toLowerCase().indexOf(needle) >= 0
      || (t.tenant_slug || '').toLowerCase().indexOf(needle) >= 0
      || (t.corp_id || '').toLowerCase().indexOf(needle) >= 0;
  }

  function render() {
    var rows = tenants.filter(matches);
    metaBox.textContent = '共 ' + rows.length + ' 个客户';
    metaBox.hidden = false;
    listBox.replaceChildren();
    if (!rows.length) {
      listBox.appendChild(el('div', 'result-empty', '没有匹配的客户'));
      return;
    }
    rows.forEach(function (t) {
      var card = el('div', 'result-card');
      card.dataset.tenant = t.tenant_id;
      var name = el('div', 'name');
      name.textContent = (t.tenant_name || '—') + ' ';
      var badge = el('span', t.tenant_is_active ? 'badge badge-success' : 'badge badge-plain',
        t.tenant_is_active ? 'active' : '—');
      name.appendChild(badge);
      card.appendChild(name);
      card.appendChild(el('div', 'meta',
        'slug: ' + (t.tenant_slug || '—') + ' · corp_id: ' + (t.corp_id || '—')));
      card.addEventListener('click', function () {
        window.location.href = '/platform/tenants/' + encodeURIComponent(t.tenant_id);
      });
      listBox.appendChild(card);
    });
  }

  function load() {
    fetch('/api/platform/tenants', { credentials: 'include' }).then(function (r) {
      if (!r.ok) { throw new Error('load_failed'); }
      return r.json();
    }).then(function (data) {
      tenants = data.tenants || [];
      render();
    }).catch(function () {
      metaBox.textContent = '客户列表加载失败，请稍后重试。';
      metaBox.hidden = false;
    });
  }

  function runSearch() {
    query = input.value.trim();
    render();
  }

  input.addEventListener('input', runSearch);
  input.addEventListener('keydown', function (event) {
    if (event.key === 'Enter') { event.preventDefault(); runSearch(); }
  });

  /* The standard platform topbar's 刷新数据 button re-fetches the tenant
     list; guarded so the page still works if platform-console.js is absent. */
  if (window.PC && typeof PC.wireRefreshStamp === 'function') { PC.wireRefreshStamp(load); }

  var params = new URLSearchParams(window.location.search);
  var initialQ = params.get('q');
  if (initialQ) { input.value = initialQ; }
  runSearch();
  load();
}());
