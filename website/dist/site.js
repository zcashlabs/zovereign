/* Zovereign site enhancements. The page is complete without this file. */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var toast = document.querySelector('.toast');
  var toastTimer;

  function announce(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toast.classList.remove('show'); }, 2200);
  }

  /* ---- masthead border on scroll ---- */
  var masthead = document.querySelector('.masthead');
  function onScroll() { masthead.classList.toggle('scrolled', window.scrollY > 8); }
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  /* ---- copy to clipboard ---- */
  function fallbackCopy(text) {
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    var ok = false;
    try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
    document.body.removeChild(ta);
    return ok ? Promise.resolve() : Promise.reject(new Error('copy failed'));
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return fallbackCopy(text); });
    }
    return fallbackCopy(text);
  }

  function selectBlock(el) {
    var range = document.createRange();
    range.selectNodeContents(el);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  document.querySelectorAll('.copy[data-copy]').forEach(function (btn) {
    var target = document.getElementById(btn.getAttribute('data-copy'));
    if (!target) return;
    var label = btn.querySelector('.copy-label');
    var name = btn.closest('.code').querySelector('.code-name');
    btn.setAttribute('aria-label', 'Copy ' + (name ? name.textContent : '') + ' command');
    btn.hidden = false;
    var resetTimer;
    btn.addEventListener('click', function () {
      copyText(target.textContent).then(function () {
        btn.classList.add('done');
        label.textContent = 'Copied ✓';
        announce('Command copied to clipboard');
      }, function () {
        selectBlock(target);
        label.textContent = 'Press ⌘/Ctrl+C';
        announce('Copy blocked — the command is selected, press Ctrl+C or ⌘C');
      });
      clearTimeout(resetTimer);
      resetTimer = setTimeout(function () {
        btn.classList.remove('done');
        label.textContent = 'Copy';
      }, 2400);
    });
  });

  /* ---- install tabs (WAI-ARIA tabs pattern, manual activation by arrow keys) ---- */
  document.querySelectorAll('[data-tabs]').forEach(function (root) {
    var list = root.querySelector('[role="tablist"]');
    var tabs = Array.prototype.slice.call(root.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (t) { return document.getElementById(t.getAttribute('aria-controls')); });
    list.hidden = false;
    root.classList.add('ready');

    function select(i, focus) {
      tabs.forEach(function (t, j) {
        var on = i === j;
        t.setAttribute('aria-selected', on ? 'true' : 'false');
        t.tabIndex = on ? 0 : -1;
        panels[j].hidden = !on;
      });
      if (focus) tabs[i].focus();
    }

    tabs.forEach(function (t, i) {
      t.addEventListener('click', function () { select(i, false); });
      t.addEventListener('keydown', function (e) {
        var n = tabs.length, next = null;
        if (e.key === 'ArrowRight') next = (i + 1) % n;
        else if (e.key === 'ArrowLeft') next = (i - 1 + n) % n;
        else if (e.key === 'Home') next = 0;
        else if (e.key === 'End') next = n - 1;
        if (next !== null) { e.preventDefault(); select(next, true); }
      });
    });
    panels.forEach(function (p) { p.tabIndex = 0; });
    select(0, false);
  });

  /* ---- blueprint tracing: legend <-> diagram ---- */
  var blueprint = document.querySelector('.blueprint');
  var legendItems = document.querySelectorAll('.legend li[data-part]');
  if (blueprint && legendItems.length) {
    var parts = blueprint.querySelectorAll('.bp-part');
    function trace(name) {
      blueprint.classList.toggle('tracing', !!name);
      parts.forEach(function (p) { p.classList.toggle('active', p.getAttribute('data-part') === name); });
      legendItems.forEach(function (li) { li.classList.toggle('active', li.getAttribute('data-part') === name); });
    }
    legendItems.forEach(function (li) {
      var name = li.getAttribute('data-part');
      li.tabIndex = 0;
      li.addEventListener('mouseenter', function () { trace(name); });
      li.addEventListener('focus', function () { trace(name); });
      li.addEventListener('mouseleave', function () { trace(null); });
      li.addEventListener('blur', function () { trace(null); });
    });
    parts.forEach(function (p) {
      p.addEventListener('mouseenter', function () { trace(p.getAttribute('data-part')); });
      p.addEventListener('mouseleave', function () { trace(null); });
    });
  }

  /* ---- reveal on scroll (purely decorative; content is visible without JS) ---- */
  var revealTargets = document.querySelectorAll('.timeline, .step, .report, .trust-col, .plate');
  if (!('IntersectionObserver' in window) || reduceMotion) {
    revealTargets.forEach(function (el) { el.classList.add('in'); });
  } else {
    document.querySelectorAll('.step, .report, .trust-col, .plate').forEach(function (el) { el.classList.add('reveal'); });
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add('in'); io.unobserve(entry.target); }
      });
    }, { rootMargin: '0px 0px -10% 0px', threshold: 0.15 });
    revealTargets.forEach(function (el) { io.observe(el); });
  }
})();
