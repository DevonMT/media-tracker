/* Generated from devondoes/ds/chrome.js — do not edit here.
   Edit the source and run `node ds/sync.mjs`. `--check` fails on drift. */
/*
 * Fills in the estate bar's identity, and signs you out.
 *
 * Deliberately not a framework component: it runs in a static Astro page, a
 * server-rendered template and a Streamlit app equally, and asks nothing of
 * any of them beyond the markup contract in chrome.css.
 *
 * BOTH REQUESTS ARE SAME-ORIGIN. The gateway proxies /api/me and
 * /api/auth/sign-out through to the platform on every app hostname, which is
 * what keeps this free of CORS, of a second allowlist to maintain, and of the
 * "* is invalid with credentials" trap that the drill sync had to solve.
 *
 * An app served from OUTSIDE the gateway — Daily Drill is on Pages — has no
 * such proxy. It sets data-platform on the bar and the calls go to the platform
 * directly, answered by the BAR_ORIGINS allow-list there. Same two paths either
 * way, so the bar never has to know which side of the gateway it is on.
 */
(function () {
  /*
   * Keep the address bar in step with the page.
   *
   * theme-color paints the browser chrome and, on Android, the status bar. It
   * cannot follow a media query, so a page that follows the device ends up with
   * a light bar over a dark page at sunset. Reading --bg means it can never
   * disagree, and doing it here means every app gets it from the shared bar
   * rather than each pasting the same six lines.
   */
  var meta = document.querySelector('meta[name="theme-color"]');
  if (meta) {
    var sync = function () {
      var g = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
      if (g) meta.setAttribute('content', g);
    };
    sync();
    var mq = window.matchMedia('(prefers-color-scheme: dark)');
    if (mq.addEventListener) mq.addEventListener('change', sync);
  }

  var bar = document.querySelector('.ds-bar');
  if (!bar) return;

  var who = bar.querySelector('.ds-who');
  var out = bar.querySelector('.ds-out');
  // A public app has neither; there is nothing to look up.
  if (!who && !out) return;

  var base = bar.dataset.platform || '';

  fetch(base + '/api/me', { credentials: 'include' })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (d) {
      var u = d && d.user;
      if (!u) return;
      // The name if there is one; the address is the fallback because it is the
      // thing somebody signing in with Google has and may not have named.
      if (who) who.textContent = u.name || u.email || '';
      if (out) out.hidden = false;
    })
    .catch(function () {
      /* Signed out, offline, or the platform is down. The bar still says where
         you are, which is most of its job. */
    });

  if (out) {
    // Hidden until identity confirms: a Sign out button shown to somebody who
    // is not signed in does nothing and looks broken.
    out.hidden = true;
    out.addEventListener('click', function () {
      out.disabled = true;
      fetch(base + '/api/sign-out', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: '{}',
      })
        .catch(function () { /* fall through to the reload either way */ })
        .then(function () { location.reload(); });
    });
  }
})();
