(function () {
  'use strict';

  // Closed event catalogue mirrored by app.schemas.product_analytics.  This
  // browser helper deliberately has no generic `track(name, payload)` escape
  // hatch: product code can send only reviewed v1 fields.
  var attributesByEvent = {
    'product.conversation.review_opened.v1': [],
    'product.directory.view_selected.v1': ['view_kind'],
    'product.directory.subject_selected.v1': ['subject_kind'],
    'product.conversation.detail_opened.v1': [],
    'product.conversation.older_messages_loaded.v1': [],
    'product.search.executed.v1': ['search_scope'],
    'product.search.filter_applied.v1': ['filter_dimension', 'filter_action'],
    'product.media.preview_opened.v1': [],
    'product.settings.opened.v1': []
  };

  function eventId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID();
    }
    // A UUID-shaped fallback is sufficient for local idempotency if an older
    // browser lacks randomUUID. It is never a browser fingerprint or session
    // identifier and is generated anew for one logical action only.
    var value = String(Date.now()) + '-' + String(Math.random()).slice(2);
    return '00000000-0000-4000-8000-' + value.replace(/[^0-9]/g, '').slice(-12).padStart(12, '0');
  }

  function track(eventName, attributes) {
    if (!Object.prototype.hasOwnProperty.call(attributesByEvent, eventName) || !window.fetch) {
      return;
    }
    var allowed = attributesByEvent[eventName];
    var input = attributes || {};
    var keys = Object.keys(input);
    if (keys.length !== allowed.length || keys.some(function (key) { return allowed.indexOf(key) === -1; })) {
      return;
    }
    var body = {
      event_id: eventId(),
      event_name: eventName,
      occurred_at: new Date().toISOString()
    };
    allowed.forEach(function (key) { body[key] = input[key]; });

    // Intentionally fire-and-forget. Failure to collect a statistic can never
    // delay, reject, or otherwise change the user action that caused it.
    window.fetch('/api/product-analytics/events', {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    }).catch(function () {});
  }

  window.ProductAnalytics = {track: track};
}());
