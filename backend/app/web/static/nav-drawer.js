/* GH-102: narrow-screen navigation drawer for the admin console.
 *
 * Injected by render_template on every design-system page (platform chrome
 * excluded — it keeps its own responsive nav). ≤900px design-system.css
 * turns the shared .side-nav into a fixed slide-in drawer; this file owns
 * the open/close state: hamburger, backdrop, Esc, and close-on-navigate.
 */
(function () {
  'use strict';

  document.documentElement.classList.add('has-nav-drawer');

  function drawer() { return document.getElementById('side-nav'); }
  function backdrop() { return document.getElementById('nav-backdrop'); }
  function button() { return document.getElementById('nav-toggle'); }

  function isOpen() {
    var d = drawer();
    return !!d && d.classList.contains('open');
  }

  function openNav() {
    var d = drawer(), b = backdrop(), t = button();
    if (!d) return;
    d.classList.add('open');
    if (b) b.hidden = false;
    if (t) t.setAttribute('aria-expanded', 'true');
  }

  function closeNav() {
    var d = drawer(), b = backdrop(), t = button();
    if (!d) return;
    d.classList.remove('open');
    if (b) b.hidden = true;
    if (t) t.setAttribute('aria-expanded', 'false');
  }

  function toggleNav() { isOpen() ? closeNav() : openNav(); }

  window.toggleNav = toggleNav;
  window.closeNav = closeNav;

  document.addEventListener('click', function (event) {
    if (!isOpen()) return;
    if (event.target.closest('.side-nav')) {
      // 点导航项跳页后收起抽屉，避免窄屏停留在遮罩下。
      if (event.target.closest('a')) closeNav();
      return;
    }
    if (event.target.closest('#nav-toggle')) return;
    closeNav();
  });

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && isOpen()) closeNav();
  });
}());
