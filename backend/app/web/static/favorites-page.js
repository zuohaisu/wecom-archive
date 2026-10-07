(function () {
  "use strict";

  var state = {
    limit: 50,
    offset: 0,
    total: 0,
    items: [],
    selected: Object.create(null),
    requestId: 0,
    loading: false,
    feedback: "",
    canManage: false,
    canExport: false,
    permissionMessage: "favoritesPage.permissionUnavailable",
  };

  function t(key) {
    return I18N.t(key);
  }

  function text(key, values) {
    var value = t(key);
    Object.keys(values || {}).forEach(function (name) {
      value = value.replace("{" + name + "}", String(values[name]));
    });
    return value;
  }

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function identity(name, id) {
    return name || id || t("favoritesPage.identityFallback");
  }

  function formatDate(value) {
    if (value == null || value === "") return "—";
    var date = new Date(value);
    return isNaN(date.getTime()) ? "—" : date.toLocaleString(I18N.getLocale());
  }

  function localDayBoundary(value, end) {
    if (!value) return null;
    var date = new Date(value + "T00:00:00");
    if (isNaN(date.getTime())) return null;
    if (end) {
      date.setDate(date.getDate() + 1);
      date.setMilliseconds(date.getMilliseconds() - 1);
    }
    return date;
  }

  function buildQuery() {
    var params = new URLSearchParams();
    var conversation = document.getElementById("favorites-filter-conversation").value.trim();
    var staff = document.getElementById("favorites-filter-staff").value.trim();
    var contact = document.getElementById("favorites-filter-contact").value.trim();
    var mediaType = document.getElementById("favorites-filter-media-type").value;
    var actor = document.getElementById("favorites-filter-actor").value.trim();
    var favoriteSince = localDayBoundary(document.getElementById("favorites-filter-favorite-since").value, false);
    var favoriteUntil = localDayBoundary(document.getElementById("favorites-filter-favorite-until").value, true);
    var messageSince = localDayBoundary(document.getElementById("favorites-filter-message-since").value, false);
    var messageUntil = localDayBoundary(document.getElementById("favorites-filter-message-until").value, true);
    if (conversation) params.set("conversation_id", conversation);
    if (staff) params.set("staff_filter", staff);
    if (contact) params.set("contact_filter", contact);
    if (mediaType === "message") params.set("object_type", "message");
    else if (mediaType) params.set("media_type", mediaType);
    if (actor) params.set("favorited_by_name", actor);
    if (favoriteSince) params.set("favorited_since", favoriteSince.toISOString());
    if (favoriteUntil) params.set("favorited_until", favoriteUntil.toISOString());
    if (messageSince) params.set("message_since_ms", String(messageSince.getTime()));
    if (messageUntil) params.set("message_until_ms", String(messageUntil.getTime()));
    params.set("limit", String(state.limit));
    params.set("offset", String(state.offset));
    return params.toString();
  }

  function isSupportedMediaType(type) {
    return ["image", "video", "voice", "file"].indexOf(type) !== -1;
  }

  function favoriteStatus(item) {
    if (item.object_type === "message") return t("favoritesPage.available");
    if (item.media_download_status === "downloaded" && isSupportedMediaType(item.media_type)) return t("favoritesPage.mediaReady");
    if (item.media_download_status === "downloaded") return t("favoritesPage.mediaUnsupported");
    if (item.media_download_status === "pending") return t("favoritesPage.mediaPending");
    if (item.media_download_status === "failed") return t("favoritesPage.mediaFailed");
    if (item.media_download_status === "unsupported") return t("favoritesPage.mediaUnsupported");
    return t("favoritesPage.mediaUnknown");
  }

  function locateHref(item) {
    if (!item.conversation_id || !item.message_id || !item.focus_entity_id || !item.focus_entity_type) return null;
    var params = new URLSearchParams();
    params.set("focus", item.message_id);
    params.set("conv", item.conversation_id);
    if (item.conversation_type) params.set("convType", item.conversation_type);
    params.set("entityId", item.focus_entity_id);
    params.set("entityType", item.focus_entity_type);
    return "/admin/conversations?" + params.toString();
  }

  function mediaAccessUrl(item) {
    var params = new URLSearchParams();
    if (item.focus_entity_type) {
      params.set("mode", item.focus_entity_type);
      if (item.focus_entity_type === "staff") params.set("staff_id", item.focus_entity_id);
      else params.set("contact_id", item.focus_entity_id);
    }
    if (item.conversation_type) params.set("conversation_type", item.conversation_type);
    var query = params.toString();
    return "/api/conversations/" + encodeURIComponent(item.conversation_id)
      + "/messages/" + encodeURIComponent(item.message_id) + "/media/access"
      + (query ? "?" + query : "");
  }

  function translatePage() {
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll("[data-i18n]").forEach(function (element) {
      element.textContent = t(element.getAttribute("data-i18n"));
    });
    document.title = t("favoritesPage.title");
  }

  function renderLanguageMenu() {
    var menu = document.getElementById("lang-menu");
    if (!menu) return;
    menu.innerHTML = I18N.availableLocales().map(function (locale) {
      return '<div class="lang-option' + (locale.code === I18N.getLocale() ? " active" : "")
        + '" data-locale="' + escapeHtml(locale.code) + '">' + escapeHtml(locale.nativeName) + "</div>";
    }).join("");
  }

  window.toggleLangMenu = function () {
    var menu = document.getElementById("lang-menu");
    if (menu) menu.style.display = menu.style.display === "none" ? "block" : "none";
    renderLanguageMenu();
  };
  window.doLogout = function () {
    fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" }).finally(function () {
      window.location = "/admin/login";
    });
  };
  document.addEventListener("click", function (event) {
    var option = event.target.closest(".lang-option");
    if (!option) return;
    var locale = option.getAttribute("data-locale");
    I18N.setLocale(locale);
    var menu = document.getElementById("lang-menu");
    if (menu) menu.style.display = "none";
    fetch("/api/auth/me/preferences", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locale: locale }),
    }).catch(function () {});
  });

  function setError(message) {
    var error = document.getElementById("favorites-error");
    error.textContent = message || "";
    error.classList.toggle("hidden", !message);
    document.getElementById("favorites-retry").classList.toggle("hidden", !message);
  }

  function setLoading(loading) {
    state.loading = loading;
    var wrap = document.getElementById("favorites-table-wrap");
    wrap.setAttribute("aria-busy", loading ? "true" : "false");
    document.getElementById("favorites-previous").disabled = loading || state.offset <= 0;
    document.getElementById("favorites-next").disabled = loading || state.offset + state.items.length >= state.total;
    document.getElementById("favorites-unfavorite-selected").disabled = loading || !state.canManage || selectedItems().length === 0;
  }

  function selectedItems() {
    return state.items.filter(function (item) { return !!state.selected[item.favorite_id]; });
  }

  function updateSelectionToolbar() {
    var items = selectedItems();
    var toolbar = document.getElementById("favorites-selection");
    toolbar.classList.toggle("hidden", items.length === 0);
    document.getElementById("favorites-selected-count").textContent = text("favoritesPage.selectedCount", { n: items.length });
    document.getElementById("favorites-unfavorite-selected").disabled = state.loading || !state.canManage || items.length === 0;
    var exportButton = document.getElementById("favorites-export-selected");
    exportButton.classList.toggle("hidden", !state.canExport);
    exportButton.disabled = state.loading || !state.canExport || !items.some(function (item) { return !!item.message_id; });
    var pageCheckboxes = document.querySelectorAll("[data-favorite-select]");
    document.getElementById("favorites-select-page").disabled = state.loading || !state.canManage || !pageCheckboxes.length;
  }

  function renderRows() {
    var rows = document.getElementById("favorites-rows");
    rows.innerHTML = state.items.map(function (item) {
      var locate = locateHref(item);
      var sender = identity(item.sender_name, item.sender_id);
      var actor = identity(item.favorited_by_name, item.favorited_by_admin_user_id);
      var conversation = identity(item.conversation_name, item.conversation_id);
      var mediaLabelKey = { image: "media.type.image", video: "media.type.video", voice: "media.type.voice", file: "media.type.file" }[item.media_type];
      var summary = item.object_type === "media"
        ? (item.preview || t(mediaLabelKey || "favoritesPage.mediaTypeLabel"))
        : (item.preview || t("favoritesPage.messageType"));
      var locator = locate
        ? '<a class="btn btn-secondary btn-sm" href="' + escapeHtml(locate) + '">' + escapeHtml(t("favoritesPage.locateMessage")) + "</a>"
        : '<span class="favorites-unavailable" title="' + escapeHtml(t("favoritesPage.locateUnavailable")) + '">' + escapeHtml(t("favoritesPage.locateUnavailable")) + "</span>";
      var preview = "";
      if (item.object_type === "media") {
        if (item.media_download_status === "downloaded" && isSupportedMediaType(item.media_type) && item.conversation_id && item.message_id && item.focus_entity_id && item.focus_entity_type) {
          preview = '<button class="btn btn-secondary btn-sm" type="button" data-favorites-preview="' + escapeHtml(item.favorite_id) + '">' + escapeHtml(t("favoritesPage.previewMedia")) + "</button>";
        } else {
          preview = '<span class="favorites-unavailable">' + escapeHtml(t("favoritesPage.previewUnavailable")) + "</span>";
        }
      }
      return '<tr data-favorite-row="' + escapeHtml(item.favorite_id) + '">'
        + '<td><input type="checkbox" data-favorite-select="' + escapeHtml(item.favorite_id) + '" aria-label="' + escapeHtml(t("favoritesPage.selectItem") + ": " + conversation) + '"' + (state.canManage ? "" : " disabled") + (state.selected[item.favorite_id] ? " checked" : "") + '></td>'
        + '<td><span class="favorites-status-chip">' + escapeHtml(favoriteStatus(item)) + '</span><span class="favorites-kind">' + escapeHtml(t(item.object_type === "media" ? "favoritesPage.mediaTypeLabel" : "favoritesPage.messageType")) + '</span></td>'
        + '<td><strong>' + escapeHtml(conversation) + '</strong>' + (item.conversation_type ? '<span class="favorites-meta">' + escapeHtml(item.conversation_type) + '</span>' : "") + '<div class="favorites-row-actions">' + locator + (preview ? " " + preview : "") + '</div></td>'
        + '<td class="favorites-summary">' + escapeHtml(summary) + '</td>'
        + '<td>' + escapeHtml(sender) + '</td>'
        + '<td>' + escapeHtml(actor) + '</td>'
        + '<td>' + escapeHtml(formatDate(item.favorited_at)) + '</td>'
        + '<td>' + escapeHtml(formatDate(item.message_time_ms)) + '</td>'
        + '<td><button class="btn btn-secondary btn-sm" type="button" data-favorites-unfavorite="' + escapeHtml(item.favorite_id) + '" aria-label="' + escapeHtml(t("favoritesPage.unfavorite") + ": " + conversation) + '"' + (state.canManage ? "" : " disabled") + '>' + escapeHtml(t("favoritesPage.unfavorite")) + '</button></td>'
        + '</tr>';
    }).join("");
    rows.querySelectorAll("[data-favorite-select]").forEach(function (checkbox) {
      checkbox.addEventListener("change", function () {
        var id = checkbox.getAttribute("data-favorite-select");
        if (checkbox.checked) state.selected[id] = true;
        else delete state.selected[id];
        updateSelectionToolbar();
      });
    });
    rows.querySelectorAll("[data-favorites-preview]").forEach(function (button) {
      button.addEventListener("click", function () {
        var item = state.items.find(function (candidate) { return candidate.favorite_id === button.getAttribute("data-favorites-preview"); });
        if (!item || typeof openViewer !== "function") return;
        openViewer([{
          kind: item.media_type || "file",
          accessUrl: mediaAccessUrl(item),
          downloadUrl: "/api/admin/media/" + encodeURIComponent(item.media_file_id) + "/download",
          label: t("media.type." + (item.media_type || "file")),
          mimeType: item.media_mime_type,
          sizeBytes: item.media_size_bytes,
        }], 0);
      });
    });
    rows.querySelectorAll("[data-favorites-unfavorite]").forEach(function (button) {
      button.addEventListener("click", function () {
        var item = state.items.find(function (candidate) { return candidate.favorite_id === button.getAttribute("data-favorites-unfavorite"); });
        if (item) runUnfavorite([item]);
      });
    });
    var empty = state.items.length === 0 && !state.loading;
    document.getElementById("favorites-empty").classList.toggle("hidden", !empty);
    document.getElementById("favorites-results").textContent = text("favoritesPage.results", {
      from: state.total ? state.offset + 1 : 0,
      to: state.offset + state.items.length,
      total: state.total,
    });
    document.getElementById("favorites-previous").disabled = state.loading || state.offset <= 0;
    document.getElementById("favorites-next").disabled = state.loading || state.offset + state.items.length >= state.total;
    updateSelectionToolbar();
  }

  function loadPage() {
    var requestId = ++state.requestId;
    setError("");
    setLoading(true);
    document.getElementById("favorites-status").textContent = state.feedback || t("favoritesPage.loading");
    return fetch("/api/favorites?" + buildQuery(), { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) {
          var error = new Error("favorites_load_failed");
          error.status = response.status;
          throw error;
        }
        return response.json();
      })
      .then(function (page) {
        if (requestId !== state.requestId) return;
        state.items = Array.isArray(page.items) ? page.items : [];
        state.total = Number(page.total) || 0;
        state.offset = Number(page.offset) || 0;
        if (!state.items.length && state.offset > 0 && state.offset >= state.total) {
          state.offset = Math.max(0, state.total - (state.total % state.limit || state.limit));
          return loadPage();
        }
        document.getElementById("favorites-status").textContent = state.feedback;
        setLoading(false);
        renderRows();
      })
      .catch(function (error) {
        if (requestId !== state.requestId) return;
        document.getElementById("favorites-status").textContent = "";
        setLoading(false);
        setError(t(error && error.status === 422 ? "favoritesPage.invalidFilters" : "favoritesPage.loadFailed"));
        renderRows();
      });
  }

  function runUnfavorite(items) {
    if (!items.length || state.loading) return;
    var requestId = ++state.requestId;
    setError("");
    setLoading(true);
    state.feedback = "";
    document.getElementById("favorites-status").textContent = t("favoritesPage.actionPending");
    return fetch("/api/favorites/batch", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "unfavorite",
        items: items.map(function (item) {
          return { object_type: item.object_type, object_id: item.object_id, source_page: "favorites" };
        }),
      }),
    })
      .then(function (response) {
        if (!response.ok) throw new Error("favorite_action_failed");
        return response.json();
      })
      .then(function (result) {
        if (requestId !== state.requestId) return;
        state.selected = Object.create(null);
        state.feedback = text("favoritesPage.actionResult", {
          applied: result.applied || 0,
          unchanged: result.unchanged || 0,
          notFound: result.not_found || 0,
        });
        document.getElementById("favorites-status").textContent = state.feedback;
        return loadPage().then(function () {
          var status = document.getElementById("favorites-status");
          if (status && typeof status.focus === "function") status.focus();
        });
      })
      .catch(function () {
        if (requestId !== state.requestId) return;
        state.feedback = "";
        setLoading(false);
        document.getElementById("favorites-status").textContent = "";
        setError(t("favoritesPage.actionFailed"));
        var errorMessage = document.getElementById("favorites-error");
        if (errorMessage && typeof errorMessage.focus === "function") errorMessage.focus();
      });
  }

  document.getElementById("favorites-filters").addEventListener("submit", function (event) {
    event.preventDefault();
    state.offset = 0;
    state.selected = Object.create(null);
    state.feedback = "";
    loadPage();
  });
  document.getElementById("favorites-clear-filters").addEventListener("click", function () {
    document.getElementById("favorites-filters").reset();
    state.offset = 0;
    state.selected = Object.create(null);
    state.feedback = "";
    loadPage();
  });
  document.getElementById("favorites-previous").addEventListener("click", function () {
    state.offset = Math.max(0, state.offset - state.limit);
    state.selected = Object.create(null);
    state.feedback = "";
    loadPage();
  });
  document.getElementById("favorites-next").addEventListener("click", function () {
    if (state.offset + state.items.length < state.total) {
      state.offset += state.limit;
      state.selected = Object.create(null);
      state.feedback = "";
      loadPage();
    }
  });
  document.getElementById("favorites-select-page").addEventListener("click", function () {
    state.items.forEach(function (item) { state.selected[item.favorite_id] = true; });
    renderRows();
  });
  document.getElementById("favorites-clear-selection").addEventListener("click", function () {
    state.selected = Object.create(null);
    renderRows();
  });
  document.getElementById("favorites-export-selected").addEventListener("click", function () {
    var messageIds = Array.from(new Set(selectedItems().map(function (item) { return item.message_id; }).filter(Boolean))).sort();
    if (!messageIds.length) {
      document.getElementById("favorites-status").textContent = t("favoritesPage.exportUnavailable");
      return;
    }
    try {
      sessionStorage.setItem("wecom.exportPrefill", JSON.stringify({ message_ids: messageIds }));
    } catch (error) {
      document.getElementById("favorites-status").textContent = t("favoritesPage.exportFailed");
      return;
    }
    document.getElementById("favorites-status").textContent = t("favoritesPage.exportPreparing");
    window.location.href = "/admin/exports";
  });
  document.getElementById("favorites-unfavorite-selected").addEventListener("click", function () {
    runUnfavorite(selectedItems());
  });
  document.getElementById("favorites-retry").addEventListener("click", loadPage);

  function loadIdentity() {
    fetch("/api/auth/me", { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("identity_request_failed");
      return response.json();
    }).then(function (identity) {
      if (!identity.authenticated) {
        window.location = "/admin/login";
        return;
      }
      state.canManage = ["owner", "admin", "compliance", "legal"].indexOf(identity.role) !== -1;
      state.canExport = identity.role === "owner";
      document.getElementById("current-user").textContent = identity.display_name || "";
      state.permissionMessage = state.canManage ? "" : "favoritesPage.permissionDenied";
      document.getElementById("favorites-permission").textContent = state.permissionMessage ? t(state.permissionMessage) : "";
      renderRows();
    }).catch(function () {
      state.canManage = false;
      state.canExport = false;
      state.permissionMessage = "favoritesPage.permissionUnavailable";
      document.getElementById("favorites-permission").textContent = t(state.permissionMessage);
      renderRows();
    });
  }

  I18N.onChange(function () {
    translatePage();
    renderLanguageMenu();
    document.getElementById("favorites-permission").textContent = state.permissionMessage ? t(state.permissionMessage) : "";
    if (state.feedback) document.getElementById("favorites-status").textContent = state.feedback;
    renderRows();
  });
  translatePage();
  renderLanguageMenu();
  loadIdentity();
  loadPage();
})();
