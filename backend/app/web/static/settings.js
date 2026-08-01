(function () {
  'use strict';

  function applyI18n() {
    if (!window.I18N) return;
    document.documentElement.lang = I18N.getLocale();
    document.querySelectorAll('[data-i18n]').forEach(function (element) {
      element.textContent = I18N.t(element.getAttribute('data-i18n'));
    });
  }

  function initSettingsNavigation() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll('[data-settings-section]'));
    var panels = Array.prototype.slice.call(document.querySelectorAll('.settings-section'));

    function selectSection(name) {
      var panelId = 'settings-section-' + name;
      tabs.forEach(function (tab) {
        var selected = tab.getAttribute('data-settings-section') === name;
        tab.classList.toggle('active', selected);
        tab.setAttribute('aria-selected', String(selected));
        tab.tabIndex = selected ? 0 : -1;
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.id !== panelId;
      });
    }

    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        selectSection(tab.getAttribute('data-settings-section'));
      });
    });

    var activeTab = tabs.filter(function (tab) {
      return tab.getAttribute('aria-selected') === 'true';
    })[0];
    if (activeTab) selectSection(activeTab.getAttribute('data-settings-section'));
    applyI18n();
    if (window.I18N) I18N.onChange(applyI18n);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initSettingsNavigation);
  } else {
    initSettingsNavigation();
  }
}());
