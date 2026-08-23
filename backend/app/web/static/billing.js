(function () {
  'use strict';

  var plan = null;
  var order = null;
  var capacity = null;
  var subscription = null;
  var refund = null;
  var pollTimer = null;
  var busy = false;
  var cancelIntentBusy = false;
  var billingMode = document.body.dataset.billingMode || 'admin';
  var canOrder = document.body.dataset.billingCanOrder === 'true';
  var isOwner = document.body.dataset.billingIsOwner === 'true';
  var terminal = { succeeded: true, closed: true, failed: true };
  var activeOrder = { creating: true, pending: true, paid_activation_pending: true };
  var cancelableSubscriptionStatus = { trial: true, active: true, grace: true };

  function t(key, values) {
    var value = I18N.t(key);
    Object.keys(values || {}).forEach(function (name) {
      value = value.replace('{' + name + '}', String(values[name]));
    });
    return value;
  }

  function node(id) { return document.getElementById(id); }

  function money(cents, currency) {
    return new Intl.NumberFormat(I18N.getLocale(), {
      style: 'currency',
      currency: currency
    }).format(cents / 100);
  }

  function date(value) {
    if (!value) { return '—'; }
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? '—' : parsed.toLocaleString(I18N.getLocale());
  }

  function bytes(value) {
    if (value === null || value === undefined) { return '—'; }
    return value === 5 * 1024 * 1024 * 1024
      ? '5 GiB'
      : (value / 1024 / 1024 / 1024).toFixed(2) + ' GiB';
  }

  function showError(message) {
    var element = node('billing-error');
    element.textContent = message || '';
    element.hidden = !message;
  }

  function request(url, options) {
    return fetch(url, Object.assign({ credentials: 'include' }, options || {})).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok) {
          var error = new Error(body.detail || 'request_failed');
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }

  function statusKey(status) { return 'billing.status.' + (status || 'none'); }

  function statusClass(status) {
    if (status === 'succeeded') { return 'badge badge-success'; }
    if (status === 'failed') { return 'badge badge-danger'; }
    if (status === 'paid_activation_pending') { return 'badge badge-warning'; }
    if (status === 'closed') { return 'badge badge-neutral'; }
    return 'badge badge-info';
  }

  function subscriptionClass(state) {
    if (state === 'paid_active') { return 'badge badge-success'; }
    if (state === 'trial') { return 'badge badge-info'; }
    if (state === 'expiring_soon' || state === 'grace') { return 'badge badge-warning'; }
    if (state === 'expired' || state === 'canceled') { return 'badge badge-danger'; }
    return 'badge badge-neutral';
  }

  function billingCta(displayState, unavailableReason, mode, orderStatus, paymentEnabled, tenantStatus) {
    // RND-404: a manually suspended tenant is denied every self-serve
    // payment action (ADR-0005 §2.6) — this must win over any pending
    // order or subscription state so the CTA never implies a payment can
    // lift the suspension.
    if (tenantStatus === 'suspended') {
      return {
        kind: 'suspended',
        labelKey: 'billing.cta.suspended',
        nextStepKey: 'billing.next.suspended'
      };
    }
    if (activeOrder[orderStatus]) {
      return {
        kind: 'order',
        labelKey: 'billing.cta.orderPending',
        nextStepKey: orderStatus === 'paid_activation_pending'
          ? 'billing.next.activationPending'
          : 'billing.next.orderPending'
      };
    }
    if (mode === 'provisioning' && unavailableReason === 'no_subscription') {
      return {
        kind: 'setup',
        labelKey: 'billing.cta.completeSetup',
        nextStepKey: 'billing.next.completeSetup'
      };
    }
    var labelKeys = {
      trial: 'billing.cta.renewTrial',
      paid_active: 'billing.cta.renew',
      expiring_soon: 'billing.cta.renewSoon',
      grace: 'billing.cta.restore',
      expired: 'billing.cta.restore',
      canceled: 'billing.cta.purchase'
    };
    var nextKeys = {
      trial: 'billing.next.trial',
      paid_active: 'billing.next.paidActive',
      expiring_soon: 'billing.next.expiringSoon',
      grace: 'billing.next.restore',
      expired: 'billing.next.restore',
      canceled: 'billing.next.purchase'
    };
    return {
      kind: paymentEnabled ? 'payment' : 'unavailable',
      labelKey: labelKeys[displayState] || 'billing.cta.purchase',
      nextStepKey: nextKeys[displayState] || 'billing.next.purchase'
    };
  }

  function entitlement(value) { return t('billing.entitlement.' + value); }

  function trialDaysRemaining() {
    if (!subscription || subscription.display_state !== 'trial' || !subscription.ends_at) { return null; }
    var end = new Date(subscription.ends_at).getTime();
    var measured = new Date(subscription.measured_at || Date.now()).getTime();
    if (!isFinite(end) || !isFinite(measured)) { return null; }
    return Math.max(0, Math.ceil((end - measured) / 86400000));
  }

  function currentPolicy() {
    if (!subscription || !plan) { return null; }
    return billingCta(
      subscription.display_state,
      subscription.unavailable_reason,
      billingMode,
      order && order.status,
      plan.payment_enabled,
      subscription.tenant_lifecycle_status
    );
  }

  function renderActions() {
    var policy = currentPolicy();
    if (!policy) { return; }
    var create = node('create-order');
    var setup = node('complete-setup');
    var unavailable = node('payment-unavailable');
    var suspendedNotice = node('suspended-notice');
    var next = node('billing-next-step');
    setup.hidden = policy.kind !== 'setup';
    create.hidden = !canOrder || policy.kind !== 'payment';
    create.disabled = busy;
    create.textContent = t(policy.labelKey, {
      amount: money(plan.amount_cents, plan.currency)
    });
    // RND-404: suspended has its own dedicated notice; never also show the
    // generic "payment not configured" copy for it.
    unavailable.hidden = policy.kind === 'suspended' || plan.payment_enabled || policy.kind === 'setup';
    suspendedNotice.hidden = policy.kind !== 'suspended';
    next.textContent = canOrder
      ? t(policy.nextStepKey)
      : t('billing.next.readonly');
  }

  function renderPlan() {
    if (!plan) { return; }
    node('plan-title').textContent = plan.display_name;
    node('plan-price').textContent = money(plan.amount_cents, plan.currency);
    node('plan-storage').textContent = bytes(plan.storage_quota_bytes);
    renderActions();
  }

  function renderSubscription() {
    if (!subscription) { return; }
    var state = subscription.display_state;
    var badge = node('subscription-badge');
    var messageKey = subscription.unavailable_reason === 'no_subscription' && billingMode === 'provisioning'
      ? 'billing.subscription.reason.no_subscription_provisioning'
      : subscription.unavailable_reason
        ? 'billing.subscription.reason.' + subscription.unavailable_reason
        : 'billing.subscription.message.' + state;
    badge.textContent = t('billing.subscription.state.' + state);
    badge.className = subscriptionClass(state);
    node('current-plan').textContent = subscription.plan_name || t('billing.none');
    node('subscription-starts').textContent = date(subscription.starts_at);
    node('subscription-current-ends').textContent = date(subscription.ends_at);
    node('subscription-entitlements').textContent = subscription.entitlements.length
      ? subscription.entitlements.map(entitlement).join(' · ')
      : t('billing.none');
    node('subscription-state').textContent = t(messageKey);
    node('subscription-measured').textContent = t('billing.subscriptionMeasured', {
      time: date(subscription.measured_at)
    });
    var remaining = trialDaysRemaining();
    var trial = node('trial-remaining');
    trial.hidden = remaining === null;
    trial.textContent = remaining === null ? '' : t('billing.trialRemaining', { days: remaining });
    renderTenantStatus();
    renderCancelIntent();
    renderActions();
  }

  function renderTenantStatus() {
    var banner = node('tenant-status-banner');
    if (!subscription) { banner.hidden = true; return; }
    var status = subscription.tenant_lifecycle_status;
    if (status !== 'frozen' && status !== 'suspended') {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    banner.textContent = t('billing.tenantStatus.' + status);
    banner.className = 'alert tenant-status-banner ' + (status === 'suspended' ? 'alert-danger' : 'alert-warning');
  }

  function renderCancelIntent() {
    var block = node('cancel-intent');
    var status = node('cancel-intent-status');
    var toggle = node('cancel-intent-toggle');
    var eligible = Boolean(
      subscription
      && isOwner
      && subscription.tenant_lifecycle_status !== 'suspended'
      && cancelableSubscriptionStatus[subscription.stored_status]
    );
    block.hidden = !eligible;
    if (!eligible) { return; }
    var canceled = Boolean(subscription.cancel_at_period_end);
    status.textContent = t(canceled ? 'billing.cancelIntent.active' : 'billing.cancelIntent.inactive');
    toggle.textContent = t(canceled ? 'billing.cancelIntent.restore' : 'billing.cancelIntent.set');
    toggle.disabled = cancelIntentBusy;
  }

  function renderRefund() {
    var card = node('refund-card');
    if (!refund) { card.hidden = true; return; }
    card.hidden = false;
    var status = node('refund-status');
    status.textContent = t('billing.refund.status.' + refund.status);
    status.className = 'badge ' + (
      refund.status === 'succeeded'
        ? 'badge-success'
        : refund.status === 'abnormal' || refund.status === 'manual_recovery_required'
          ? 'badge-danger'
          : refund.status === 'closed'
            ? 'badge-neutral'
            : 'badge-warning'
    );
    node('refund-amount').textContent = money(refund.amount_cents, refund.currency);
    node('refund-requested').textContent = date(refund.requested_at);
    node('refund-succeeded').textContent = date(refund.succeeded_at);
    var note = node('refund-note');
    var noteKey = refund.status === 'abnormal' || refund.status === 'manual_recovery_required'
      ? 'billing.refund.note.needsRecovery'
      : '';
    note.textContent = noteKey ? t(noteKey) : '';
    note.hidden = !noteKey;
  }

  function renderCapacity() {
    if (!capacity) { return; }
    node('capacity-quota').textContent = bytes(capacity.quota_bytes);
    node('capacity-used').textContent = bytes(capacity.used_bytes);
    node('capacity-remaining').textContent = bytes(capacity.remaining_bytes);
    var state = node('capacity-state');
    state.textContent = t('billing.capacity.' + capacity.state);
    state.className = 'alert capacity-state ' + (
      capacity.state === 'normal'
        ? 'alert-success'
        : capacity.state === 'warning_80' || capacity.state === 'warning_90'
          ? 'alert-warning'
          : capacity.state === 'full' || capacity.state === 'over_limit'
            ? 'alert-danger'
            : 'alert-neutral'
    );
    node('capacity-measured').textContent = t('billing.capacityMeasured', {
      time: date(capacity.measured_at)
    });
  }

  function stopPolling() {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  }

  function schedulePoll() {
    stopPolling();
    if (!order || terminal[order.status]) { return; }
    if (new Date(order.expires_at) <= new Date() && order.status === 'pending') { return; }
    pollTimer = setTimeout(function () {
      if (document.visibilityState === 'visible') { loadOrder(order.order_id); }
      else { schedulePoll(); }
    }, 3000);
  }

  function renderOrder() {
    var placeholder = node('checkout-placeholder');
    var qr = node('checkout-qr');
    var redirect = node('checkout-redirect');
    var meta = node('order-meta');
    var status = node('billing-status');
    var refresh = node('refresh-order');
    var close = node('close-order');
    var hasLiveOrder = Boolean(order && activeOrder[order.status]);
    var validQr = Boolean(
      canOrder
      && order
      && order.status === 'pending'
      && order.checkout_kind === 'qr_code'
      && order.qr_available
      && new Date(order.expires_at) > new Date()
    );
    var validRedirect = Boolean(
      canOrder
      && order
      && order.status === 'pending'
      && order.checkout_kind === 'redirect'
      && new Date(order.expires_at) > new Date()
    );

    qr.hidden = !validQr;
    if (validQr) {
      qr.src = '/api/billing/orders/' + encodeURIComponent(order.order_id) + '/qr?v=' + encodeURIComponent(order.order_id);
    } else {
      qr.removeAttribute('src');
    }
    redirect.hidden = !validRedirect;
    redirect.href = validRedirect
      ? '/api/billing/orders/' + encodeURIComponent(order.order_id) + '/checkout'
      : '#';
    placeholder.hidden = !hasLiveOrder || validQr || validRedirect;
    if (!placeholder.hidden) {
      placeholder.textContent = t(
        order.status === 'paid_activation_pending'
          ? 'billing.checkoutActivationPending'
          : new Date(order.expires_at) <= new Date()
            ? 'billing.checkoutExpired'
            : 'billing.checkoutPreparing'
      );
    }
    status.hidden = !order;
    meta.hidden = !order;
    if (order) {
      node('order-id').textContent = order.order_id;
      node('order-expires').textContent = date(order.expires_at);
      node('subscription-ends').textContent = date(order.subscription_ends_at);
      status.textContent = t(statusKey(order.status));
      status.className = statusClass(order.status);
    }
    // RND-404: a suspended tenant's write endpoints 403 server-side; hide
    // the buttons proactively so the Owner never sees a failed request for
    // an action that was never going to be allowed.
    var suspended = Boolean(subscription && subscription.tenant_lifecycle_status === 'suspended');
    refresh.hidden = !hasLiveOrder || !canOrder || suspended;
    close.hidden = !(order && (order.status === 'creating' || order.status === 'pending')) || !canOrder || suspended;
    refresh.disabled = busy || Boolean(plan && !plan.payment_enabled);
    close.disabled = busy || Boolean(plan && !plan.payment_enabled);
    renderActions();
    schedulePoll();
  }

  function loadAccountState() {
    return Promise.all([
      request('/api/billing/subscription'),
      request('/api/billing/capacity'),
      request('/api/billing/refunds/latest')
    ]).then(function (values) {
      subscription = values[0];
      capacity = values[1];
      refund = values[2];
      renderSubscription();
      renderCapacity();
      renderRefund();
    });
  }

  function loadOrder(id) {
    return request('/api/billing/orders/' + encodeURIComponent(id)).then(function (value) {
      order = value;
      renderOrder();
      if (order.status === 'succeeded') { return loadAccountState(); }
      return null;
    }).catch(function (error) {
      stopPolling();
      showError(error.status === 401 ? t('billing.error.auth') : t('billing.error.load'));
    });
  }

  function load() {
    showError('');
    Promise.all([
      request('/api/billing/plan'),
      request('/api/billing/orders/latest'),
      request('/api/billing/subscription'),
      request('/api/billing/capacity'),
      request('/api/billing/refunds/latest')
    ]).then(function (values) {
      plan = values[0];
      order = values[1];
      subscription = values[2];
      capacity = values[3];
      refund = values[4];
      sessionStorage.removeItem('billing_create_key');
      renderPlan();
      renderSubscription();
      renderOrder();
      renderCapacity();
      renderRefund();
    }).catch(function (error) {
      showError(error.status === 401 ? t('billing.error.auth') : t('billing.error.load'));
    });
  }

  function randomKey() {
    if (window.crypto && window.crypto.randomUUID) { return 'billing-' + window.crypto.randomUUID(); }
    var values = new Uint8Array(24);
    window.crypto.getRandomValues(values);
    return 'billing-' + Array.from(values, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function createOrder() {
    if (!plan || busy || !plan.payment_enabled || activeOrder[order && order.status]) { return; }
    busy = true;
    showError('');
    var key = sessionStorage.getItem('billing_create_key') || randomKey();
    sessionStorage.setItem('billing_create_key', key);
    renderActions();
    request('/api/billing/orders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ plan_code: plan.code })
    }).then(function (value) {
      order = value;
      sessionStorage.removeItem('billing_create_key');
      renderOrder();
    }).catch(function (error) {
      showError(error.message === 'payment_unavailable' ? t('billing.unavailable') : t('billing.error.create'));
    }).finally(function () {
      busy = false;
      renderOrder();
    });
  }

  function refreshOrder() {
    if (!order || busy || !plan || !plan.payment_enabled || !activeOrder[order.status]) { return; }
    busy = true;
    showError('');
    renderOrder();
    request('/api/billing/orders/' + encodeURIComponent(order.order_id) + '/refresh', { method: 'POST' }).then(function (value) {
      order = value;
      renderOrder();
      if (order.status === 'succeeded') { return loadAccountState(); }
      return null;
    }).catch(function (error) {
      if (error.message === 'payment_activation_pending') { loadOrder(order.order_id); }
      showError(error.message === 'payment_activation_pending' ? t('billing.error.activationPending') : t('billing.error.refresh'));
    }).finally(function () {
      busy = false;
      renderOrder();
    });
  }

  function closeOrder() {
    if (!order || busy || !plan || !plan.payment_enabled || !['creating', 'pending'].includes(order.status)) { return; }
    busy = true;
    showError('');
    renderOrder();
    request('/api/billing/orders/' + encodeURIComponent(order.order_id) + '/close', { method: 'POST' }).then(function (value) {
      order = value;
      renderOrder();
    }).catch(function () {
      showError(t('billing.error.close'));
    }).finally(function () {
      busy = false;
      renderOrder();
    });
  }

  function applyI18n() {
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = t(element.getAttribute('data-i18n'));
    });
    document.querySelectorAll('[data-i18n-alt]').forEach(function (element) {
      element.alt = t(element.getAttribute('data-i18n-alt'));
    });
    document.title = t('billing.title');
    renderPlan();
    renderSubscription();
    renderOrder();
    renderCapacity();
    renderRefund();
  }

  function toggleCancelIntent() {
    if (!subscription || !isOwner || cancelIntentBusy) { return; }
    cancelIntentBusy = true;
    showError('');
    renderCancelIntent();
    var nextEnabled = !subscription.cancel_at_period_end;
    request('/api/billing/subscription/cancel-intent', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: nextEnabled })
    }).then(function (value) {
      subscription = value;
      renderSubscription();
    }).catch(function () {
      showError(t('billing.error.cancelIntent'));
    }).finally(function () {
      cancelIntentBusy = false;
      renderCancelIntent();
    });
  }

  window.doLogout = function () {
    fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).finally(function () {
      window.location = '/admin/login';
    });
  };
  node('create-order').addEventListener('click', createOrder);
  node('refresh-order').addEventListener('click', refreshOrder);
  node('close-order').addEventListener('click', closeOrder);
  node('cancel-intent-toggle').addEventListener('click', toggleCancelIntent);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && order && activeOrder[order.status]) {
      loadOrder(order.order_id);
    }
  });
  I18N.onChange(applyI18n);
  applyI18n();
  load();
}());
