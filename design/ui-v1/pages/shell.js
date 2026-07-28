/* Crowntime WeCom Archive — shared shell (vanilla JS, no framework).
   Renders the side nav from one config so every page stays in sync.
   In production this becomes a Jinja include; the markup is identical. */
(function () {
  var NAV = [
    { group: '概览', items: [
      { id: 'dashboard', label: '总览', href: 'dashboard.html' },
      { id: 'analytics', label: '用量分析', href: 'analytics.html' }
    ]},
    { group: '审阅', items: [
      { id: 'review', label: '对话审阅', href: '../reference/review_console.html' },
      { id: 'search', label: '全局搜索', href: 'search-advanced.html' },
      { id: 'audit', label: '审计日志', href: 'audit-log.html' }
    ]},
    { group: '数据', items: [
      { id: 'messages', label: '消息记录', href: '../reference/messages.html' },
      { id: 'media', label: '媒体与附件', href: 'media.html' },
      { id: 'exports', label: '导出记录', disabled: true, note: '即将推出' }
    ]},
    { group: '通讯录', items: [
      { id: 'users', label: '用户管理', href: 'users.html' },
      { id: 'contacts', label: '外部联系人', href: 'contacts.html' }
    ]},
    { group: '系统', items: [
      { id: 'diagnostics', label: '消息可达性诊断', href: '../reference/diagnostics.html' },
      { id: 'settings', label: '设置', href: 'settings.html' }
    ]},
    { group: '平台', items: [
      { id: 'platform', label: '平台控制台', href: 'platform.html', note: '超管' },
      { id: 'tenant-provisioning', label: '开通配置', href: 'tenant-provisioning.html', note: '超管' }
    ]}
  ];

  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]; }); }

  function navHTML(active) {
    var out = '<div class="sidenav-brand"><img class="sidenav-logo" src="../brand/icon-tile.svg" alt="康冠时代" width="24" height="24"><div class="sidenav-title">会话存档控制台</div></div><div class="sidenav-scroll">';
    NAV.forEach(function (g) {
      out += '<div class="sidenav-group">' + esc(g.group) + '</div>';
      g.items.forEach(function (it) {
        var note = it.note ? '<em>' + esc(it.note) + '</em>' : '';
        if (it.disabled) {
          out += '<span class="sidenav-item is-disabled" aria-disabled="true"><span>' + esc(it.label) + '</span>' + note + '</span>';
        } else {
          out += '<a class="sidenav-item' + (it.id === active ? ' active' : '') + '" href="' + it.href + '"' +
            (it.id === active ? ' aria-current="page"' : '') + '><span>' + esc(it.label) + '</span>' + note + '</a>';
        }
      });
    });
    out += '</div><div class="sidenav-foot">' +
      '<div class="sidenav-user"><span class="avatar avatar-sm">陈</span><span class="sidenav-user-name">陈静 · 合规管理员</span></div>' +
      '<div class="sidenav-actions">' +
      '<button class="btn btn-sm btn-ghost" type="button" data-theme-toggle>深色</button>' +
      '<button class="btn btn-sm btn-ghost" type="button" data-lang-toggle>EN</button>' +
      '<a class="btn btn-sm btn-ghost" href="login.html">退出</a>' +
      '</div></div>';
    return out;
  }

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    Array.prototype.forEach.call(document.querySelectorAll('[data-theme-toggle]'), function (b) {
      b.textContent = t === 'dark' ? '浅色' : '深色';
    });
  }

  function init() {
    var host = document.querySelector('[data-sidenav]');
    if (host) { host.className = 'sidenav'; host.innerHTML = navHTML(host.getAttribute('data-sidenav')); }

    var saved = null;
    try { saved = localStorage.getItem('ct-theme'); } catch (e) {}
    applyTheme(saved === 'dark' ? 'dark' : 'light');

    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-theme-toggle]');
      if (t) {
        var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        applyTheme(next);
        try { localStorage.setItem('ct-theme', next); } catch (err) {}
      }
      var m = e.target.closest('[data-modal-open]');
      if (m) { var el = document.getElementById(m.getAttribute('data-modal-open')); if (el) el.classList.remove('hidden'); }
      var c = e.target.closest('[data-modal-close]');
      if (c) { var p = c.closest('.modal-backdrop'); if (p) p.classList.add('hidden'); }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
