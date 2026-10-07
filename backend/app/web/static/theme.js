/* GH-101: dark-theme bootstrap for the admin console.
 *
 * Loaded from <head> on every design-system page (injected by
 * app/web::__init__::render_template) so the data-theme attribute is set
 * before first paint — no light flash on dark sessions. Contract:
 *
 *   localStorage 'wecom_admin_theme'  'light' | 'dark' | absent
 *   absent                            → follow prefers-color-scheme live
 *
 * Pages opt in by declaring data-theme on <html>; design-system.css flips
 * its tokens on [data-theme="dark"]. Platform chrome intentionally stays
 * light and does not load this file.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'wecom_admin_theme';
  var mql = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function stored() {
    try {
      var value = localStorage.getItem(STORAGE_KEY);
      return value === 'light' || value === 'dark' ? value : null;
    } catch (error) {
      return null;
    }
  }

  function effective() {
    var value = stored();
    if (value) return value;
    return mql && mql.matches ? 'dark' : 'light';
  }

  function apply() {
    document.documentElement.setAttribute('data-theme', effective());
  }

  function setTheme(choice) {
    try {
      if (choice === 'light' || choice === 'dark') {
        localStorage.setItem(STORAGE_KEY, choice);
      } else {
        localStorage.removeItem(STORAGE_KEY);
      }
    } catch (error) {
      /* Private browsing / disabled storage — the in-page choice still
         applies for this load; persistence resumes when storage returns. */
    }
    apply();
  }

  function toggleTheme() {
    setTheme(effective() === 'dark' ? 'light' : 'dark');
  }

  function getTheme() {
    return effective();
  }

  if (mql) {
    var onChange = function () { if (!stored()) apply(); };
    if (mql.addEventListener) mql.addEventListener('change', onChange);
    else if (mql.addListener) mql.addListener(onChange);
  }

  apply();

  window.ThemeControl = { apply: apply, setTheme: setTheme, getTheme: getTheme, toggleTheme: toggleTheme };
  window.toggleTheme = toggleTheme;
}());
