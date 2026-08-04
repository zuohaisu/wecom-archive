/* Static demo shell and local-only interactions. It never sends a network request. */
(function () {
  "use strict";

  var data = window.CrowntimeDemoData || { conversations: [], searchResults: [] };
  var page = document.body.getAttribute("data-demo-page") || "overview";
  var navGroups = [
    { label: "总览", items: [{ id: "overview", label: "Demo 总览", href: "index.html" }] },
    { label: "审阅", items: [
      { id: "conversations", label: "对话审阅", href: "conversations.html" },
      { id: "search", label: "全局搜索", href: "search.html" }
    ] },
    { label: "数据", items: [
      { id: "contacts", label: "外部联系人", href: "contacts.html" },
      { id: "media", label: "媒体与附件", href: "media.html" },
      { id: "analytics", label: "用量分析", href: "analytics.html" }
    ] },
    { label: "安全", items: [{ id: "audit", label: "审计日志", href: "audit-log.html" }] }
  ];

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character];
    });
  }

  function navHtml() {
    var groups = navGroups.map(function (group) {
      var items = group.items.map(function (item) {
        var active = item.id === page ? " is-active" : "";
        return '<a class="demo-nav-item' + active + '" href="' + item.href + '"' +
          (item.id === page ? ' aria-current="page"' : "") + '><i class="demo-nav-dot"></i>' +
          escapeHtml(item.label) + "</a>";
      }).join("");
      return '<div class="demo-nav-group">' + escapeHtml(group.label) + "</div>" + items;
    }).join("");
    return '<a class="demo-brand" href="index.html" aria-label="Demo 总览">' +
      '<img src="../brand/logo-icon.svg" alt="" width="27" height="27">' +
      '<span><span class="demo-brand-name">康冠时代</span><span class="demo-brand-sub">会话存档 Demo</span></span></a>' +
      '<nav class="demo-nav" aria-label="Demo 导航">' + groups + "</nav>" +
      '<div class="demo-sidebar-foot"><div class="demo-demo-badge"><i></i><span>静态演示环境<br>全部内容为虚构示例</span></div></div>';
  }

  function topbarHtml() {
    return '<div class="demo-crumb"><a href="../index.html">官网</a> <span> / </span> <strong>产品 Demo</strong></div>' +
      '<div class="demo-topbar-actions">' +
      '<button class="demo-btn demo-btn-quiet" type="button" data-demo-theme-toggle aria-pressed="false" aria-label="切换为深色主题">深色</button>' +
      '<a class="demo-btn" href="../index.html#pricing">查看套餐</a>' +
      '<a class="demo-btn demo-btn-primary" href="mailto:hs@crowntime.cn?subject=%E5%BA%B7%E5%86%A0%E6%97%B6%E4%BB%A3%E4%BC%81%E4%B8%9A%E5%BE%AE%E4%BF%A1%E4%BC%9A%E8%AF%9D%E5%AD%98%E6%A1%A3%E5%92%A8%E8%AF%A2">咨询开通</a>' +
      "</div>";
  }

  function showToast(message) {
    var toast = document.getElementById("demo-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "demo-toast";
      toast.className = "demo-toast";
      document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.classList.add("is-visible");
    window.clearTimeout(showToast.timeout);
    showToast.timeout = window.setTimeout(function () { toast.classList.remove("is-visible"); }, 3000);
  }

  function applyTheme(theme) {
    var isDark = theme === "dark";
    document.documentElement.setAttribute("data-demo-theme", theme);
    Array.prototype.forEach.call(document.querySelectorAll("[data-demo-theme-toggle]"), function (button) {
      button.textContent = isDark ? "浅色" : "深色";
      button.setAttribute("aria-pressed", String(isDark));
      button.setAttribute("aria-label", isDark ? "切换为浅色主题" : "切换为深色主题");
    });
  }

  function setupTheme() {
    applyTheme("light");
  }

  function renderConversation(conversationId) {
    var conversation = data.conversations.filter(function (item) { return item.id === conversationId; })[0] || data.conversations[0];
    if (!conversation) return;
    var list = document.getElementById("demo-conversation-list");
    var timeline = document.getElementById("demo-timeline");
    var title = document.getElementById("demo-timeline-title");
    var info = document.getElementById("demo-conversation-info");

    if (list) {
      list.innerHTML = data.conversations.map(function (item) {
        var active = item.id === conversation.id ? " is-active" : "";
        return '<button class="demo-conversation-item' + active + '" type="button" data-demo-conversation="' + escapeHtml(item.id) + '">' +
          '<span class="demo-conversation-name">' + escapeHtml(item.name) + '<time class="demo-conversation-time">' + escapeHtml(item.time) + "</time></span>" +
          '<span class="demo-conversation-snippet">' + escapeHtml(item.snippet) + "</span>" +
          '<span class="demo-conversation-tag">' + escapeHtml(item.type) + "</span></button>";
      }).join("");
    }
    if (title) title.textContent = conversation.name;
    if (timeline) {
      timeline.innerHTML = '<div class="demo-day-divider">2026 年 8 月 3 日</div>' + conversation.messages.map(function (message) {
        var self = message.self ? " is-self" : "";
        var body = message.file
          ? '<div class="demo-bubble demo-bubble-file"><span class="demo-file-glyph">PDF</span><span><b>' + escapeHtml(message.file) + '</b><br><small>' + escapeHtml(message.meta) + "</small></span></div>"
          : '<div class="demo-bubble">' + escapeHtml(message.text) + "</div>";
        return '<article class="demo-message' + self + '"><div class="demo-message-meta">' + escapeHtml(message.sender) + " · " + escapeHtml(message.time) + "</div>" + body + "</article>";
      }).join("");
    }
    if (info) {
      var stats = conversation.stats.map(function (stat) {
        return '<div class="demo-info-stat"><span>' + escapeHtml(stat[0]) + "</span><b>" + escapeHtml(stat[1]) + "</b></div>";
      }).join("");
      var participants = conversation.participants.map(function (participant, index) {
        var classes = ["", " alt", " mint", " gray"];
        return '<div class="demo-participant"><span class="demo-avatar' + classes[index % classes.length] + '">' + escapeHtml(participant[2]) + "</span>" +
          '<span class="demo-participant-name">' + escapeHtml(participant[0]) + "</span><span class=\"demo-participant-tag\">" + escapeHtml(participant[1]) + "</span></div>";
      }).join("");
      info.innerHTML = '<section class="demo-info-section"><h3>会话概览</h3><div class="demo-info-kv">' + stats + "</div></section>" +
        '<section class="demo-info-section"><h3>参与人</h3>' + participants + "</section>" +
        '<section class="demo-info-section"><h3>审计提示</h3><span class="demo-hash">所有查看、检索与导出动作均可留痕。<br>demo_hash: 9fd0…c18a</span></section>';
    }
  }

  function highlight(value, keyword) {
    var escaped = escapeHtml(value);
    var safeKeyword = String(keyword || "").trim();
    if (!safeKeyword) return escaped;
    var escapedKeyword = escapeHtml(safeKeyword);
    return escaped.split(escapedKeyword).join("<mark>" + escapedKeyword + "</mark>");
  }

  function renderSearch(keyword) {
    var host = document.getElementById("demo-search-results");
    var count = document.getElementById("demo-search-count");
    if (!host) return;
    var query = String(keyword || "").trim().toLowerCase();
    var matches = data.searchResults.filter(function (item) {
      return !query || [item.conversation, item.sender, item.text].join(" ").toLowerCase().indexOf(query) !== -1;
    });
    if (count) count.textContent = "命中 " + matches.length + " 条（演示数据）";
    host.innerHTML = matches.length ? matches.map(function (result) {
      return '<article class="demo-search-result"><div class="demo-search-result-head"><span class="demo-tag demo-tag-blue">会话</span><b>' + escapeHtml(result.conversation) + "</b><time>" + escapeHtml(result.time) + "</time></div>" +
        '<p>' + highlight(result.text, keyword) + "</p>" +
        '<div class="demo-search-result-foot"><span>' + escapeHtml(result.sender) + "</span><span>·</span><span>" + escapeHtml(result.id) + "</span></div></article>";
    }).join("") : '<div class="demo-card-body demo-meta">没有匹配的演示结果，请尝试“归档”或“审计”。</div>';
  }

  function setupInteractiveElements() {
    document.addEventListener("click", function (event) {
      var themeButton = event.target.closest("[data-demo-theme-toggle]");
      if (themeButton) {
        var next = document.documentElement.getAttribute("data-demo-theme") === "dark" ? "light" : "dark";
        applyTheme(next);
        return;
      }
      var conversationButton = event.target.closest("[data-demo-conversation]");
      if (conversationButton) {
        renderConversation(conversationButton.getAttribute("data-demo-conversation"));
        return;
      }
      var chip = event.target.closest("[data-demo-chip]");
      if (chip) {
        chip.classList.toggle("is-active");
        return;
      }
      var modalOpen = event.target.closest("[data-demo-modal-open]");
      if (modalOpen) {
        var modal = document.getElementById(modalOpen.getAttribute("data-demo-modal-open"));
        if (modal) modal.hidden = false;
        return;
      }
      var modalClose = event.target.closest("[data-demo-modal-close]");
      if (modalClose) {
        var dialog = modalClose.closest(".demo-modal-backdrop");
        if (dialog) dialog.hidden = true;
        return;
      }
      var noop = event.target.closest("[data-demo-noop]");
      if (noop) {
        event.preventDefault();
        showToast("这是静态演示站：不会执行同步、下载、导出或任何写入操作。");
      }
    });

    var searchInput = document.getElementById("demo-search-input");
    if (searchInput) {
      renderSearch(searchInput.value);
      searchInput.addEventListener("input", function () { renderSearch(searchInput.value); });
    }
  }

  function init() {
    var nav = document.querySelector("[data-demo-nav]");
    var topbar = document.querySelector("[data-demo-topbar]");
    if (nav) nav.innerHTML = navHtml();
    if (topbar) topbar.innerHTML = topbarHtml();
    setupTheme();
    renderConversation("orbit");
    setupInteractiveElements();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
}());
