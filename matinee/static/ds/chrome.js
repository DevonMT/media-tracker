/* Generated from devondoes/ds/chrome.js — do not edit here.
   Edit the source and run `node ds/sync.mjs`. `--check` fails on drift. */
/*
 * The estate bar: who you are, where you are, where else you can go.
 *
 * Deliberately not a framework component: it runs in a static Astro page, a
 * server-rendered template, Jinja and React equally, and asks nothing of any
 * of them beyond the markup contract in chrome.css (v2 since #559; the old
 * six-element bar is upgraded in place, below, so apps can move one at a time).
 *
 * Load it with a plain <script> in <head>, no defer: the theme and motion
 * stamps below must run before the first paint. It waits for the document
 * before it touches the bar.
 *
 * BOTH REQUESTS ARE SAME-ORIGIN. The gateway proxies /api/me, /api/sign-out
 * and /api/estate/prefs through to the platform on every app hostname, which
 * keeps this free of CORS, of a second allowlist to maintain, and of the
 * "* is invalid with credentials" trap that the drill sync had to solve.
 *
 * An app served from OUTSIDE the gateway (Rumpus, Delve) has no such proxy. It
 * sets data-platform on the bar and the calls go to the platform directly,
 * answered by the BAR_ORIGINS allow-list there. Same paths either way, so the
 * bar never has to know which side of the gateway it is on.
 *
 * Images (emblems, avatars) and the links in the menus always come from the
 * hub, whose address is the mark's own href: a relative "/" on the hub, the
 * full https://devondoes.dev/ in an app.
 */
(function () {
  /*
   * Background motion, estate-wide.
   *
   * The platform mirrors the profile setting into a cookie on .devondoes.dev,
   * so every app can read it here, before its background starts, with no
   * request. Stamped on <html> as data-motion="off". Anything animated asks
   * window.dsMotion() before it moves: false when the setting is off OR the
   * device asks for reduced motion.
   */
  var root = document.documentElement;
  if (document.cookie.split('; ').indexOf('ds_motion=off') > -1) root.setAttribute('data-motion', 'off');
  window.dsMotion = function () {
    return root.getAttribute('data-motion') !== 'off' &&
      !(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  };

  /*
   * Keep the address bar in step with the page.
   *
   * theme-color paints the browser chrome and, on Android, the status bar. It
   * cannot follow a media query, so a page that follows the device ends up with
   * a light bar over a dark page at sunset. Reading --bg means it can never
   * disagree. Looked up each time: in <head> this runs before the meta exists.
   */
  var syncColour = function () {
    var meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    var g = getComputedStyle(root).getPropertyValue('--bg').trim();
    if (g) meta.setAttribute('content', g);
  };
  if (window.matchMedia) {
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', syncColour);
  }

  /*
   * Light / dark / system, estate-wide (SPEC-estate-shell.md).
   *
   * The cookie ds_theme holds the PREFERENCE; the platform keeps it in step with
   * the profile (/api/me). The built rule is the same on both sides: localhost
   * gets no Domain, everywhere else is .devondoes.dev and Secure. Stamps
   * data-theme for light and dark, and NOTHING for system, so the device answers.
   */
  var THEMES = ['light', 'dark', 'system'];
  var picks = 0;
  var lastPick = null;
  var bar = null;
  // 'in' | 'out' | 'unknown', or null while /api/me is still out.
  var state = null;
  var stampTheme = function (t) {
    if (t === 'light' || t === 'dark') root.setAttribute('data-theme', t);
    else root.removeAttribute('data-theme');
    syncColour();
  };
  var themeCookie = function () {
    var m = document.cookie.match(/(?:^|; )ds_theme=(light|dark|system)(?:;|$)/);
    return m ? m[1] : null;
  };
  var writeThemeCookie = function (t) {
    document.cookie = 'ds_theme=' + t + '; Max-Age=31536000; Path=/; SameSite=Lax' +
      (location.hostname === 'localhost' ? '' : '; Domain=.devondoes.dev; Secure');
  };
  stampTheme(themeCookie());
  window.dsTheme = {
    /** The stored preference: 'light' | 'dark' | 'system' (never chosen reads as system).
        A pick on this page wins over a cookie the browser would not take. */
    get: function () { return lastPick || themeCookie() || 'system'; },
    /** A pick: cookie and stamp at once, then (unless known signed out) save it. */
    set: function (t) {
      if (THEMES.indexOf(t) < 0) return;
      picks++;
      lastPick = t;
      writeThemeCookie(t);
      stampTheme(t);
      showPref();
      if (state === 'out') return;
      fetch((bar && bar.dataset.platform || '') + '/api/estate/prefs', {
        method: 'POST',
        credentials: 'include',
        redirect: 'manual',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: t }),
      }).catch(function () { /* the cookie still carries the choice on this device */ });
    },
  };

  /* ---- Icons for the menus (the bar's own are CSS masks, chrome.css) ---- */

  var SVG = function (vb, sw, body) {
    return '<svg viewBox="' + vb + '" fill="none" stroke="currentColor" stroke-width="' + sw +
      '" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + body + '</svg>';
  };
  var ICON = {
    home: SVG('0 0 24 24', 1.8, '<path d="M3.5 10.5 12 3.8l8.5 6.7V20a.9.9 0 0 1-.9.9h-4.8v-6h-5.6v6H4.4a.9.9 0 0 1-.9-.9z"/>'),
    people: SVG('0 0 24 24', 1.8, '<circle cx="9" cy="8" r="3.4"/><path d="M2.8 19.5c.6-3.4 3.2-5.3 6.2-5.3s5.6 1.9 6.2 5.3"/><path d="M15.5 4.9a3.3 3.3 0 0 1 0 6.3M17.6 14.6c1.9.7 3.2 2.4 3.6 4.9"/>'),
    light: SVG('0 0 24 24', 1.9, '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>'),
    dark: SVG('0 0 24 24', 1.9, '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>'),
    system: SVG('0 0 24 24', 1.9, '<rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8.5 20h7M12 16.5V20"/>'),
    check: SVG('0 0 16 16', 2.2, '<path d="M3.5 8.5l3 3 6-7"/>'),
  };
  var THEME_LABEL = { light: 'Light', dark: 'Dark', system: 'System' };

  /*
   * Names for an upgraded six-element bar, which only says "chief of staff"
   * in lower case. /api/me renames it once it answers; this is so the first
   * paint is already right.
   */
  var NAMES = {
    backlog: 'Backlog', cos: 'Morning Brief', delve: 'Delve', jobs: 'Jobs', keep: 'Keep',
    ledger: 'Ledger', matinee: 'Matinee', mise: 'Mise', playfinder: 'Playfinder',
    rumpus: 'Rumpus', solitaire: 'Solitaire',
  };

  var el = function (tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  var img = function (src, size) {
    var i = el('img');
    i.src = src;
    i.alt = '';
    if (size) { i.width = size; i.height = size; }
    return i;
  };

  var hub = '';          // the hub's origin, from the mark
  var me = null;         // the /api/me answer, once signed in
  var unread = 0;
  var hubUrl = function (p) { return hub + p; };
  var q = function (sel) { return bar.querySelector(sel); };

  /** Only http(s) or a path on the hub; anything else (javascript:) is not a link. */
  var safeUrl = function (u) {
    if (!u || typeof u !== 'string') return null;
    try {
      var r = new URL(u, hub + '/');
      return r.protocol === 'https:' || r.protocol === 'http:' ? r.href : null;
    } catch (e) { return null; }
  };

  /* ---- The old six-element contract, upgraded in place ------------------ */

  function upgrade() {
    var mark = q('.ds-mark');
    var here = q('.ds-here');
    var hadAccounts = !!q('.ds-who, .ds-out');
    var host = /^([a-z0-9-]+)\.devondoes\.dev$/.exec(location.hostname);
    var said = here ? here.textContent.trim() : '';
    var slug = bar.dataset.app || (host && host[1]) || said.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    bar.setAttribute('data-upgraded', '');
    if (slug) bar.dataset.app = slug;

    if (mark) {
      mark.textContent = '';
      mark.appendChild(img(hubUrl('/emblems/hub.svg'), 26));
      mark.appendChild(el('span', null, 'devondoes'));
    }
    if (here) {
      var sw = el('button', 'ds-switch');
      sw.type = 'button';
      sw.appendChild(img(hubUrl('/emblems/' + slug + '.svg'), 22));
      sw.appendChild(el('span', null, NAMES[slug] || said));
      here.replaceWith(sw);
    }
    var who = q('.ds-who');
    var out = q('.ds-out');
    if (who) who.remove();
    if (out) out.remove();
    // Playfinder's standalone bar stops at the place; the rest sits at the right.
    if (!q('.ds-fill')) bar.appendChild(el('span', 'ds-fill'));
    if (hadAccounts) {
      var nav = el('nav', 'ds-nav');
      nav.setAttribute('aria-label', 'Site');
      var ppl = el('a', 'ds-people');
      ppl.href = hubUrl('/people');
      ppl.setAttribute('aria-label', 'People');
      var av = el('button', 'ds-me');
      av.type = 'button';
      av.setAttribute('aria-label', 'Account');
      nav.appendChild(ppl);
      nav.appendChild(av);
      bar.appendChild(nav);
    }
    var tb = el('button', 'ds-theme');
    tb.type = 'button';
    tb.setAttribute('aria-label', 'Theme');
    tb.hidden = true;
    bar.appendChild(tb);
  }

  /* ---- Menus ------------------------------------------------------------ */

  var open = null; // { menu, trigger }

  /** Up/Down stops: every item, with the theme group as ONE stop (its checked radio). */
  function stops(menu) {
    var out = [];
    var seen = false;
    var all = menu.querySelectorAll('.ds-mi, [role="menuitemradio"]');
    for (var i = 0; i < all.length; i++) {
      var n = all[i];
      if (n.getAttribute('role') !== 'menuitemradio') { out.push(n); continue; }
      if (seen) continue;
      seen = true;
      out.push(n.parentNode.querySelector('[aria-checked="true"]') || n);
    }
    return out;
  }

  function closeMenu(refocus) {
    if (!open) return;
    var o = open;
    open = null;
    o.menu.remove();
    o.trigger.setAttribute('aria-expanded', 'false');
    document.removeEventListener('pointerdown', outside, true);
    document.removeEventListener('keydown', onKey);
    if (refocus) o.trigger.focus();
  }

  function outside(e) {
    if (!open) return;
    if (open.menu.contains(e.target) || open.trigger.contains(e.target)) return;
    closeMenu(false);
  }

  function onKey(e) {
    var menu = open && open.menu;
    if (!menu) return;
    var k = e.key;
    if (k === 'Escape') { e.preventDefault(); closeMenu(true); return; }
    // Tab leaves the menu the way it came: back to the trigger, then onward.
    if (k === 'Tab') { closeMenu(true); return; }
    var cur = document.activeElement;
    if (cur && cur.getAttribute('role') === 'menuitemradio' && (k === 'ArrowLeft' || k === 'ArrowRight')) {
      e.preventDefault();
      var rs = cur.parentNode.querySelectorAll('[role="menuitemradio"]');
      var ri = Array.prototype.indexOf.call(rs, cur);
      rs[(ri + (k === 'ArrowRight' ? 1 : rs.length - 1)) % rs.length].focus();
      return;
    }
    if (k !== 'ArrowDown' && k !== 'ArrowUp' && k !== 'Home' && k !== 'End') return;
    e.preventDefault();
    var s = stops(menu);
    if (!s.length) return;
    var i = s.indexOf(cur);
    if (i < 0 && cur && cur.getAttribute('role') === 'menuitemradio') {
      for (var j = 0; j < s.length; j++) if (s[j].parentNode === cur.parentNode) i = j;
    }
    var next = k === 'Home' ? 0 : k === 'End' ? s.length - 1
      : k === 'ArrowDown' ? (i + 1) % s.length : (i <= 0 ? s.length - 1 : i - 1);
    s[next].focus();
  }

  function openMenu(trigger, menu, end, last) {
    closeMenu(false);
    menu.className = 'ds-menu' + (end ? ' ds-menu-end' : '');
    menu.setAttribute('role', 'menu');
    // A click on a label or the avatar header must not drop focus to <body>.
    menu.tabIndex = -1;
    menu.setAttribute('aria-label', trigger.getAttribute('aria-label') || trigger.textContent.trim());
    bar.appendChild(menu);
    if (!end) {
      // Under the switcher, but never off the right edge of a phone.
      var left = Math.min(trigger.offsetLeft, bar.clientWidth - menu.offsetWidth - 8);
      menu.style.left = Math.max(8, left) + 'px';
    }
    open = { menu: menu, trigger: trigger };
    trigger.setAttribute('aria-expanded', 'true');
    // On the document, not the menu: keys still work wherever focus went.
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', outside, true);
    var s = stops(menu);
    if (s.length) s[last ? s.length - 1 : 0].focus();
  }

  function wire(trigger, build, end) {
    trigger.setAttribute('aria-haspopup', 'menu');
    trigger.setAttribute('aria-expanded', 'false');
    var show = function (last) {
      var m = build();
      if (m) openMenu(trigger, m, end, last);
    };
    trigger.addEventListener('click', function () {
      if (open && open.trigger === trigger) { closeMenu(false); return; }
      show(false);
    });
    // The menu pattern: Down opens on the first item, Up on the last.
    trigger.addEventListener('keydown', function (e) {
      if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && !(open && open.trigger === trigger)) {
        e.preventDefault();
        // The menu's own key handler is on the document: this key is done.
        e.stopPropagation();
        show(e.key === 'ArrowUp');
      }
    });
  }

  function item(tag, label, href) {
    var a = el(tag, 'ds-mi');
    a.setAttribute('role', 'menuitem');
    a.tabIndex = -1;
    if (tag === 'a') a.href = href;
    else a.type = 'button';
    if (label != null) a.appendChild(el('span', null, label));
    // A link closes the menu by leaving the page; this covers a new tab too.
    a.addEventListener('click', function () { if (tag === 'a') closeMenu(false); });
    return a;
  }

  function sep() {
    var s = el('div', 'ds-msep');
    s.setAttribute('role', 'separator');
    return s;
  }

  function group(label) {
    var g = el('div');
    g.setAttribute('role', 'group');
    if (label) {
      var id = 'ds-ml-' + label.toLowerCase();
      var l = el('div', 'ds-mlabel', label);
      l.id = id;
      g.setAttribute('aria-labelledby', id);
      g.appendChild(l);
    }
    return g;
  }

  function badge(n) {
    var b = el('span', 'ds-badge', String(n > 99 ? '99+' : n));
    b.setAttribute('aria-hidden', 'true');
    return b;
  }

  function themeControl() {
    var row = el('div', 'ds-mrow');
    var cap = el('span', null, 'Theme');
    cap.setAttribute('aria-hidden', 'true');
    row.appendChild(cap);
    var seg = el('div', 'ds-seg');
    seg.setAttribute('role', 'group');
    seg.setAttribute('aria-label', 'Theme');
    var pref = window.dsTheme.get();
    THEMES.forEach(function (t) {
      var b = el('button');
      b.type = 'button';
      b.tabIndex = -1;
      b.setAttribute('role', 'menuitemradio');
      b.setAttribute('aria-checked', String(t === pref));
      b.dataset.theme = t;
      b.innerHTML = ICON[t];
      b.appendChild(el('span', null, THEME_LABEL[t]));
      // Picking does not close the menu: you want to see what you picked.
      b.addEventListener('click', function () { window.dsTheme.set(t); b.focus(); });
      seg.appendChild(b);
    });
    row.appendChild(seg);
    return row;
  }

  /** The stored preference, wherever it is shown: the theme button and any open radios. */
  function showPref() {
    if (!bar) return;
    var pref = window.dsTheme.get();
    var tb = q('.ds-theme');
    if (tb) tb.setAttribute('data-pref', pref);
    var rs = bar.querySelectorAll('[role="menuitemradio"]');
    for (var i = 0; i < rs.length; i++) rs[i].setAttribute('aria-checked', String(rs[i].dataset.theme === pref));
  }

  /** The hub rendered this bar for a signed-in viewer; /api/me only fills in detail. */
  var hubSignedIn = function () { return bar.hasAttribute('data-signed-in'); };

  function switcherMenu() {
    if (!me && !hubSignedIn()) return null;
    var m = el('div');
    var home = item('a', null, hubUrl('/'));
    home.innerHTML = ICON.home;
    home.appendChild(el('span', null, 'Home'));
    if (!bar.dataset.app && location.pathname === '/') home.setAttribute('aria-current', 'page');
    var ppl = item('a', null, hubUrl('/people'));
    ppl.innerHTML = ICON.people;
    ppl.appendChild(el('span', null, 'People'));
    ppl.classList.add('ds-mi-people');
    if (unread > 0) {
      ppl.appendChild(badge(unread));
      ppl.setAttribute('aria-label', 'People, ' + unread + ' unread');
    }
    m.appendChild(home);
    m.appendChild(ppl);
    // Until /people holds the inbox as a tab (#560), the inbox needs its own way in.
    var msgs = item('a', 'Messages', hubUrl('/messages'));
    msgs.insertBefore(el('span', 'ds-mi-gap'), msgs.firstChild);
    m.appendChild(msgs);
    if (!me) return m;

    // The server decides who sees what (apps[].group, from appsFor). This only
    // sorts: an app the viewer cannot open is never in the answer.
    var here = bar.dataset.app || '';
    var byName = function (a, b) { return String(a.name).localeCompare(String(b.name)); };
    [['shared', 'Apps'], ['mine', 'Mine']].forEach(function (gl) {
      var apps = (me.apps || []).filter(function (a) { return a.group === gl[0] && safeUrl(a.url); }).sort(byName);
      if (!apps.length) return;
      m.appendChild(sep());
      var g = group(gl[1]);
      apps.forEach(function (a) {
        var it = item('a', null, safeUrl(a.url));
        it.appendChild(img(hubUrl('/emblems/' + encodeURIComponent(a.slug) + '.svg'), 22));
        it.appendChild(el('span', null, a.name));
        if (a.slug === here) {
          it.setAttribute('aria-current', 'page');
          var c = el('span', 'ds-check');
          c.innerHTML = ICON.check;
          it.appendChild(c);
        }
        g.appendChild(it);
      });
      m.appendChild(g);
    });
    return m;
  }

  function accountMenu() {
    if (!me && !hubSignedIn()) return null;
    var m = el('div');
    if (!me) {
      // /api/me failed, but the hub knows you are signed in: the parts that
      // need no answer from it, so there is always a way to sign out.
      m.appendChild(item('a', 'Your Profile', hubUrl('/me')));
      m.appendChild(themeControl());
      m.appendChild(sep());
      m.appendChild(signOutItem());
      return m;
    }
    var u = me.user;
    var head = el('div', 'ds-mhead');
    head.setAttribute('role', 'presentation');
    head.appendChild(img(hubUrl('/avatar/u/' + encodeURIComponent(u.id)), 40));
    var who = el('span');
    who.appendChild(el('b', null, u.name || u.email || ''));
    who.appendChild(el('small', null, 'Signed In'));
    head.appendChild(who);
    m.appendChild(head);
    m.appendChild(item('a', 'Your Profile', hubUrl('/me')));
    m.appendChild(themeControl());
    if (me.isOwner || me.isAdmin) {
      m.appendChild(sep());
      if (me.isOwner) m.appendChild(item('a', 'Week', hubUrl('/week')));
      if (me.isAdmin) m.appendChild(item('a', 'Admin', hubUrl('/admin')));
    }
    m.appendChild(sep());
    m.appendChild(signOutItem());
    return m;
  }

  function signOutItem() {
    var out = item('button', 'Sign Out');
    out.addEventListener('click', function () {
      out.disabled = true;
      fetch((bar.dataset.platform || '') + '/api/sign-out', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
        .catch(function () { /* fall through either way */ })
        .then(function () {
          // The hub sends you to its front door; an app reloads into its own.
          if (bar.dataset.app) location.reload();
          else location.href = '/';
        });
    });
    return out;
  }

  function themeMenu() {
    var m = el('div');
    m.appendChild(themeControl());
    return m;
  }

  /* ---- The People badge -------------------------------------------------- */

  function setUnread(n) {
    unread = Math.max(0, n | 0);
    if (!bar) return;
    var ppl = q('.ds-people');
    if (ppl) {
      var b = ppl.querySelector('.ds-badge');
      if (b) b.remove();
      if (unread > 0) ppl.appendChild(badge(unread));
      ppl.setAttribute('aria-label', unread > 0 ? 'People, ' + unread + ' unread' : 'People');
    }
    if (open) {
      var mp = open.menu.querySelector('.ds-mi-people');
      if (mp) {
        var ob = mp.querySelector('.ds-badge');
        if (ob) ob.remove();
        if (unread > 0) mp.appendChild(badge(unread));
        if (unread > 0) mp.setAttribute('aria-label', 'People, ' + unread + ' unread');
        else mp.removeAttribute('aria-label');
      }
    }
  }
  /** For the hub's live stream (platform/src/shell.ts LIVE_JS). */
  window.dsBar = { setUnread: setUnread };

  /* ---- Signed in, signed out, or unknown -------------------------------- */

  /** No menu behind it: the place becomes a label, not a button that opens nothing. */
  function placeOnly() {
    var sw = q('button.ds-switch');
    if (!sw) return;
    var s = el('span', sw.className);
    while (sw.firstChild) s.appendChild(sw.firstChild);
    sw.replaceWith(s);
  }

  function apply(next) {
    state = next;
    var nav = q('.ds-nav');
    var tb = q('.ds-theme');
    if (nav) nav.style.visibility = '';
    if (state === 'in') {
      var u = me.user;
      if (nav) {
        nav.hidden = false;
        var ppl = q('.ds-people');
        if (ppl && !ppl.getAttribute('href')) ppl.href = hubUrl('/people');
        var av = q('.ds-me');
        if (av) {
          av.textContent = '';
          av.appendChild(img(hubUrl('/avatar/u/' + encodeURIComponent(u.id)), 32));
          av.setAttribute('aria-label', 'Account: ' + (u.name || u.email || ''));
        }
      }
      // With an avatar menu, the theme control lives in it.
      if (tb) tb.hidden = !!nav;
      var si = q('.ds-signin');
      if (si) si.remove();
      var sw = q('.ds-switch');
      var mine = (me.apps || []).filter(function (a) { return a.slug === bar.dataset.app; })[0];
      if (sw && mine) {
        var name = sw.querySelector('span');
        if (name) name.textContent = mine.name;
      }
      setUnread(me.unread || 0);
      return;
    }
    // Unknown on the hub, which rendered you signed in: keep what it drew.
    if (state === 'unknown' && hubSignedIn()) return;
    // Signed out or unknown: mark, place and the theme button.
    if (nav) nav.hidden = true;
    if (tb) tb.hidden = false;
    placeOnly();
    if (state === 'out' && nav && !q('.ds-signin')) {
      var a = el('a', 'ds-signin', 'Sign In');
      a.href = 'https://id.devondoes.dev/?next=' + encodeURIComponent(location.href);
      bar.appendChild(a);
    }
  }

  function init() {
    bar = document.querySelector('.ds-bar');
    if (!bar) return;
    var mark = bar.querySelector('.ds-mark');
    try { hub = new URL(mark ? mark.getAttribute('href') || '/' : '/', location.href).origin; } catch (e) { hub = ''; }
    if (!q(':scope > .ds-theme')) upgrade();
    // Skip to Content, first in the tab order, wherever the page has a #main.
    if (!document.querySelector('.ds-skip') && document.getElementById('main')) {
      var skip = el('a', 'ds-skip', 'Skip to Content');
      skip.href = '#main';
      document.body.insertBefore(skip, document.body.firstChild);
    }

    var nav = q('.ds-nav');
    // Not knowing yet is not the same as signed out: hold the space, show
    // nothing that might be wrong. The platform knows already and says so.
    if (nav && !bar.hasAttribute('data-signed-in')) nav.style.visibility = 'hidden';

    var sw = q('button.ds-switch');
    if (sw) wire(sw, switcherMenu, false);
    var av = q('.ds-me');
    if (av) wire(av, accountMenu, true);
    var tb = q('.ds-theme');
    if (tb) wire(tb, themeMenu, true);
    showPref();
    syncColour();

    // Always asked: /api/me is also how a new device learns the profile's
    // theme. redirect:'manual' because a gated app host answers a signed-out
    // viewer with a 302 to the sign-in host.
    var started = picks;
    var base = bar.dataset.platform || '';
    fetch(base + '/api/me', { credentials: 'include', redirect: 'manual' })
      .then(function (r) {
        if (r.ok) return r.json();
        apply(r.status === 401 || r.type === 'opaqueredirect' ? 'out' : 'unknown');
        return null;
      })
      .then(function (d) {
        if (!d) return;
        if (!d.user) { apply('unknown'); return; }
        // The profile wins once it has an answer, unless the viewer picked
        // something after this request went out.
        var t = d.prefs && d.prefs.theme;
        if (t && picks === started) stampTheme(t);
        // A pick made while this request was out: the response's Set-Cookie
        // carried the profile's older answer, so put the newer pick back.
        if (picks !== started && lastPick) writeThemeCookie(lastPick);
        me = d;
        apply('in');
        showPref();
      })
      .catch(function () {
        /* Offline, or the platform is down. The bar still says where you
           are, which is most of its job. */
        if (state === null) apply('unknown');
      });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
