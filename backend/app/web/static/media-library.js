(function () {
  "use strict";

  var state = {
    offset: 0,
    limit: 24,
    total: 0,
    items: [],
    requestId: 0,
    loading: false,
    selectionMode: false,
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

  function formatSize(bytes) {
    if (bytes == null) return "—";
    var units = ["B", "KB", "MB", "GB"];
    var value = Number(bytes);
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return (unit ? value.toFixed(value >= 10 ? 0 : 1) : value) + " " + units[unit];
  }

  function formatDate(value) {
    var date = new Date(value);
    return isNaN(date) ? "—" : date.toLocaleString(I18N.getLocale());
  }

  function mediaUrl(item) {
    return "/api/conversations/" + encodeURIComponent(item.conversation_id) + "/messages/" + encodeURIComponent(item.msgid) + "/media?variant=thumb";
  }

  function mediaAccessUrl(item) {
    return "/api/conversations/" + encodeURIComponent(item.conversation_id) + "/messages/" + encodeURIComponent(item.msgid) + "/media/access";
  }

  function viewerItem(item) {
    return {
      kind: item.file_type || "file",
      accessUrl: mediaAccessUrl(item),
      downloadUrl: "/api/admin/media/" + encodeURIComponent(item.id) + "/download",
      label: item.name || t("media.unnamed"),
      mimeType: item.mime_type,
      sizeBytes: item.file_size,
    };
  }

  function setStatus(value, error) {
    var el = document.getElementById("media-status");
    el.textContent = value;
    el.className = (error ? "field-error" : "field-help") + " media-status";
  }

  function setActionStatus(value, error) {
    var el = document.getElementById("media-favorite-status");
    el.textContent = value;
    el.className = (error ? "field-error" : "field-help") + " media-status";
  }

  function favoriteIndicator(item) {
    var status = favorites.getStatus(item.id);
    if (!status) {
      return '<span class="media-favorite-state pending" aria-label="' + escapeHtml(t("media.favoriteStatusLoading")) + '">☆</span>';
    }
    if (status.result === "not_found") {
      return '<span class="media-favorite-state unavailable">' + escapeHtml(t("media.favoriteUnavailable")) + "</span>";
    }
    if (status.isFavorited) {
      return '<a class="media-favorite-state is-favorited" href="/admin/favorites" aria-label="' + escapeHtml(t("media.viewFavorites")) + '" title="' + escapeHtml(t("media.viewFavorites")) + '"><span aria-hidden="true">★</span> ' + escapeHtml(t("media.favorited")) + "</a>";
    }
    return '<span class="media-favorite-state not-favorited"><span aria-hidden="true">☆</span> ' + escapeHtml(t("media.notFavorited")) + "</span>";
  }

  function card(item, index) {
    var type = item.file_type || "file";
    var label = t("media.type." + type);
    var name = item.name || t("media.unnamed");
    var url = mediaUrl(item);
    var thumb = type === "image"
      ? '<img src="' + escapeHtml(url) + '" alt="" onerror="this.style.display=&apos;none&apos;">'
      : "<span>" + escapeHtml(label) + "</span>";
    var preview = '<button class="media-preview" type="button" data-media-index="' + index + '" aria-label="' + escapeHtml(t("media.preview")) + '"><div class="media-thumb">' + thumb + '<span class="badge media-kind">' + escapeHtml(label) + "</span></div></button>";
    var selectionLabel = text("media.selectItem", { name: name });
    var checkbox = '<label class="media-select-wrap"><input class="media-selection-checkbox" type="checkbox" data-media-select="' + escapeHtml(item.id) + '" aria-label="' + escapeHtml(selectionLabel) + '"><span class="sr-only">' + escapeHtml(selectionLabel) + "</span></label>";
    return '<article class="media-card" data-media-id="' + escapeHtml(item.id) + '">' + preview + checkbox + '<div class="media-body"><div class="media-name">' + escapeHtml(name) + '</div><div class="media-session">' + escapeHtml(item.session_title || "—") + '</div><div class="media-meta">' + escapeHtml(formatDate(item.created_at)) + '</div><div class="media-favorite-indicator" data-favorite-indicator>' + favoriteIndicator(item) + '</div></div><div class="media-footer"><span>' + escapeHtml(formatSize(item.file_size)) + '</span><span>' + escapeHtml(item.mime_type || "—") + "</span></div></article>";
  }

  function render(items, append) {
    var grid = document.getElementById("media-grid");
    var start = append ? state.items.length : 0;
    if (!append) {
      state.items = items.slice();
      grid.innerHTML = items.length
        ? items.map(function (item, index) { return card(item, index); }).join("")
        : '<p class="empty">' + escapeHtml(t(document.getElementById("media-favorited-only").checked ? "media.favoritesEmpty" : "media.empty")) + "</p>";
    } else {
      state.items = state.items.concat(items);
      grid.insertAdjacentHTML("beforeend", items.map(function (item, index) { return card(item, start + index); }).join(""));
    }
    updateFavoriteIndicators();
    updateSelectionUi();
  }

  function params(offset, limit) {
    var p = new URLSearchParams({
      limit: String(limit == null ? state.limit : limit),
      offset: String(offset == null ? state.offset : offset),
      sort: document.getElementById("media-sort").value,
    });
    var type = document.getElementById("media-type").value;
    var days = document.getElementById("media-days").value;
    var query = document.getElementById("media-search").value.trim();
    if (type) p.set("file_type", type);
    if (days) p.set("days", days);
    if (query) p.set("q", query);
    if (document.getElementById("media-favorited-only").checked) p.set("favorited_only", "true");
    return p;
  }

  function isFavoriteActionBusy() {
    return typeof favorites !== "undefined" && favorites.getSnapshot().busy;
  }

  function updatePageSummary(hasMore) {
    document.getElementById("media-count").textContent = text("media.results", { n: state.total });
    var more = document.getElementById("media-more");
    more.classList.toggle("hidden", !hasMore);
    more.disabled = state.loading || isFavoriteActionBusy() || state.offset >= state.total;
  }

  function load(append) {
    if (state.loading || (append && (state.offset >= state.total || isFavoriteActionBusy()))) return Promise.resolve();
    var requestId = ++state.requestId;
    state.loading = true;
    setStatus(append ? t("media.loadingMore") : "", false);
    document.getElementById("media-grid").setAttribute("aria-busy", "true");
    updatePageSummary(state.offset < state.total);
    var query = params().toString();
    return fetch("/api/admin/media?" + query, { credentials: "include" })
      .then(function (response) {
        if (!response.ok) throw new Error("media_request_failed");
        return response.json();
      })
      .then(function (data) {
        if (requestId !== state.requestId) return;
        state.total = data.total;
        render(data.items, append);
        state.offset += data.items.length;
        updatePageSummary(data.has_more);
        setStatus("");
        return favorites.setItems(state.items).catch(function () {
          setStatus(t("media.favoriteStatusFailed"), true);
        });
      })
      .catch(function () {
        if (requestId === state.requestId) setStatus(t("media.loadFailed"), true);
      })
      .finally(function () {
        if (requestId !== state.requestId) return;
        state.loading = false;
        document.getElementById("media-grid").setAttribute("aria-busy", "false");
        updatePageSummary(state.offset < state.total);
      });
  }

  function reset() {
    state.requestId += 1;
    state.loading = false;
    document.getElementById("media-grid").setAttribute("aria-busy", "false");
    favorites.clearSelection();
    state.items = [];
    state.offset = 0;
    state.total = 0;
    document.getElementById("media-grid").innerHTML = "";
    favorites.setItems([]);
    updatePageSummary(false);
    setActionStatus("");
    document.getElementById("media-favorites-link").classList.add("hidden");
    return load(false);
  }

  function updateFavoriteIndicators() {
    document.querySelectorAll("[data-favorite-indicator]").forEach(function (indicator) {
      var cardElement = indicator.closest("[data-media-id]");
      if (!cardElement) return;
      var item = state.items.find(function (candidate) { return String(candidate.id) === cardElement.dataset.mediaId; });
      if (item) indicator.innerHTML = favoriteIndicator(item);
    });
  }

  function updateSelectionUi(snapshot) {
    snapshot = snapshot || favorites.getSnapshot();
    var grid = document.getElementById("media-grid");
    var modeButton = document.getElementById("media-select-mode");
    var toolbar = document.getElementById("media-selection-toolbar");
    modeButton.setAttribute("aria-pressed", String(state.selectionMode));
    modeButton.textContent = t(state.selectionMode ? "media.exitSelection" : "media.enterSelection");
    toolbar.classList.toggle("hidden", !state.selectionMode);
    grid.classList.toggle("selection-mode", state.selectionMode);
    document.getElementById("media-selection-count").textContent = text("media.selectedCount", { n: snapshot.selectedCount });
    var allButton = document.getElementById("media-select-all");
    allButton.textContent = t(snapshot.allSelected ? "media.clearSelection" : "media.selectAllLoaded");
    allButton.disabled = snapshot.loadedCount === 0 || snapshot.busy || state.loading;
    document.getElementById("media-clear-selection").disabled = snapshot.selectedCount === 0 || snapshot.busy || state.loading;
    document.getElementById("media-favorite-selected").disabled = !snapshot.canApply || state.loading;
    document.getElementById("media-unfavorite-selected").disabled = !snapshot.canApply || state.loading;
    document.getElementById("media-more").disabled = state.loading || snapshot.busy || state.offset >= state.total;
    var hint = document.getElementById("media-selection-hint");
    if (!snapshot.canManage) hint.textContent = t("media.favoritePermissionDenied");
    else if (!snapshot.statusReady) hint.textContent = t("media.favoriteStatusLoading");
    else hint.textContent = "";
    document.querySelectorAll("[data-media-select]").forEach(function (checkbox) {
      var id = checkbox.getAttribute("data-media-select");
      var status = favorites.getStatus(id);
      checkbox.checked = favorites.isSelected(id);
      checkbox.disabled = snapshot.busy || state.loading || !!(status && status.result === "not_found");
      var cardElement = checkbox.closest("[data-media-id]");
      if (cardElement) cardElement.classList.toggle("is-selected", checkbox.checked);
    });
    updateFavoriteIndicators();
  }

  function favoriteRequest(url, options) {
    var requestOptions = Object.assign({ credentials: "include" }, options || {});
    if (requestOptions.body) {
      requestOptions.headers = Object.assign({ "Content-Type": "application/json" }, requestOptions.headers || {});
    }
    return fetch(url, requestOptions).then(function (response) {
      if (!response.ok) throw new Error("favorite_request_failed");
      return response.json();
    });
  }

  var favorites = MediaFavorites.create({ request: favoriteRequest, onChange: updateSelectionUi });

  function favoriteResultMessage(summary, partial) {
    return text(partial ? "media.favoritePartial" : "media.favoriteResult", {
      applied: summary.applied || 0,
      unchanged: summary.unchanged || 0,
      notFound: summary.not_found || 0,
    });
  }

  function showFavoritesLink() {
    document.getElementById("media-favorites-link").classList.remove("hidden");
  }

  function removeUnavailableFavorites(action, summary) {
    var removeIds = Object.create(null);
    if (action === "unfavorite" && document.getElementById("media-favorited-only").checked) {
      state.items.forEach(function (item) {
        var status = favorites.getStatus(item.id);
        if (status && (status.result === "not_found" || !status.isFavorited)) removeIds[String(item.id)] = true;
      });
    } else if (summary && summary.not_found) {
      state.items.forEach(function (item) {
        var status = favorites.getStatus(item.id);
        if (status && status.result === "not_found") removeIds[String(item.id)] = true;
      });
    }
    var removed = Object.keys(removeIds).length;
    if (!removed) return Promise.resolve();

    var scrollY = window.scrollY || 0;
    state.items = state.items.filter(function (item) { return !removeIds[String(item.id)]; });
    state.total = Math.max(0, state.total - removed);
    state.offset = state.items.length;
    render(state.items, false);
    updatePageSummary(state.offset < state.total);
    window.scrollTo(window.scrollX || 0, scrollY);

    var requestId = ++state.requestId;
    var grid = document.getElementById("media-grid");
    var remaining = removed;
    var statusRefreshed = false;
    state.loading = true;
    grid.setAttribute("aria-busy", "true");
    setStatus(t("media.loadingMore"), false);
    updatePageSummary(state.offset < state.total);
    updateSelectionUi();

    function refreshFavoriteStatuses() {
      if (statusRefreshed || requestId !== state.requestId) return Promise.resolve();
      statusRefreshed = true;
      return favorites.setItems(state.items).catch(function () {
        if (requestId === state.requestId) setStatus(t("media.favoriteStatusFailed"), true);
      });
    }
    function fillGap() {
      if (requestId !== state.requestId) return Promise.resolve();
      if (remaining <= 0 || state.offset >= state.total) {
        updatePageSummary(state.offset < state.total);
        return refreshFavoriteStatuses();
      }
      var limit = Math.min(remaining, 200);
      var query = params(state.offset, limit).toString();
      return fetch("/api/admin/media?" + query, { credentials: "include" })
        .then(function (response) {
          if (!response.ok) throw new Error("media_request_failed");
          return response.json();
        })
        .then(function (data) {
          if (requestId !== state.requestId) return;
          var received = data.items || [];
          if (received.length) {
            state.items = state.items.concat(received);
            state.offset += received.length;
            remaining -= received.length;
            render(state.items, false);
          } else {
            remaining = 0;
          }
          state.total = data.total;
          return received.length === limit && remaining > 0 ? fillGap() : refreshFavoriteStatuses();
        });
    }
    return fillGap().catch(function () {
      if (requestId !== state.requestId) return;
      setStatus(t("media.loadFailed"), true);
      updatePageSummary(state.offset < state.total);
    }).finally(function () {
      if (requestId !== state.requestId) return;
      state.loading = false;
      grid.setAttribute("aria-busy", "false");
      updatePageSummary(state.offset < state.total);
      updateSelectionUi();
    });
  }

  function performFavoriteAction(action) {
    setActionStatus("");
    return favorites.apply(action).then(function (summary) {
      setActionStatus(favoriteResultMessage(summary, false));
      showFavoritesLink();
      return removeUnavailableFavorites(action, summary);
    }).catch(function (error) {
      var partial = error.favoriteSummary;
      if (partial && partial.requested > 0) {
        setActionStatus(favoriteResultMessage(partial, true), true);
        showFavoritesLink();
        return removeUnavailableFavorites(action, partial);
      }
      setActionStatus(t("media.favoriteActionFailed"), true);
      return undefined;
    });
  }

  function translate() {
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
    });
    document.title = t("media.pageTitle");
    updateSelectionUi();
  }

  function langMenu() {
    var menu = document.getElementById("lang-menu");
    if (menu) {
      menu.innerHTML = I18N.availableLocales().map(function (locale) {
        return '<div class="lang-option ' + (locale.code === I18N.getLocale() ? "active" : "") + '" data-locale="' + escapeHtml(locale.code) + '">' + escapeHtml(locale.nativeName) + "</div>";
      }).join("");
    }
  }

  window.toggleLangMenu = function () {
    var menu = document.getElementById("lang-menu");
    if (menu) menu.style.display = menu.style.display === "none" ? "block" : "none";
    langMenu();
  };
  window.doLogout = function () {
    fetch("/api/auth/logout", { method: "POST", credentials: "include" }).finally(function () {
      window.location = "/admin/login";
    });
  };

  document.getElementById("media-select-mode").addEventListener("click", function () {
    state.selectionMode = !state.selectionMode;
    if (!state.selectionMode) favorites.clearSelection();
    updateSelectionUi();
    if (state.selectionMode) {
      var checkbox = document.querySelector(".media-selection-checkbox:not(:disabled)");
      if (checkbox) checkbox.focus();
    } else {
      document.getElementById("media-select-mode").focus();
    }
  });
  document.getElementById("media-select-all").addEventListener("click", function () {
    if (favorites.getSnapshot().allSelected) favorites.clearSelection();
    else favorites.selectAllLoaded();
  });
  document.getElementById("media-clear-selection").addEventListener("click", function () {
    favorites.clearSelection();
  });
  document.getElementById("media-favorite-selected").addEventListener("click", function () {
    performFavoriteAction("favorite");
  });
  document.getElementById("media-unfavorite-selected").addEventListener("click", function () {
    performFavoriteAction("unfavorite");
  });
  document.getElementById("media-favorited-only").addEventListener("change", reset);
  ["media-type", "media-days", "media-sort"].forEach(function (id) {
    document.getElementById(id).addEventListener("change", reset);
  });
  document.getElementById("media-search").addEventListener("search", reset);
  document.getElementById("media-search").addEventListener("change", reset);
  document.getElementById("media-more").addEventListener("click", function () {
    favorites.clearSelection();
    load(true);
  });
  document.getElementById("media-grid").addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-media-index]");
    if (!trigger) return;
    var index = Number(trigger.dataset.mediaIndex);
    if (index >= 0 && index < state.items.length) openViewer(state.items.map(viewerItem), index);
  });
  document.getElementById("media-grid").addEventListener("change", function (event) {
    var checkbox = event.target.closest("[data-media-select]");
    if (!checkbox) return;
    favorites.toggleSelected(checkbox.getAttribute("data-media-select"), checkbox.checked);
  });
  document.addEventListener("click", function (event) {
    var option = event.target.closest(".lang-option");
    if (!option) return;
    I18N.setLocale(option.dataset.locale);
    document.getElementById("lang-menu").style.display = "none";
    fetch("/api/auth/me/preferences", {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ locale: option.dataset.locale }),
    }).catch(function () {});
  });
  I18N.onChange(function () {
    translate();
    langMenu();
    refreshViewerLabels();
    reset();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape" || !state.selectionMode) return;
    state.selectionMode = false;
    favorites.clearSelection();
    updateSelectionUi();
    document.getElementById("media-select-mode").focus();
  });

  fetch("/api/auth/me", { credentials: "include" }).then(function (response) {
    if (!response.ok) throw new Error("identity_request_failed");
    return response.json();
  }).then(function (identity) {
    if (!identity.authenticated) {
      window.location = "/admin/login";
      return;
    }
    favorites.setRole(identity.role);
  }).catch(function () {
    favorites.setRole(null);
    setActionStatus(t("media.favoritePermissionUnavailable"), true);
  });

  translate();
  langMenu();
  reset();
}());
