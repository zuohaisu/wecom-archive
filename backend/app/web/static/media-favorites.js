(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MediaFavorites = api;
})(typeof window === "undefined" ? globalThis : window, function () {
  "use strict";

  var WRITE_ROLES = ["owner", "admin", "compliance", "legal"];
  var BATCH_LIMIT = 100;

  function chunks(items, size) {
    var result = [];
    for (var index = 0; index < items.length; index += size) {
      result.push(items.slice(index, index + size));
    }
    return result;
  }

  function emptySummary() {
    return { requested: 0, unique: 0, applied: 0, unchanged: 0, not_found: 0, items: [] };
  }

  function create(options) {
    if (!options || typeof options.request !== "function") {
      throw new TypeError("A favorite API request function is required");
    }

    var request = options.request;
    var onChange = typeof options.onChange === "function" ? options.onChange : function () {};
    var items = [];
    var selected = Object.create(null);
    var statuses = Object.create(null);
    var canManage = false;
    var statusReady = false;
    var busy = false;
    var revision = 0;
    var statusVersions = Object.create(null);

    function bumpStatusVersion(id) {
      var key = String(id);
      statusVersions[key] = (statusVersions[key] || 0) + 1;
      return statusVersions[key];
    }

    function captureStatusVersions(targets) {
      var versions = Object.create(null);
      targets.forEach(function (item) {
        var key = String(item.id);
        versions[key] = bumpStatusVersion(key);
      });
      return versions;
    }

    function statusVersionMatches(id, versions) {
      var key = String(id);
      return Object.prototype.hasOwnProperty.call(versions, key)
        && statusVersions[key] === versions[key];
    }

    function allStatusVersionsMatch(versions) {
      return Object.keys(versions).every(function (id) {
        return statusVersions[id] === versions[id];
      });
    }

    function selectedIds() {
      return Object.keys(selected);
    }

    function snapshot() {
      var ids = selectedIds();
      var selectable = ids.length > 0 && ids.every(function (id) {
        return statuses[id] && statuses[id].result === "found";
      });
      return {
        selectedCount: ids.length,
        loadedCount: items.length,
        allSelected: items.length > 0 && ids.length === items.length,
        canManage: canManage,
        statusReady: statusReady,
        busy: busy,
        canApply: canManage && statusReady && selectable && !busy,
      };
    }

    function notify() {
      onChange(snapshot());
    }

    function setRole(role) {
      canManage = WRITE_ROLES.indexOf(role) !== -1;
      notify();
    }

    function setItems(nextItems) {
      revision += 1;
      var currentRevision = revision;
      items = Array.isArray(nextItems) ? nextItems.slice() : [];
      var versions = captureStatusVersions(items);
      selected = Object.create(null);
      statuses = Object.create(null);
      statusReady = items.length === 0;
      notify();
      if (items.length === 0) return Promise.resolve();

      var batches = chunks(items, BATCH_LIMIT);
      return batches.reduce(function (promise, batch) {
        return promise.then(function () {
          return request("/api/favorites/status", {
            method: "POST",
            body: JSON.stringify({
              items: batch.map(function (item) {
                return {
                  object_type: "media",
                  object_id: String(item.id),
                  source_page: "media",
                };
              }),
            }),
          }).then(function (data) {
            if (!data || !Array.isArray(data.items)) throw new Error("invalid_favorite_status");
            if (currentRevision !== revision) return;
            data.items.forEach(function (item) {
              if (item.object_type !== "media" || typeof item.object_id !== "string") return;
              if (!statusVersionMatches(item.object_id, versions)) return;
              statuses[item.object_id] = {
                result: item.result,
                isFavorited: typeof item.is_favorited === "boolean" ? item.is_favorited : null,
              };
              if (item.result === "not_found") delete selected[item.object_id];
            });
            notify();
          });
        });
      }, Promise.resolve()).then(function () {
        if (currentRevision !== revision) return;
        statusReady = items.every(function (item) {
          var status = statuses[String(item.id)];
          return status && (status.result === "not_found" || typeof status.isFavorited === "boolean");
        });
        notify();
      }).catch(function (error) {
        if (currentRevision === revision && allStatusVersionsMatch(versions)) {
          statusReady = false;
          notify();
        }
        throw error;
      });
    }

    function reconcileVisibleTargetStatuses(targets) {
      var targetIds = Object.create(null);
      targets.forEach(function (item) { targetIds[String(item.id)] = true; });
      var visibleItems = items.filter(function (item) { return !!targetIds[String(item.id)]; });
      if (!visibleItems.length) return Promise.resolve();

      var currentRevision = revision;
      var versions = captureStatusVersions(visibleItems);
      var batches = chunks(visibleItems, BATCH_LIMIT);
      return batches.reduce(function (promise, batch) {
        return promise.then(function () {
          return request("/api/favorites/status", {
            method: "POST",
            body: JSON.stringify({
              items: batch.map(function (item) {
                return {
                  object_type: "media",
                  object_id: String(item.id),
                  source_page: "media",
                };
              }),
            }),
          }).then(function (data) {
            if (!data || !Array.isArray(data.items)) throw new Error("invalid_favorite_status");
            if (currentRevision !== revision) return;
            data.items.forEach(function (item) {
              if (item.object_type !== "media" || typeof item.object_id !== "string") return;
              if (!statusVersionMatches(item.object_id, versions)) return;
              statuses[item.object_id] = {
                result: item.result,
                isFavorited: typeof item.is_favorited === "boolean" ? item.is_favorited : null,
              };
              if (item.result === "not_found") delete selected[item.object_id];
            });
          });
        });
      }, Promise.resolve()).then(function () {
        if (currentRevision !== revision) return;
        statusReady = items.every(function (item) {
          var status = statuses[String(item.id)];
          return status && (status.result === "not_found" || typeof status.isFavorited === "boolean");
        });
        notify();
      }).catch(function () {
        if (currentRevision === revision && allStatusVersionsMatch(versions)) {
          statusReady = false;
          notify();
        }
      });
    }

    function toggleSelected(id, checked) {
      if (busy) return;
      var key = String(id);
      if (!items.some(function (item) { return String(item.id) === key; })) return;
      if (checked && statusReady && statuses[key] && statuses[key].result === "not_found") return;
      if (checked) selected[key] = true;
      else delete selected[key];
      notify();
    }

    function selectAllLoaded() {
      if (busy) return;
      selected = Object.create(null);
      items.forEach(function (item) {
        var key = String(item.id);
        if (!statusReady || (statuses[key] && statuses[key].result === "found")) selected[key] = true;
      });
      notify();
    }

    function clearSelection() {
      selected = Object.create(null);
      notify();
    }

    function getStatus(id) {
      return statuses[String(id)] || null;
    }

    function isSelected(id) {
      return !!selected[String(id)];
    }

    function canApply() {
      return snapshot().canApply;
    }

    function apply(action) {
      if (action !== "favorite" && action !== "unfavorite") {
        return Promise.reject(new Error("invalid_favorite_action"));
      }
      if (!canApply()) return Promise.reject(new Error("favorite_action_unavailable"));

      var currentRevision = revision;
      var targets = items.filter(function (item) { return !!selected[String(item.id)]; });
      var writeVersions = captureStatusVersions(targets);
      var batches = chunks(targets, BATCH_LIMIT);
      var summary = emptySummary();
      busy = true;
      notify();

      function postBatch(batch) {
        return request("/api/favorites/batch", {
          method: "POST",
          body: JSON.stringify({
            action: action,
            items: batch.map(function (item) {
              return {
                object_type: "media",
                object_id: String(item.id),
                source_page: "media",
              };
            }),
          }),
        }).then(function (data) {
          if (!data || !Array.isArray(data.items)) throw new Error("invalid_favorite_result");
          summary.requested += Number(data.requested) || 0;
          summary.unique += Number(data.unique) || 0;
          summary.applied += Number(data.applied) || 0;
          summary.unchanged += Number(data.unchanged) || 0;
          summary.not_found += Number(data.not_found) || 0;
          summary.items = summary.items.concat(data.items);
          data.items.forEach(function (item) {
            if (item.object_type !== "media" || typeof item.object_id !== "string") return;
            if (!Object.prototype.hasOwnProperty.call(writeVersions, item.object_id)) return;
            bumpStatusVersion(item.object_id);
          });
          if (currentRevision === revision) {
            data.items.forEach(function (item) {
              if (item.object_type !== "media" || typeof item.object_id !== "string") return;
              if (item.result === "not_found") {
                statuses[item.object_id] = { result: "not_found", isFavorited: null };
                delete selected[item.object_id];
              } else if (["favorited", "already_favorited", "unfavorited", "already_unfavorited"].indexOf(item.result) !== -1) {
                statuses[item.object_id] = {
                  result: "found",
                  isFavorited: item.result === "favorited" || item.result === "already_favorited",
                };
              }
            });
            notify();
          }
        });
      }

      return batches.reduce(function (promise, batch) {
        return promise.then(function () { return postBatch(batch); });
      }, Promise.resolve()).then(function () {
        var reconciled = currentRevision === revision
          ? Promise.resolve()
          : reconcileVisibleTargetStatuses(targets);
        return reconciled.then(function () {
          if (currentRevision === revision) selected = Object.create(null);
          busy = false;
          notify();
          return summary;
        });
      }).catch(function (error) {
        busy = false;
        notify();
        error.favoriteSummary = summary;
        throw error;
      });
    }

    return {
      setRole: setRole,
      setItems: setItems,
      toggleSelected: toggleSelected,
      selectAllLoaded: selectAllLoaded,
      clearSelection: clearSelection,
      getStatus: getStatus,
      isSelected: isSelected,
      getSnapshot: snapshot,
      canApply: canApply,
      apply: apply,
    };
  }

  return { create: create, batchLimit: BATCH_LIMIT };
});
