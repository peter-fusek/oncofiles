/* Opt-in analytics for the public pages (landing, /demo) — #700.
 *
 * Nothing from Google loads before consent: no gtag.js, no dataLayer, no
 * _ga cookie. The banner offers two equal choices; only "accept" calls
 * loadAnalytics(), which is the single place that injects the gtag script.
 * The choice is stored in localStorage (wrapped in try/catch; private mode
 * simply asks again). A footer link with data-consent="open" reopens the
 * banner. No inline handlers (one delegated click listener), no innerHTML.
 * The logged-in dashboard never loads this file.
 */
(function () {
  'use strict';

  var KEY = 'oncofiles_analytics_consent';
  var GA_ID = 'G-JPSK46QKRB';
  var loaded = false;

  function readChoice() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function saveChoice(value) {
    try { localStorage.setItem(KEY, value); } catch (e) { /* private mode: ask again next time */ }
  }
  function cleanUrl(u) {
    // page_location / page_referrer without query string and hash.
    try { var x = new URL(u); return x.origin + x.pathname; } catch (e) { return ''; }
  }

  function loadAnalytics() {
    if (loaded) return;
    loaded = true;
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    window.gtag = gtag;
    // Consent Mode v2: ads stay denied; analytics granted only because the
    // visitor just accepted (or accepted earlier).
    gtag('consent', 'default', {
      ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'granted'
    });
    gtag('js', new Date());
    gtag('config', GA_ID, {
      anonymize_ip: true,
      allow_google_signals: false,
      allow_ad_personalization_signals: false,
      page_location: cleanUrl(window.location.href),
      page_referrer: document.referrer ? cleanUrl(document.referrer) : ''
    });
    var s = document.createElement('script');
    s.async = true;
    s.src = 'https://www.googletagmanager.com/gtag/js?id=' + GA_ID;
    document.head.appendChild(s);
  }

  function banner() { return document.getElementById('cookie-banner'); }
  function showBanner() { var b = banner(); if (b) b.hidden = false; }
  function hideBanner() { var b = banner(); if (b) b.hidden = true; }

  function accept() {
    saveChoice('granted');
    hideBanner();
    loadAnalytics();
  }
  function decline() {
    saveChoice('denied');
    hideBanner();
    // If analytics were already running in this page view, stop storage now;
    // the script itself is gone on the next page load.
    if (loaded && typeof window.gtag === 'function') window.gtag('consent', 'update', { analytics_storage: 'denied' });
  }

  document.addEventListener('click', function (e) {
    var el = e.target.closest && e.target.closest('[data-consent]');
    if (!el) return;
    var action = el.getAttribute('data-consent');
    if (action === 'accept') accept();
    else if (action === 'decline') decline();
    else if (action === 'open') { e.preventDefault(); showBanner(); }
  });

  function init() {
    var choice = readChoice();
    if (choice === 'granted') loadAnalytics();
    else if (choice !== 'denied') showBanner();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
