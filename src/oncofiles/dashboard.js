    /* All data rendered via safe DOM methods — textContent for strings, createElement for structure.
       The esc() helper is used only as a safety net for the few cases where we build HTML strings
       from our own authenticated API responses (numeric/boolean fields). */

    /* ── #525: inline event-handler delegation ────────────────────────
       Pre-#525 every interactive element used `onclick="foo()"` style
       inline handlers, which forced CSP `script-src-attr 'unsafe-inline'`.
       The dispatcher below routes every click / change / input to the
       function named in `data-action` / `data-change` / `data-input`,
       so the HTML carries pure data and CSP can lock attribute-level
       script execution to 'none'.

       Spec format:  "fnName"               → fn()
                     "fnName|arg1|arg2"     → fn('arg1', 'arg2')
                     "fnName|@this"         → fn(targetElement)
                     "fnName|@value"        → fn(targetElement.value)
                     "foo|x|@this|y"        → fn('x', targetElement, 'y')
       Multiple actions chain with `;`:  "showGdriveSetup;toggleIntegrationPanel"
    */
    /* Demo mode (the public /demo page): the same page and script, fed by one
       invented patient from /api/demo-data. No sign-in, nothing is written. */
    var DEMO = document.documentElement.getAttribute('data-demo') === '1';
    var DEMO_ALLOWED_PATHS = ['/api/demo-data'];
    if (DEMO) {
      // Chokepoint: in demo mode the page can reach nothing but the demo data.
      var _nativeFetch = window.fetch.bind(window);
      window.fetch = function (input, init) {
        var target;
        try { target = new URL(typeof input === 'string' ? input : (input && input.url) || '', location.origin); }
        catch (e) { return Promise.reject(new Error('demo: request blocked')); }
        if (target.origin !== location.origin || DEMO_ALLOWED_PATHS.indexOf(target.pathname) === -1) {
          return Promise.reject(new Error('demo: request blocked: ' + target.pathname));
        }
        return _nativeFetch(input, init);
      };
      // The upload (#921) uses XMLHttpRequest for its progress bar: the same
      // chokepoint for XHR, so no request of the demo can leave this page.
      var _nativeXhrOpen = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function (method, url) {
        var target = new URL(String(url || ''), location.origin);
        if (target.origin !== location.origin || DEMO_ALLOWED_PATHS.indexOf(target.pathname) === -1) {
          throw new Error('demo: request blocked: ' + target.pathname);
        }
        return _nativeXhrOpen.apply(this, arguments);
      };
    }
    // Actions that write or need an account: in the demo they only explain themselves.
    var DEMO_BLOCKED_ACTIONS = {
      showOnboarding: 1, showGdriveSetup: 1, showGdriveSetupAndToggle: 1, triggerManualSync: 1,
      generateShareCode: 1, togglePatientArchive: 1, logout: 1, rotateInboundAddress: 1
    };

    window._dispatch = function (spec, target) {
      if (!spec) return;
      String(spec).split(';').forEach(function (one) {
        one = one.trim();
        if (!one) return;
        var parts = one.split('|');
        if (DEMO && DEMO_BLOCKED_ACTIONS[parts[0]]) { demoNotice(); return; }
        var fn = window[parts[0]];
        if (typeof fn !== 'function') {
          console.warn('data-action: function not found:', parts[0]);
          return;
        }
        var args = parts.slice(1).map(function (a) {
          if (a === '@this') return target;
          if (a === '@value') return target.value;
          return a;
        });
        try { fn.apply(null, args); } catch (e) { console.error('data-action failed:', parts[0], e); }
      });
    };
    document.addEventListener('click', function (e) {
      var t = e.target.closest('[data-action]');
      if (t) window._dispatch(t.dataset.action, t);
    });
    document.addEventListener('change', function (e) {
      var t = e.target.closest('[data-change]');
      if (t) window._dispatch(t.dataset.change, t);
    });
    document.addEventListener('input', function (e) {
      var t = e.target.closest('[data-input]');
      if (t) window._dispatch(t.dataset.input, t);
    });

    /* Named wrappers for the few inline expressions that don't fit the
       fnName(args) shape — keeps `data-action` purely declarative. */
    window.showGdriveSetupAndToggle = function () {
      if (typeof showGdriveSetup === 'function') showGdriveSetup();
      if (typeof toggleIntegrationPanel === 'function') toggleIntegrationPanel();
    };

    let authMode = '';  // 'bearer' or 'session'
    let TOKEN = '';     // bearer token or session token
    let sortCol = 'uploaded_at';  // newest upload first on load
    let sortAsc = false;
    let docsData = [];
    let autoRefreshTimer = null;

    // ── Helpers ──────────────────────────────────────────────────────────

    /** HTML-escape a string (safety net for API data rendered in HTML context). */
    function esc(s) {
      const d = document.createElement('div');
      d.textContent = s || '';
      return d.innerHTML;
    }

    // ── Language toggle (SK/EN) ─────────────────────────────────────────
    var dashLang = localStorage.getItem('oncofiles_lang') || 'sk';

    function setDashLang(lang) {
      dashLang = lang;
      localStorage.setItem('oncofiles_lang', dashLang);
      applyDashLang();
      // names, dates and file names are formatted per language: draw them again
      if (typeof relabelPatients === 'function' && patientsCache.length) relabelPatients();
      if (typeof renderTable === 'function' && docsData.length) renderTable();
      if (typeof renderActivity === 'function' && activityData) renderActivity(null);
      if (typeof renderQuickLook === 'function' && quickLook && quickLook.id !== null) renderQuickLook();
    }

    function toggleDashLang() {
      setDashLang(dashLang === 'sk' ? 'en' : 'sk');
    }

    function applyDashLang() {
      document.documentElement.lang = dashLang;
      var skBtn = document.getElementById('lang-btn-sk');
      var enBtn = document.getElementById('lang-btn-en');
      if (skBtn && enBtn) {
        skBtn.classList.toggle('lang-active', dashLang === 'sk');
        enBtn.classList.toggle('lang-active', dashLang === 'en');
      }
      // NOTE: data-html attribute is only used on developer-controlled elements
      // with hardcoded SK/EN translations — never with user input. This is safe.
      document.querySelectorAll('[data-sk][data-en]').forEach(function(el) {
        var text = dashLang === 'sk' ? el.getAttribute('data-sk')
          : el.getAttribute('data-en');
        if (el.hasAttribute('data-html')) {
          el.innerHTML = text;  // safe: only hardcoded i18n strings, no user input
        } else {
          el.textContent = text;
        }
      });
      // Placeholder i18n for inputs
      document.querySelectorAll('[data-sk-placeholder][data-en-placeholder]').forEach(function(el) {
        el.placeholder = dashLang === 'sk' ? el.getAttribute('data-sk-placeholder') : el.getAttribute('data-en-placeholder');
      });
    }

    // Apply language on load
    applyDashLang();

    /** Give a dynamically built element both languages; applyDashLang() keeps it in sync. */
    function setI18n(el, sk, en) {
      el.setAttribute('data-sk', sk);
      el.setAttribute('data-en', en);
      el.textContent = dashLang === 'sk' ? sk : en;
      return el;
    }

    /** Clear all children of an element. */
    function clearEl(el) {
      while (el.firstChild) el.removeChild(el.firstChild);
    }

    /** Get the Authorization header value based on auth mode. */
    function authHeader() {
      if (authMode === 'session') return 'Bearer session:' + TOKEN;
      return 'Bearer ' + TOKEN;
    }

    // ── Patient state ─────────────────────────────────────────────────
    var currentPatientId = localStorage.getItem('oncofiles_patient_id') || '';
    var patientsCache = [];

    // ── Init ─────────────────────────────────────────────────────────────

    (function init() {
      // #500: bearer tokens MUST NOT be accepted from URL query params.
      // Even with replaceState() cleanup, the token has already been logged
      // by reverse proxies, browser history, and any analytics middleware.
      // If a ?token= is present, scrub it from the URL but do NOT use it.
      var params = new URLSearchParams(window.location.search);
      if (params.has('token')) {
        console.warn('Dashboard ignored ?token= URL param (#500). Use the Authorization header or sign in with Google.');
        params.delete('token');
        var cleanQs = params.toString();
        var cleanUrl = window.location.pathname + (cleanQs ? '?' + cleanQs : '');
        window.history.replaceState({}, '', cleanUrl);
      }
      // ?lang=sk|en picks the language (the demo's hreflang links use it)
      var wantLang = params.get('lang');
      if (wantLang === 'sk' || wantLang === 'en') { dashLang = wantLang; applyDashLang(); }
      if (DEMO) {
        ensureDemoData().then(showDashboard).catch(function () {
          document.getElementById('dashboard').style.display = 'block';
          showError(dashLang === 'sk' ? 'Ukážku sa nepodarilo načítať.' : 'The demo could not be loaded.');
        });
        return;
      }
      TOKEN = sessionStorage.getItem('oncofiles_token') || '';
      authMode = sessionStorage.getItem('oncofiles_auth_mode') || '';

      if (TOKEN && authMode) {
        showDashboard();
      } else {
        initGoogleSignIn();
      }
    })();

    function initGoogleSignIn() {
      document.getElementById('auth-gate').style.display = 'flex';
      // Fetch client_id from server config
      fetch('/dashboard/config')
        .then(function(r) { return r.json(); })
        .then(function(cfg) {
          if (!cfg.client_id) {
            showAuthError('Google OAuth not configured on server');
            return;
          }
          // Wait for GSI library to load (max 10 seconds)
          var gsiRetries = 0;
          function tryInit() {
            if (typeof google !== 'undefined' && google.accounts) {
              google.accounts.id.initialize({
                client_id: cfg.client_id,
                callback: handleGoogleCredential,
                ux_mode: 'popup',
                use_fedcm_for_prompt: true
              });
              google.accounts.id.renderButton(
                document.getElementById('g_id_signin'),
                { theme: 'filled_black', size: 'large', text: 'signin_with', shape: 'rectangular' }
              );
            } else if (gsiRetries++ < 50) {
              setTimeout(tryInit, 200);
            } else {
              showAuthError('Google Sign-In library failed to load. Check your network connection.');
            }
          }
          tryInit();
        })
        .catch(function(e) {
          showAuthError('Failed to load config: ' + e.message);
        });
    }

    function handleGoogleCredential(response) {
      // Send credential to server for verification
      fetch('/dashboard/verify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: response.credential })
      })
        .then(function(r) {
          if (r.status === 403) throw new Error('Access denied — your email is not authorized');
          if (r.status === 401) throw new Error('Invalid Google token — please try again');
          if (!r.ok) throw new Error('Server error (' + r.status + ')');
          return r.json();
        })
        .then(function(data) {
          TOKEN = data.session_token;
          authMode = 'session';
          sessionStorage.setItem('oncofiles_token', TOKEN);
          sessionStorage.setItem('oncofiles_auth_mode', 'session');
          sessionStorage.setItem('oncofiles_email', data.email || '');
          sessionStorage.setItem('oncofiles_name', data.name || '');
          showDashboard();
        })
        .catch(function(e) {
          showAuthError(e.message);
        });
    }

    function showAuthError(msg) {
      var el = document.getElementById('auth-error');
      el.textContent = msg;
      el.style.display = 'block';
    }

    function showDashboard() {
      var gate = document.getElementById('auth-gate');
      if (gate) gate.style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      // Show user info
      var email = sessionStorage.getItem('oncofiles_email') || '';
      var name = sessionStorage.getItem('oncofiles_name') || '';
      var userEl = document.getElementById('user-info');
      if (email && userEl) userEl.textContent = name || email;
      loadPatients().then(function() { loadGermlineBanner(); refresh(); });
      if (DEMO) return;  // static invented data: nothing to poll
      loadOnboardingStatus();
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = setInterval(refresh, 60000);
    }

    function logout() {
      // #510: revoke server-side BEFORE wiping the local token, so the POST
      // can still authenticate. Fire-and-forget — we always proceed with
      // the local cleanup even if the network call fails (offline logout
      // is still better than no logout).
      try {
        var prevToken = TOKEN;
        var prevMode = authMode;
        if (prevToken && prevMode === 'session') {
          fetch('/api/logout', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer session:' + prevToken },
            credentials: 'include',
            keepalive: true,
          }).catch(function(){ /* offline / network failure — local cleanup still runs */ });
        } else if (prevToken) {
          // Bearer-mode: the cookie may still hold a session token from
          // an earlier dashboard sign-in; clear it via cookie auth.
          fetch('/api/logout', {
            method: 'POST',
            credentials: 'include',
            keepalive: true,
          }).catch(function(){});
        }
      } catch (e) { /* never block logout on a script error */ }
      sessionStorage.removeItem('oncofiles_token');
      sessionStorage.removeItem('oncofiles_auth_mode');
      sessionStorage.removeItem('oncofiles_email');
      sessionStorage.removeItem('oncofiles_name');
      _swrCacheClearAll();
      closeQuickLook();
      resetThumbs();  // previews belong to this sign-in
      TOKEN = '';
      authMode = '';
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      document.getElementById('dashboard').style.display = 'none';
      // Revoke Google session
      if (typeof google !== 'undefined' && google.accounts) {
        google.accounts.id.disableAutoSelect();
      }
      initGoogleSignIn();
    }

    // ── Stale-while-revalidate cache (#469 Phase 5) ──────────────────────
    // Cache /status, /api/documents, /api/prompt-log responses in
    // sessionStorage (10 min TTL, per-patient keys). On refresh() start we
    // hydrate from cache so the UI stays populated during fetch — instead of
    // tearing down to skeletons every tick. On a 503 the cached
    // data keeps rendering while the countdown banner handles recovery.

    var _SWR_MAX_AGE_MS = 10 * 60 * 1000; // 10 minutes — "stale" ceiling
    var _SWR_PREFIX = 'dashCache:';

    function _swrKey(endpoint) {
      return _SWR_PREFIX + (currentPatientId || 'default') + ':' + endpoint;
    }

    function _swrGet(endpoint) {
      try {
        var raw = sessionStorage.getItem(_swrKey(endpoint));
        if (!raw) return null;
        var entry = JSON.parse(raw);
        if (!entry || !entry.cached_at) return null;
        var ageMs = Date.now() - entry.cached_at;
        if (ageMs > _SWR_MAX_AGE_MS) {
          sessionStorage.removeItem(_swrKey(endpoint));
          return null;
        }
        return { data: entry.data, ageMs: ageMs };
      } catch (e) { return null; }
    }

    function _swrSet(endpoint, data) {
      try {
        sessionStorage.setItem(_swrKey(endpoint), JSON.stringify({
          data: data,
          cached_at: Date.now()
        }));
      } catch (e) { /* quota / JSON errors — non-fatal */ }
    }

    function _swrCacheClearAll() {
      try {
        for (var i = sessionStorage.length - 1; i >= 0; i--) {
          var k = sessionStorage.key(i);
          if (k && k.indexOf(_SWR_PREFIX) === 0) sessionStorage.removeItem(k);
        }
      } catch (e) { /* ignore */ }
    }

    // Budget pill (#442) — fetches /api/patient-budget and reflects the
    // patient's rolling 30-day AI spend. Hidden for admin/paid_* tiers
    // (server returns show_pill=false). Piggybacks on refresh() cycle.
    async function updateBudgetPill() {
      var pill = document.getElementById('budget-pill');
      if (!pill) return;
      try {
        var query = currentPatientId ? ('?patient_id=' + encodeURIComponent(currentPatientId)) : '';
        var resp = await apiFetch('/api/patient-budget' + query);
        if (!resp || !resp.show_pill) {
          pill.style.display = 'none';
          return;
        }
        pill.style.display = 'inline-flex';
        pill.classList.remove('state-warn', 'state-over');
        var used = Number(resp.used_eur || 0);
        var cap = Number(resp.cap_eur || 0);
        var ratio = cap > 0 ? (used / cap) : 0;
        if (ratio >= 1) pill.classList.add('state-over');
        else if (ratio >= 0.8) pill.classList.add('state-warn');
        var iconEl = document.getElementById('bp-icon');
        // Text, not an emoji: "Starter" while the onboarding credit applies, else €.
        if (iconEl) {
          if (resp.in_onboarding) setI18n(iconEl, 'Štart', 'Starter');
          else { iconEl.removeAttribute('data-sk'); iconEl.removeAttribute('data-en'); iconEl.textContent = '€'; }
        }
        var spentEl = document.getElementById('bp-spent');
        if (spentEl) spentEl.textContent = '€' + used.toFixed(2);
        var capEl = document.getElementById('bp-cap');
        if (capEl) capEl.textContent = '€' + cap.toFixed(0);
        var tip = resp.in_onboarding
          ? 'Onboarding bonus — prvý mesiac zadarmo / free trial first month'
          : 'Mesačný rozpočet AI / Monthly AI budget';
        if (resp.onboarding_ends_at) tip += ' · ends ' + resp.onboarding_ends_at.slice(0, 10);
        pill.title = tip;
      } catch (e) {
        // Silent — pill stays at last known state; the 503 banner handles UX
      }
    }

    function _hydrateFromSwrCache() {
      // Try to populate the critical sections from cache. Returns true if
      // at least one section was hydrated — caller skips skeletons in that case.
      if (DEMO) return false;
      var hydrated = false;
      var s = _swrGet('status');
      if (s) { try { renderStatus(s.data); hydrated = true; } catch (e) { console.warn('SWR status render failed:', e); } }
      var d = _swrGet('documents');
      if (d) { try { renderDocs(d.data); hydrated = true; } catch (e) { console.warn('SWR documents render failed:', e); } }
      var a = _swrGet('activity');
      if (a) { try { renderActivity(a.data); hydrated = true; } catch (e) { console.warn('SWR activity render failed:', e); } }
      return hydrated;
    }

    function showError(msg) {
      const banner = document.getElementById('error-banner');
      document.getElementById('error-text').textContent = msg;
      banner.classList.add('visible');
    }

    function hideError() {
      document.getElementById('error-banner').classList.remove('visible');
      _clearBreakerCountdown();
    }

    // ── 503 + Retry-After countdown banner (#469) ───────────────────────
    // The "breaker" names are historical: the DB circuit breaker is gone
    // (#617, #751), but real 503s with Retry-After still happen (e.g. rate
    // limiting, #591), so the banner and trackBreaker() stay.

    var _breakerCountdownTimer = null;
    var _breakerSecondsRemaining = 0;

    function _clearBreakerCountdown() {
      if (_breakerCountdownTimer) {
        clearInterval(_breakerCountdownTimer);
        _breakerCountdownTimer = null;
      }
      _breakerSecondsRemaining = 0;
    }

    function _parseRetryAfter(headerValue) {
      if (!headerValue) return null;
      var n = parseInt(headerValue, 10);
      if (!isNaN(n) && n >= 0) return n * 1000;
      var d = Date.parse(headerValue);
      if (!isNaN(d)) return Math.max(0, d - Date.now());
      return null;
    }

    function _jitterMs(attempt, baseMs, maxMs) {
      var exp = Math.min(maxMs, baseMs * Math.pow(2, attempt - 1));
      var jitter = exp * 0.25 * (Math.random() * 2 - 1); // ±25%
      return Math.max(0, Math.floor(exp + jitter));
    }

    function showBreakerBanner(retryAfterMs) {
      _clearBreakerCountdown();
      _breakerSecondsRemaining = Math.max(1, Math.ceil(retryAfterMs / 1000));
      var banner = document.getElementById('error-banner');
      var text = document.getElementById('error-text');
      banner.classList.add('visible');

      function render() {
        var s = _breakerSecondsRemaining;
        text.textContent = dashLang === 'sk'
          ? 'Databáza je krátko nedostupná. Skúsim znova o ' + s + ' s…'
          : 'Database briefly unavailable. Retrying in ' + s + 's…';
      }
      render();

      _breakerCountdownTimer = setInterval(function() {
        _breakerSecondsRemaining -= 1;
        if (_breakerSecondsRemaining <= 0) {
          _clearBreakerCountdown();
          refresh();
        } else {
          render();
        }
      }, 1000);
    }

    // ── API ──────────────────────────────────────────────────────────────

    /**
     * Fetch with 503 + Retry-After awareness.
     *
     * - 401 → logout, throw 'Unauthorized'.
     * - 503 → throw immediately with {status: 503, retryAfterMs} so the caller
     *         can surface the friendly countdown banner; the banner IS the retry.
     * - 500/502/504 → retry with exponential jitter (base 500ms, cap 5s,
     *         ±25% jitter), max 3 attempts, then throw HTTP <status>.
     * - AbortError (per-request timeout 25s) → same retry path as 5xx.
     * - 4xx (except 401) and 2xx (not ok) → throw HTTP <status>.
     *
     * See #412 (server-side 503 contract) and #469 (client rollout).
     */
    async function apiFetch(url, opts) {
      opts = opts || {};
      if (DEMO) {
        await ensureDemoData();
        var demoAnswer = demoRoute(url);
        if (demoAnswer == null) throw new Error('demo: no data for ' + url);
        return JSON.parse(JSON.stringify(demoAnswer));
      }
      var retryBudget = opts.retryBudget !== undefined ? opts.retryBudget : 3;
      var perRequestTimeoutMs = opts.perRequestTimeoutMs || 25000;

      if (currentPatientId) {
        var sep = url.includes('?') ? '&' : '?';
        url = url + sep + 'patient_id=' + encodeURIComponent(currentPatientId);
      }

      var attempt = 0;
      while (true) {
        var ac = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        var tid = ac ? setTimeout(function(){ ac.abort(); }, perRequestTimeoutMs) : null;
        try {
          var resp = await fetch(url, {
            headers: { 'Authorization': authHeader() },
            signal: ac ? ac.signal : undefined
          });
          if (tid) clearTimeout(tid);

          if (resp.status === 401) {
            logout();
            throw new Error('Unauthorized');
          }

          // 503 — surface to caller immediately. The caller renders a
          // countdown banner that auto-retries at Retry-After. No inner retry:
          // retrying inside the cooldown is guaranteed to fail.
          if (resp.status === 503) {
            var retryAfterMs = _parseRetryAfter(resp.headers.get('Retry-After'));
            var err503 = new Error('HTTP 503');
            err503.status = 503;
            err503.retryAfterMs = retryAfterMs != null ? retryAfterMs : 30000;
            throw err503;
          }

          // 500/502/504 — transient. Retry up to budget with jitter.
          if (resp.status === 500 || resp.status === 502 || resp.status === 504) {
            if (attempt < retryBudget) {
              attempt += 1;
              await new Promise(function(r){ setTimeout(r, _jitterMs(attempt, 500, 5000)); });
              continue;
            }
            var err5xx = new Error('HTTP ' + resp.status);
            err5xx.status = resp.status;
            throw err5xx;
          }

          if (!resp.ok) {
            var errNok = new Error('HTTP ' + resp.status);
            errNok.status = resp.status;
            throw errNok;
          }
          return resp.json();
        } catch (e) {
          if (tid) clearTimeout(tid);
          // Per-request timeout → treat as transient, retry within budget.
          if (e && e.name === 'AbortError') {
            if (attempt < retryBudget) {
              attempt += 1;
              await new Promise(function(r){ setTimeout(r, _jitterMs(attempt, 500, 5000)); });
              continue;
            }
            throw new Error('request timed out');
          }
          throw e;
        }
      }
    }

    // ── Demo data (public /demo) ────────────────────────────────────────
    var demoData = null;

    async function ensureDemoData() {
      if (demoData) return demoData;
      var resp = await fetch('/api/demo-data');
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      demoData = await resp.json();
      return demoData;
    }

    /** The demo twin of an API path: the same JSON shape the real route returns. */
    function demoRoute(url) {
      var path = new URL(url, location.origin).pathname;
      var d = (demoData && demoData.dashboard) || {};
      if (path === '/status') return d.status;
      if (path === '/api/documents') return withDemoUploads(d.documents);
      if (path === '/api/activity') return d.activity;
      if (path === '/api/inbound-address') return d.inbound_address;
      if (path === '/api/patients') return d.patients;
      if (path === '/api/patient-budget') return { show_pill: false };
      if (path === '/api/manifest') return { generated_at: new Date().toISOString(), invented: true, documents: (d.documents || {}).documents || [] };
      if (path.indexOf('/api/doc-detail/') === 0) {
        var detailId = path.split('/').pop();
        return (d.details || {})[detailId] || { error: 'not found' };
      }
      if (path === '/api/doc-thumbnails') {
        // the ids asked for, each with its drawing or null, like the real route (#948)
        var all = d.thumbnails || {};
        var out = {};
        String(new URL(url, location.origin).searchParams.get('ids') || '').split(',').forEach(function (id) {
          id = id.trim();
          if (id) out[id] = all[id] || null;
        });
        return { thumbnails: out };
      }
      return null;
    }

    var demoNoticeTimer = null;
    function demoNotice() {
      var el = document.getElementById('demo-toast');
      if (!el) {
        el = document.createElement('div');
        el.id = 'demo-toast';
        el.className = 'demo-toast';
        el.setAttribute('role', 'status');
        document.body.appendChild(el);
      }
      setI18n(el, 'V ukážke je táto akcia vypnutá. Po prihlásení funguje nad vašimi dokumentmi.',
        'This action is switched off in the demo. After sign-in it works on your own documents.');
      el.classList.add('visible');
      if (demoNoticeTimer) clearTimeout(demoNoticeTimer);
      demoNoticeTimer = setTimeout(function () { el.classList.remove('visible'); }, 4500);
    }

    // ── Add documents (#921) ────────────────────────────────────────────
    // Files go one at a time (one request each: the server holds one DB
    // connection and one Drive writer) to POST /api/upload, which puts them
    // unchanged into the patient's inbox on Drive. In the demo nothing is
    // sent: the files become invented inbox rows ("Waiting to be processed"),
    // the same shape GET /api/documents gives a file waiting in the inbox.

    var UPLOAD_MAX_BYTES = 25 * 1024 * 1024;
    var UPLOAD_MAX_FILES = 20;
    var UPLOAD_EXTS = ['pdf', 'jpg', 'jpeg', 'png', 'heic', 'heif', 'tif', 'tiff', 'webp', 'md', 'txt'];
    var UPLOAD_GENOTYPE_EXTS = ['vcf', 'gz', 'csv', 'tsv'];
    var UPLOAD_CODE_TEXT = {
      type_not_allowed: ['Tento typ súboru sa nenahráva.', 'This file type is not accepted.'],
      raw_genotype_excluded: ['Surové genetické dáta sa nenahrávajú.', 'Raw genetic data is not accepted.'],
      file_too_large: ['Súbor má viac ako 25 MB.', 'The file is larger than 25 MB.'],
      empty_file: ['Súbor je prázdny.', 'The file is empty.'],
      content_mismatch: ['Obsah súboru nezodpovedá jeho prípone.', 'The file content does not match its extension.'],
      not_attempted: ['Neodoslané, predchádzajúci súbor zlyhal.', 'Not sent, the previous file failed.'],
      failed: ['Nahrávanie zlyhalo. Skúste to znova neskôr.', 'The upload failed. Try again later.']
    };
    var UPLOAD_SLOW_TEXT = ['Spojenie bolo na tento súbor príliš pomalé alebo sa prerušilo. Skúste Wi-Fi alebo menší súbor.',
      'The connection was too slow for this file or was interrupted. Try Wi-Fi or a smaller file.'];
    var uploadQueue = Promise.resolve();
    var demoUploads = [];
    var demoUploadSeq = 0;

    function toggleUploadPanel() {
      var panel = document.getElementById('upload-panel');
      var btn = document.getElementById('upload-btn');
      if (!panel || !btn) return;
      panel.hidden = !panel.hidden;
      btn.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
    }

    function closeUploadPanel() {
      var panel = document.getElementById('upload-panel');
      var btn = document.getElementById('upload-btn');
      if (panel) panel.hidden = true;
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }

    function openUploadPanel() {
      var panel = document.getElementById('upload-panel');
      if (panel && panel.hidden) toggleUploadPanel();
    }

    function uploadFromInput(input) {
      var files = Array.prototype.slice.call(input.files || []);
      input.value = '';  // the same file can be chosen again
      uploadFiles(files);
    }

    function fmtSize(bytes) {
      if (bytes < 1024 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' kB';
      return (bytes / (1024 * 1024)).toFixed(1).replace('.', dashLang === 'sk' ? ',' : '.') + ' MB';
    }

    function uploadExt(name) {
      var i = name.lastIndexOf('.');
      return i > 0 ? name.slice(i + 1).toLowerCase() : '';
    }

    /** The browser's own check (the server checks again): a refusal code or ''. */
    function uploadPrecheck(file) {
      var ext = uploadExt(file.name || '');
      if (UPLOAD_GENOTYPE_EXTS.indexOf(ext) !== -1) return 'raw_genotype_excluded';
      if (UPLOAD_EXTS.indexOf(ext) === -1) return 'type_not_allowed';
      if (!file.size) return 'empty_file';
      if (file.size > UPLOAD_MAX_BYTES) return 'file_too_large';
      return '';
    }

    function uploadNote(sk, en) {
      var note = document.getElementById('upload-note');
      if (!note) return;
      if (!sk) { note.hidden = true; note.textContent = ''; return; }
      setI18n(note, sk, en);
      note.hidden = false;
    }

    /** One row of the upload list; returns setters for its progress and state. */
    function uploadItem(file) {
      var list = document.getElementById('upload-list');
      var li = document.createElement('li');
      li.className = 'upload-item';
      var mark = stateMark('progress');
      li.appendChild(mark);
      var main = document.createElement('div');
      main.className = 'upload-main';
      var name = document.createElement('span');
      name.className = 'upload-name';
      name.textContent = file.name;
      main.appendChild(name);
      var meta = document.createElement('span');
      meta.className = 'upload-meta';
      meta.textContent = fmtSize(file.size || 0);
      main.appendChild(meta);
      var bar = document.createElement('progress');
      bar.className = 'upload-bar';
      bar.max = 100;
      bar.value = 0;
      bar.setAttribute('aria-label', file.name);
      main.appendChild(bar);
      var state = document.createElement('span');
      state.className = 'upload-state';
      setI18n(state, 'Čaká na odoslanie', 'Waiting to be sent');
      main.appendChild(state);
      li.appendChild(main);
      if (list) list.insertBefore(li, list.firstChild);
      return {
        progress: function (pct) { bar.value = pct; setI18n(state, 'Odosiela sa… ' + pct + ' %', 'Sending… ' + pct + ' %'); },
        done: function (kind, sk, en) {
          li.replaceChild(stateMark(kind), li.firstChild);
          li.classList.add('is-' + kind);
          bar.value = 100;
          bar.hidden = true;
          setI18n(state, sk, en);
        }
      };
    }

    function uploadFiles(files) {
      files = (files || []).filter(function (f) { return f && f.name && f.name.charAt(0) !== '.'; });
      if (!files.length) return;
      openUploadPanel();
      if (files.length > UPLOAD_MAX_FILES) {
        uploadNote('Naraz najviac ' + UPLOAD_MAX_FILES + ' súborov; vybrali ste ' + files.length + '. Rozdeľte ich, prosím, na menšie dávky.',
          'At most ' + UPLOAD_MAX_FILES + ' files at a time; you chose ' + files.length + '. Please split them into smaller batches.');
        return;
      }
      if (DEMO) { simulateDemoUpload(files); return; }  // nothing leaves the browser
      if (isAdminMode || !currentPatientId) return;
      uploadNote('', '');
      var items = files.map(function (f) { return { file: f, row: uploadItem(f) }; });
      items.forEach(function (it) {
        uploadQueue = uploadQueue.then(function () { return uploadOne(it.file, it.row); });
      });
      uploadQueue = uploadQueue.then(function () { refresh(); });
    }

    /** Send one file; always resolves (a failure is shown on its row). */
    function uploadOne(file, row) {
      var code = uploadPrecheck(file);
      if (code) {
        var t = UPLOAD_CODE_TEXT[code];
        row.done('attention', t[0], t[1]);
        return Promise.resolve();
      }
      return new Promise(function (resolve) {
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload?patient_id=' + encodeURIComponent(currentPatientId));
        xhr.setRequestHeader('Authorization', authHeader());
        xhr.upload.onprogress = function (e) {
          if (e.lengthComputable) row.progress(Math.min(99, Math.round(e.loaded * 100 / e.total)));
        };
        // A request longer than the proxy's read timeout (60 s) is cut: on a weak
        // uplink a large file ends as a reset, a timeout or a 502/504. Say so
        // plainly; never retry by itself (the person decides).
        var tooSlow = function () {
          row.done('attention', UPLOAD_SLOW_TEXT[0], UPLOAD_SLOW_TEXT[1]);
          resolve();
        };
        xhr.onerror = tooSlow;
        xhr.ontimeout = tooSlow;
        xhr.onload = function () {
          var data = {};
          try { data = JSON.parse(xhr.responseText || '{}'); } catch (e) { data = {}; }
          if (xhr.status === 401) { logout(); resolve(); return; }
          if (xhr.status === 502 || xhr.status === 504 || xhr.status === 0) { tooSlow(); return; }
          if (xhr.status === 413) {
            row.done('attention', UPLOAD_CODE_TEXT.file_too_large[0], UPLOAD_CODE_TEXT.file_too_large[1]);
          } else if (xhr.status !== 200) {
            row.done('attention', data.message_sk || 'Nahrávanie bolo odmietnuté.', data.message_en || 'The upload was refused.');
          } else {
            var f = (data.files || [])[0] || {};
            if (f.status === 'uploaded') {
              if (data.path === 'curated_inbox') {
                row.done('ready', 'V priečinku pre nové súbory; spracovanie začne o niekoľko minút.', 'In the inbox; processing starts in a few minutes.');
              } else {
                row.done('ready', 'Uložené na Google Drive; čaká na vyťaženie.', 'Saved to Google Drive; waiting for extraction.');
              }
            } else {
              var text = UPLOAD_CODE_TEXT[f.code] || UPLOAD_CODE_TEXT.failed;
              row.done('attention', text[0], text[1]);
            }
          }
          resolve();
        };
        var form = new FormData();
        form.append('files', file, file.name);
        xhr.send(form);
      });
    }

    /** Demo: invented rows only, nothing is read or sent. */
    function simulateDemoUpload(files) {
      var now = new Date().toISOString().slice(0, 19);
      files.forEach(function (f) {
        var row = uploadItem(f);
        var code = uploadPrecheck(f);
        if (code) { row.done('attention', UPLOAD_CODE_TEXT[code][0], UPLOAD_CODE_TEXT[code][1]); return; }
        demoUploadSeq += 1;
        // the same shape as an inbox row of GET /api/documents (dashboard_inbox.row)
        demoUploads.push({
          id: 'inbox:demo-upload-' + demoUploadSeq, inbox: true, filename: f.name, category: null,
          date: null, institution: null, gdrive_id: null, size: f.size || 0, mime: f.type || '',
          uploaded_at: now, has_ocr: false, has_ai: false,
          has_metadata: false, has_date: false, has_institution: false, is_synced: false,
          is_standard_name: false, fully_complete: false, curated: true, attention_reason: null,
          ready: false, stage: 'in_inbox', steps: { extracted: false, named: false, filed: false }
        });
        row.done('ready', 'Ukážka: nič sa neodoslalo, riadok je len v tomto prehliadači.', 'Demo: nothing was uploaded, the row exists only in this browser.');
      });
      uploadNote('Ukážka: súbory sa neodoslali. V aplikácii by išli do priečinka pacienta na Google Drive a hneď by sa tu zobrazili ako „Čaká na spracovanie“.',
        'Demo: nothing was uploaded. In the app the files would go into the patient folder on Google Drive and show up here right away as “Waiting to be processed”.');
      refresh();
    }

    function withDemoUploads(docs) {
      if (!docs || !demoUploads.length) return docs;
      var copy = JSON.parse(JSON.stringify(docs));
      copy.documents = (copy.documents || []).concat(JSON.parse(JSON.stringify(demoUploads)));
      if (copy.summary) {
        copy.summary.total = (copy.summary.total || 0) + demoUploads.length;
        copy.summary.in_progress = (copy.summary.in_progress || 0) + demoUploads.length;
        copy.summary.inbox = (copy.summary.inbox || 0) + demoUploads.length;
      }
      return copy;
    }

    /** Drop files or folders anywhere on the document section. */
    function droppedFiles(dt) {
      var items = dt && dt.items ? Array.prototype.slice.call(dt.items) : [];
      var entries = items.map(function (it) { return it.webkitGetAsEntry ? it.webkitGetAsEntry() : null; })
        .filter(function (e) { return e; });
      if (!entries.length) return Promise.resolve(Array.prototype.slice.call((dt && dt.files) || []));
      function walk(entry) {
        if (entry.isFile) return new Promise(function (res) { entry.file(function (f) { res([f]); }, function () { res([]); }); });
        if (!entry.isDirectory) return Promise.resolve([]);
        var reader = entry.createReader();
        return new Promise(function (res) {
          var all = [];
          (function more() {
            reader.readEntries(function (batch) {
              if (!batch.length) {
                Promise.all(all.map(walk)).then(function (lists) { res([].concat.apply([], lists)); });
                return;
              }
              all = all.concat(batch);
              more();
            }, function () { res([]); });
          })();
        });
      }
      return Promise.all(entries.map(walk)).then(function (lists) { return [].concat.apply([], lists); });
    }

    function initUploadDrop() {
      var zone = document.querySelector('.docs-section');
      if (!zone) return;
      function canUpload(e) {
        var types = e.dataTransfer ? Array.prototype.slice.call(e.dataTransfer.types || []) : [];
        var btn = document.getElementById('upload-btn');
        return types.indexOf('Files') !== -1 && btn && !btn.hidden;
      }
      zone.addEventListener('dragover', function (e) {
        if (!canUpload(e)) return;
        e.preventDefault();
        zone.classList.add('is-dropping');
      });
      zone.addEventListener('dragleave', function (e) {
        if (!zone.contains(e.relatedTarget)) zone.classList.remove('is-dropping');
      });
      zone.addEventListener('drop', function (e) {
        zone.classList.remove('is-dropping');
        if (!canUpload(e)) return;
        e.preventDefault();
        droppedFiles(e.dataTransfer).then(uploadFiles);
      });
    }
    initUploadDrop();

    // ── Document e-mail address (#950) ──────────────────────────────────
    // The caregiver's private address for documents: copy, vCard, rotate (an
    // armed second click, no browser dialog). Hidden for an admin's read-only
    // view (the server refuses too) and while the server has it switched off.
    // In /demo the address is invented and rotating only explains itself.

    var inboundData = null;
    var inboundFor = null;
    var inboundArmed = null;

    function inboundStatus(sk, en) {
      var el = document.getElementById('inbound-status');
      if (!el) return;
      if (!sk) { el.hidden = true; el.textContent = ''; return; }
      setI18n(el, sk, en);
      el.hidden = false;
    }

    function hideInboundCard() {
      var card = document.getElementById('inbound-card');
      if (card) card.hidden = true;
      inboundData = null;
    }

    function showInboundAddress(data) {
      var card = document.getElementById('inbound-card');
      var addr = document.getElementById('inbound-address');
      if (!card || !addr) return;
      inboundData = data;
      addr.textContent = data.address;
      card.hidden = false;
    }

    async function loadInboundAddress(force) {
      if (!document.getElementById('inbound-card')) return;
      if (isAdminMode || (!DEMO && !currentPatientId)) { hideInboundCard(); inboundFor = null; return; }
      if (!force && inboundFor === currentPatientId) return;
      inboundFor = currentPatientId;
      inboundStatus('', '');
      try {
        var data = await apiFetch('/api/inbound-address');
        if (!data || !data.enabled || !data.address) { hideInboundCard(); return; }
        showInboundAddress(data);
      } catch (e) {
        hideInboundCard();
      }
    }

    function copyInboundAddress() {
      if (!inboundData) return;
      var text = inboundData.address;
      var done = function () { inboundStatus('Adresa je skopírovaná.', 'The address is copied.'); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          inboundStatus('Kopírovanie sa nepodarilo; označte adresu a skopírujte ju ručne.',
            'Copying failed; select the address and copy it by hand.');
        });
        return;
      }
      var el = document.getElementById('inbound-address');
      if (el && window.getSelection) {
        var range = document.createRange();
        range.selectNodeContents(el);
        var sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }

    function downloadInboundVcard() {
      if (!inboundData) return;
      var name = dashLang === 'sk' ? 'Oncofiles dokumenty' : 'Oncofiles documents';
      var card = ['BEGIN:VCARD', 'VERSION:3.0', 'FN:' + name, 'N:;' + name + ';;;',
        'EMAIL;TYPE=INTERNET:' + inboundData.address, 'END:VCARD', ''].join('\r\n');
      var url = URL.createObjectURL(new Blob([card], { type: 'text/vcard' }));
      var a = document.createElement('a');
      a.href = url;
      a.download = 'oncofiles-documents.vcf';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    }

    async function rotateInboundAddress() {
      if (!inboundData || isAdminMode || !currentPatientId) return;
      var btn = document.getElementById('inbound-rotate-btn');
      if (inboundArmed !== currentPatientId) {
        inboundArmed = currentPatientId;
        if (btn) {
          btn.classList.add('armed');
          setI18n(btn, 'Stará adresa prestane fungovať. Kliknite znova', 'The old address stops working. Click again');
        }
        setTimeout(function () {
          if (inboundArmed === null) return;
          inboundArmed = null;
          if (btn) { btn.classList.remove('armed'); setI18n(btn, 'Vytvoriť novú adresu', 'Create a new address'); }
        }, 6000);
        return;
      }
      inboundArmed = null;
      if (btn) { btn.classList.remove('armed'); setI18n(btn, 'Vytvoriť novú adresu', 'Create a new address'); }
      try {
        var resp = await fetch('/api/inbound-address/rotate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Authorization': authHeader() },
          body: JSON.stringify({ patient_id: currentPatientId })
        });
        if (resp.status === 401) { logout(); return; }
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var data = await resp.json();
        if (!data || !data.address) throw new Error('no address');
        showInboundAddress(data);
        inboundStatus('Nová adresa je aktívna; stará už dokumenty neprijíma.',
          'The new address is active; the old one no longer takes documents.');
      } catch (e) {
        inboundStatus('Novú adresu sa nepodarilo vytvoriť. Skúste to znova neskôr.',
          'A new address could not be created. Try again later.');
      }
    }

    // Admin zone: mail to patient addresses that was not filed (counts only).
    var INBOUND_REASON_TEXT = {
      unknown_sender: ['odosielateľ nie je opatrovateľ', 'sender is not the caregiver'],
      auth_failed: ['overenie odosielateľa neprešlo', 'sender check failed']
    };

    function renderInboundUnmatched(data) {
      var totalsEl = document.getElementById('inbound-admin-totals');
      var body = document.getElementById('inbound-admin-body');
      if (!totalsEl || !body) return;
      var t = (data && data.totals) || {};
      var unmatched = (t.unmatched_unknown_sender || 0) + (t.unmatched_auth_failed || 0);
      setI18n(totalsEl,
        (data && data.enabled ? 'Príjem pošty je zapnutý. ' : 'Príjem pošty je vypnutý (chýba tajomstvo webhooku). ') +
          'Zaradené správy: ' + (t.mails_filed || 0) + ', prijaté súbory: ' + (t.files_accepted || 0) +
          ', nezaradené: ' + unmatched + ', neznáma adresa: ' + ((t.unknown_token || 0) + (t.inactive_token || 0)) + '.',
        (data && data.enabled ? 'Receiving mail is on. ' : 'Receiving mail is off (no webhook secret). ') +
          'Filed mails: ' + (t.mails_filed || 0) + ', files accepted: ' + (t.files_accepted || 0) +
          ', unmatched: ' + unmatched + ', unknown address: ' + ((t.unknown_token || 0) + (t.inactive_token || 0)) + '.');
      clearEl(body);
      // #952: durable queue (counts survive restarts). download_expired = the
      // attachments waited longer than Brevo keeps them; nothing was filed.
      var jobs = data && data.jobs;
      if (jobs) {
        var fr = jobs.failed_reasons || {};
        var jobLine = document.createElement('p');
        jobLine.className = 'curated-ops-meta';
        setI18n(jobLine,
          'Fronta: čaká ' + (jobs.pending || 0) + ', spracúva sa ' + (jobs.processing || 0) +
            ', hotové ' + (jobs.done || 0) + ', zlyhalo ' + (jobs.failed || 0) +
            ' (prílohy expirovali: ' + (fr.download_expired || 0) + ', limit pokusov: ' + (fr.max_attempts || 0) + ').',
          'Queue: pending ' + (jobs.pending || 0) + ', processing ' + (jobs.processing || 0) +
            ', done ' + (jobs.done || 0) + ', failed ' + (jobs.failed || 0) +
            ' (attachments expired: ' + (fr.download_expired || 0) + ', attempts used up: ' + (fr.max_attempts || 0) + ').');
        body.appendChild(jobLine);
      }
      var senders = (data && data.senders) || [];
      if (!senders.length) {
        var empty = document.createElement('p');
        empty.className = 'curated-ops-meta';
        setI18n(empty, 'Žiadni nepriradení odosielatelia.', 'No unmatched senders.');
        body.appendChild(empty);
        return;
      }
      var table = document.createElement('table');
      table.className = 'inbound-admin-table';
      var head = document.createElement('tr');
      [['Odosielateľ (odtlačok)', 'Sender (hash)'], ['Pacient', 'Patient'], ['Správy', 'Mails'],
        ['Súbory', 'Files'], ['Dôvod', 'Reason'], ['Naposledy', 'Last']].forEach(function (h) {
        var th = document.createElement('th');
        setI18n(th, h[0], h[1]);
        head.appendChild(th);
      });
      table.appendChild(head);
      senders.forEach(function (s) {
        var tr = document.createElement('tr');
        var reasons = Object.keys(s.reasons || {}).map(function (code) {
          var r = INBOUND_REASON_TEXT[code] || [code, code];
          return [r[0] + ' ' + s.reasons[code], r[1] + ' ' + s.reasons[code]];
        });
        var when = schedTimeBoth(s.last_at);
        [[s.sender_hash, s.sender_hash], [s.patient, s.patient], [String(s.mails), String(s.mails)],
          [String(s.files), String(s.files)],
          [reasons.map(function (r) { return r[0]; }).join(', '), reasons.map(function (r) { return r[1]; }).join(', ')],
          when].forEach(function (cell) {
          var td = document.createElement('td');
          setI18n(td, String(cell[0] || ''), String(cell[1] || ''));
          tr.appendChild(td);
        });
        table.appendChild(tr);
      });
      body.appendChild(table);
    }

    // ── Patient selector ────────────────────────────────────────────────

    var adminPatientIds = new Set();
    var isAdminMode = false;
    // #411: caller is global admin (DASHBOARD_ADMIN_EMAILS / static bearer).
    // Distinct from `isAdminMode` which means "current selected patient is one
    // I'm admin-viewing read-only". The leaderboard widget on the analytics
    // panel hides itself unless this is true.
    var isGlobalAdmin = false;

    function patientLabel(p) {
      return loc(p, 'display_name') + (p.is_active ? '' : (dashLang === 'sk' ? ' (archivovaný)' : ' (archived)'));
    }

    /** After a language switch: the names in the patient selector follow it. */
    function relabelPatients() {
      var select = document.getElementById('patient-select');
      if (!select) return;
      Array.prototype.forEach.call(select.options, function(opt) {
        var p = patientsCache.find(function(x) { return x.patient_id === opt.value; });
        if (p) opt.textContent = patientLabel(p);
      });
      updatePatientDiagnosis();
    }

    async function loadPatients() {
      try {
        var data;
        if (DEMO) {
          data = await apiFetch('/api/patients');
        } else {
          var resp = await fetch('/api/patients', {
            headers: { 'Authorization': authHeader() }
          });
          if (!resp.ok) return;
          data = await resp.json();
        }

        // Handle both new format {own, admin} and legacy array format
        var ownPatients = data.own || data;
        var adminPatients = data.admin || [];
        var isAdmin = data.is_admin || false;
        isGlobalAdmin = isAdmin;

        // Build combined cache (own + admin)
        patientsCache = ownPatients.concat(adminPatients);
        adminPatientIds = new Set(adminPatients.map(function(p) { return p.patient_id; }));

        var select = document.getElementById('patient-select');
        clearEl(select);
        if (patientsCache.length === 0) {
          var emptyOpt = document.createElement('option');
          emptyOpt.value = '';
          emptyOpt.textContent = dashLang === 'sk' ? 'Žiadny pacient' : 'No patients';
          select.appendChild(emptyOpt);
          return;
        }

        // Render own patients
        ownPatients.forEach(function(p) {
          var opt = document.createElement('option');
          opt.value = p.patient_id;
          opt.textContent = patientLabel(p);
          select.appendChild(opt);
        });

        // Render admin separator + admin patients (read-only)
        if (isAdmin && adminPatients.length > 0) {
          var sep = document.createElement('option');
          sep.disabled = true;
          sep.textContent = dashLang === 'sk' ? '── Admin (len na čítanie) ──' : '── Admin (read-only) ──';
          select.appendChild(sep);
          adminPatients.forEach(function(p) {
            var opt = document.createElement('option');
            opt.value = p.patient_id;
            opt.textContent = patientLabel(p);
            select.appendChild(opt);
          });
        }

        // Restore selection from localStorage or default to first
        if (currentPatientId && patientsCache.some(function(p) {
          return p.patient_id === currentPatientId;
        })) {
          select.value = currentPatientId;
        } else {
          currentPatientId = patientsCache.length > 0 ? patientsCache[0].patient_id : '';
          select.value = currentPatientId;
          localStorage.setItem('oncofiles_patient_id', currentPatientId);
        }
        updateAdminMode();
        updatePatientDiagnosis();
        updateArchiveButton();
      } catch (e) {
        console.warn('Failed to load patients:', e);
      }
    }

    function updateAdminMode() {
      isAdminMode = adminPatientIds.has(currentPatientId);
      // Show/hide admin read-only banner
      var banner = document.getElementById('admin-readonly-banner');
      if (banner) banner.style.display = isAdminMode ? 'block' : 'none';
      // Disable modify controls in admin mode
      var archiveBtn = document.getElementById('archive-patient-btn');
      if (archiveBtn) archiveBtn.style.display = isAdminMode ? 'none' : '';
      var addPatientBtn = document.querySelector('.add-patient-btn');
      // Restore it when admin mode ends (#824), same as the archive button.
      if (addPatientBtn) addPatientBtn.style.display = isAdminMode ? 'none' : '';
      // #921: an admin's read-only view cannot add documents (the server refuses too).
      var uploadBtn = document.getElementById('upload-btn');
      if (uploadBtn) uploadBtn.hidden = isAdminMode;
      if (isAdminMode) closeUploadPanel();
      // #950: the document e-mail address is the caregiver's (a write credential).
      if (isAdminMode) { hideInboundCard(); inboundFor = null; }
    }

    function switchPatient(patientId) {
      currentPatientId = patientId;
      localStorage.setItem('oncofiles_patient_id', patientId);
      updateAdminMode();
      updatePatientDiagnosis();
      updateArchiveButton();
      clearAllPatientData();
      // Tile filter, search and open row belong to the previous patient: a
      // "needs attention" filter left over showed an empty list for a patient
      // with none (prod click-through, #917).
      resetDocView();
      loadGermlineBanner();
      refresh();
    }

    // Germline risk banner — #385 Session 3. Hidden unless patient has
    // germline_findings (BRCA1/2, Lynch, Li-Fraumeni etc.). Keeps the red
    // banner narrow: gene names + classification only; full details live in
    // the hereditary-genetics document category.
    async function loadGermlineBanner() {
      var banner = document.getElementById('germline-banner');
      var list = document.getElementById('germline-findings-list');
      if (DEMO || !banner || !list || !currentPatientId) { if (banner) banner.style.display = 'none'; return; }
      try {
        var resp = await fetch('/api/patient-context?patient_id=' + encodeURIComponent(currentPatientId), {
          headers: { 'Authorization': authHeader() }
        });
        if (!resp.ok) { banner.style.display = 'none'; return; }
        var ctx = await resp.json();
        var findings = ctx && ctx.germline_findings;
        if (!findings || Object.keys(findings).length === 0) { banner.style.display = 'none'; return; }
        // Build DOM via textContent only — germline values are user-supplied
        // through update_patient_context and must never be interpolated as HTML.
        while (list.firstChild) list.removeChild(list.firstChild);
        Object.keys(findings).sort().forEach(function(gene) {
          var f = findings[gene] || {};
          if (typeof f !== 'object') return;
          var span = document.createElement('span');
          span.style.display = 'inline-block';
          span.style.marginRight = '0.85rem';
          var strong = document.createElement('strong');
          var text = String(gene);
          if (f.classification) text += ' (' + String(f.classification) + ')';
          if (f.variant) text += ' ' + String(f.variant);
          if (f.test_lab) text += ' · ' + String(f.test_lab);
          strong.textContent = text;
          span.appendChild(strong);
          list.appendChild(span);
        });
        banner.style.display = 'block';
        if (typeof applyDashLang === 'function') applyDashLang();
      } catch (e) {
        banner.style.display = 'none';
      }
    }

    function updateArchiveButton() {
      var btn = document.getElementById('archive-patient-btn');
      if (!btn || !patientsCache) return;
      var p = patientsCache.find(function(x) { return x.patient_id === currentPatientId; });
      if (!p) return;
      if (p.is_active) {
        btn.textContent = dashLang === 'sk' ? 'Archivovať' : 'Archive';
        btn.setAttribute('data-sk', 'Archivovať');
        btn.setAttribute('data-en', 'Archive');
        btn.classList.remove('restore');
      } else {
        btn.textContent = dashLang === 'sk' ? 'Obnoviť' : 'Restore';
        btn.setAttribute('data-sk', 'Obnoviť');
        btn.setAttribute('data-en', 'Restore');
        btn.classList.add('restore');
      }
    }

    var archiveArmed = null;

    async function togglePatientArchive() {
      if (!currentPatientId) return;
      var p = patientsCache.find(function(x) { return x.patient_id === currentPatientId; });
      if (!p) return;
      var btn = document.getElementById('archive-patient-btn');
      var newActive = !p.is_active;
      // Two-step confirm without a browser dialog: the first click arms the
      // button for a few seconds, the second one does it.
      if (archiveArmed !== currentPatientId) {
        archiveArmed = currentPatientId;
        if (btn) {
          btn.classList.add('armed');
          setI18n(btn, newActive ? 'Naozaj obnoviť? Kliknite znova' : 'Naozaj archivovať? Kliknite znova',
            newActive ? 'Really restore? Click again' : 'Really archive? Click again');
        }
        setTimeout(function() {
          if (archiveArmed === null) return;
          archiveArmed = null;
          if (btn) btn.classList.remove('armed');
          updateArchiveButton();
        }, 6000);
        return;
      }
      archiveArmed = null;
      if (btn) btn.classList.remove('armed');
      try {
        var resp = await fetch('/api/patients/' + currentPatientId, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'Authorization': authHeader() },
          body: JSON.stringify({ is_active: newActive })
        });
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        await loadPatients();
        updateArchiveButton();
      } catch (e) {
        console.error('Archive/restore failed:', e);
        updateArchiveButton();
        showError(dashLang === 'sk' ? 'Stav pacienta sa nepodarilo zmeniť.' : 'The patient status could not be changed.');
      }
    }

    function clearAllPatientData() {
      // Clear the tiles and the list at once so the previous patient's data never lingers
      showSkeletons();
      docsData = [];
      docsSignature = '';
      openDoc = { id: null, data: null };
      activityData = null;
      hideInboundCard();
      inboundFor = null;
      tableMessage('Načítavam…', 'Loading…');
      var countEl = document.getElementById('docs-count');
      if (countEl) countEl.textContent = '';
      var note = document.getElementById('pending-note');
      if (note) note.hidden = true;
      var act = document.getElementById('activity-rows');
      if (act) clearEl(act);
      // Admin panels show per-patient numbers too
      ['cost-leaderboard-body', 'prompt-log-rows', 'prompt-stats'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) clearEl(el);
      });
    }

    function updatePatientDiagnosis() {
      var el = document.getElementById('patient-diagnosis');
      var p = patientsCache.find(function(p) {
        return p.patient_id === currentPatientId;
      });
      el.textContent = p ? loc(p, 'diagnosis_summary') : '';
      el.title = el.textContent;
    }

    // ── Onboarding wizard ───────────────────────────────────────────────

    var wizardPatientId = '';
    var wizardPatientUuid = '';  // UUID from POST /api/patients; token rows key on it
    var wizardBearerToken = '';
    var wizardCurrentStep = 1;
    var wizardPatientType = '';  // oncology | general, '' when unknown (#853 confirmation text)
    var wizardPatientName = '';  // display name, for the default new folder name

    // Step-2 bearer-token block. A token is opt-in (#824): nothing is minted
    // until the user clicks "Generate token".
    function resetWizardTokenSection(visible) {
      var section = document.getElementById('wiz-token-section');
      if (section) section.style.display = visible ? '' : 'none';
      var box = document.getElementById('wiz-token-display');
      if (box) { box.textContent = ''; box.style.display = 'none'; }
      ['wiz-token-warning', 'wiz-token-error'].forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.style.display = 'none';
      });
      var btn = document.getElementById('wiz-token-gen');
      if (btn) { btn.style.display = ''; btn.disabled = false; }
    }

    // Shown in step 2 when POST /api/patients reports context_saved: false:
    // the patient exists, but type / birth date were not stored (#824).
    // The text is static markup (data-sk / data-en); only visibility changes.
    function setWizardContextWarning(visible) {
      var el = document.getElementById('wiz-context-warning');
      if (!el) return;
      el.textContent = el.getAttribute(dashLang === 'sk' ? 'data-sk' : 'data-en');
      el.style.display = visible ? 'block' : 'none';
    }

    async function wizardGenerateToken() {
      var btn = document.getElementById('wiz-token-gen');
      var errEl = document.getElementById('wiz-token-error');
      if (!wizardPatientUuid || wizardBearerToken) return;
      errEl.style.display = 'none';
      btn.disabled = true;
      try {
        var resp = await fetch('/api/patient-tokens', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ patient_id: wizardPatientUuid, label: 'wizard' })
        });
        var data = await resp.json();
        if (!resp.ok || !data.bearer_token) {
          errEl.textContent = (dashLang === 'sk' ? 'Token sa nepodarilo vytvoriť: ' : 'Could not create the token: ') + (data.error || resp.status);
          errEl.style.display = 'block';
          btn.disabled = false;
          return;
        }
        wizardBearerToken = data.bearer_token;
        var box = document.getElementById('wiz-token-display');
        box.textContent = wizardBearerToken;
        box.style.display = '';
        document.getElementById('wiz-token-warning').style.display = '';
        btn.style.display = 'none';
      } catch (e) {
        errEl.textContent = (dashLang === 'sk' ? 'Sieťová chyba: ' : 'Network error: ') + e.message;
        errEl.style.display = 'block';
        btn.disabled = false;
      }
    }

    function toggleIntegrationPanel() {
      var p = document.getElementById('integration-popover');
      if (!p) return;
      p.style.display = p.style.display === 'block' ? 'none' : 'block';
    }
    // Close popover on outside click
    document.addEventListener('click', function(e) {
      var p = document.getElementById('integration-popover');
      if (p && p.style.display === 'block' && !e.target.closest('#svc-badges')) {
        p.style.display = 'none';
      }
    });

    function showGdriveSetup() {
      // Open wizard Step 2 directly for the current patient (skip patient creation)
      var pid = document.getElementById('patient-select').value;
      if (!pid) return;
      if (onboardingBlocked()) { showOnboardingClosed(null); return; }
      wizardPatientId = pid;
      // #835: the select holds the patient's UUID; keep it so "Go to Dashboard"
      // re-selects this patient by UUID, never by slug.
      wizardPatientUuid = pid;
      wizardBearerToken = '';
      wizardCurrentStep = 2;
      var current = patientsCache.find(function(x) { return x.patient_id === pid; });
      wizardPatientName = (current && current.display_name) || '';
      wizardPatientType = (current && current.patient_type) || '';
      document.getElementById('wiz-folder-link').value = '';  // a pasted link wins, so never keep a stale one
      for (var i = 1; i <= 4; i++) {
        document.getElementById('wiz-step-' + i).style.display = i === 2 ? 'block' : 'none';
      }
      // Check if patient already has drive scope — skip to folder selection
      var gs = window._lastStatus && window._lastStatus.google_services;
      if (gs && gs.drive) {
        document.getElementById('wiz-step2-auth').style.display = 'none';
        document.getElementById('wiz-step2-folder').style.display = 'block';
        document.getElementById('wiz-step2-auth-done').style.display = 'block';
        var statusEl = document.getElementById('wiz-step2-status');
        if (statusEl) statusEl.style.display = 'none';
        wizardStep2LoadFolders();
      } else {
        document.getElementById('wiz-step2-auth').style.display = 'block';
        document.getElementById('wiz-step2-folder').style.display = 'none';
        document.getElementById('wiz-step2-auth-done').style.display = 'none';
        var statusEl = document.getElementById('wiz-step2-status');
        if (statusEl) statusEl.style.display = 'none';
      }
      // Hide the token display (not relevant for existing patient)
      resetWizardTokenSection(false);
      setWizardContextWarning(false);
      updateWizardDots();
      document.getElementById('wizard-overlay').classList.add('active');
      wizardScrollTop();
    }

    function showOnboarding() {
      if (onboardingBlocked()) { showOnboardingClosed(null); return; }
      wizardPatientId = '';
      wizardPatientUuid = '';
      wizardBearerToken = '';
      wizardCurrentStep = 1;
      wizardPatientType = '';
      wizardPatientName = '';
      resetWizardTokenSection(true);
      setWizardContextWarning(false);
      // Reset form fields
      document.getElementById('wiz-name').value = '';
      document.getElementById('wiz-id').value = '';
      document.getElementById('wiz-type').value = 'oncology';
      document.getElementById('wiz-dob').value = '';
      document.getElementById('wiz-diagnosis').value = '';
      document.getElementById('wiz-email').value = '';
      document.getElementById('wiz-lang').value = 'sk';
      document.getElementById('wiz-consent').checked = false;  // #857: consent is per patient
      document.getElementById('wiz-folder-link').value = '';  // a pasted link wins, so never keep a stale one
      document.getElementById('wiz-step1-error').style.display = 'none';
      // Show step 1, hide others
      for (var i = 1; i <= 4; i++) {
        document.getElementById('wiz-step-' + i).style.display = i === 1 ? 'block' : 'none';
      }
      updateWizardDots();
      document.getElementById('wizard-overlay').classList.add('active');
      wizardScrollTop();
    }

    // #910: a new step starts at its top, so the step indicator is never clipped.
    function wizardScrollTop() {
      var panel = document.querySelector('#wizard-overlay .wizard-panel');
      if (panel) panel.scrollTop = 0;
    }

    function hideOnboarding() {
      document.getElementById('wizard-overlay').classList.remove('active');
    }

    // #852: server-side onboarding gate. /api/onboarding-status tells this
    // caller the mode and whether it may create a patient / connect Drive /
    // set a folder; when not, a waitlist notice replaces the wizard. The
    // server enforces the same rule (403 onboarding_closed), this only spares
    // the user a failing wizard. Unknown (fetch failed) = let the server decide.
    var onboardingStatus = null;

    async function loadOnboardingStatus() {
      if (DEMO) return;
      try {
        var resp = await fetch('/api/onboarding-status', { headers: { 'Authorization': authHeader() } });
        if (resp.ok) onboardingStatus = await resp.json();
      } catch (e) { /* keep null: the server still enforces the gate */ }
    }

    function onboardingBlocked() {
      return !!(onboardingStatus && onboardingStatus.allowed === false);
    }

    // A refusal body from the server: {error, message_sk, message_en, waitlist_url}.
    function isGateRefusal(resp, data) {
      return resp.status === 403 && data &&
        (data.error === 'onboarding_closed' || data.error === 'legacy_sync_admin_only');
    }

    function gateRefusalText(data) {
      if (data && data.message_sk && data.message_en) {
        return dashLang === 'sk' ? data.message_sk : data.message_en;
      }
      return (data && data.error) || 'Error';
    }

    function showOnboardingClosed(data) {
      var text = document.getElementById('onboarding-closed-text');
      if (data && data.message_sk && data.message_en) {
        setI18n(text, data.message_sk, data.message_en);
      }
      var link = document.getElementById('onboarding-closed-waitlist');
      var url = (data && data.waitlist_url) || (onboardingStatus && onboardingStatus.waitlist_url);
      if (url && /^\/(?!\/)/.test(url)) link.setAttribute('href', url);  // same-origin path only
      hideOnboarding();
      document.getElementById('onboarding-closed-overlay').classList.add('active');
    }

    function hideOnboardingClosed() {
      document.getElementById('onboarding-closed-overlay').classList.remove('active');
    }

    function updateWizardDots() {
      for (var i = 1; i <= 4; i++) {
        var dot = document.getElementById('wiz-dot-' + i);
        dot.className = 'wizard-step-dot';
        if (i < wizardCurrentStep) dot.className += ' done';
        else if (i === wizardCurrentStep) dot.className += ' active';
      }
    }

    function wizardGoToStep(step) {
      for (var i = 1; i <= 4; i++) {
        document.getElementById('wiz-step-' + i).style.display = i === step ? 'block' : 'none';
      }
      wizardCurrentStep = step;
      updateWizardDots();
      wizardScrollTop();
    }

    // Auto-generate slug from display name
    document.addEventListener('DOMContentLoaded', function() {
      var nameInput = document.getElementById('wiz-name');
      var idInput = document.getElementById('wiz-id');
      if (nameInput) {
        nameInput.addEventListener('input', function() {
          if (!idInput.dataset.manual) {
            idInput.value = nameInput.value.toLowerCase()
              .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
              .replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
          }
        });
        idInput.addEventListener('input', function() {
          idInput.dataset.manual = 'true';
        });
      }
    });

    // The button carries data-sk / data-en; put its own label back in the active language.
    function wizardResetCreateBtn(btn) {
      btn.textContent = btn.getAttribute(dashLang === 'sk' ? 'data-sk' : 'data-en');
    }

    // #857: version of the privacy + terms text the consent checkbox refers to.
    // Must equal oncofiles.patient_consent.CONSENT_VERSION (a test keeps them equal).
    var CONSENT_VERSION = '2026-11-21';

    async function wizardStep1Submit() {
      var btn = document.getElementById('wiz-create-btn');
      var errEl = document.getElementById('wiz-step1-error');
      errEl.style.display = 'none';
      wizSetPlain(errEl, '');  // drop data-sk/en of an earlier bilingual error
      btn.disabled = true;
      btn.textContent = dashLang === 'sk' ? 'Vytváram…' : 'Creating…';

      var name = document.getElementById('wiz-name').value.trim();
      var pid = document.getElementById('wiz-id').value.trim().toLowerCase();
      if (!name || !pid) {
        wizSetBi(errEl, 'Meno a ID pacienta sú povinné.', 'Patient name and ID are required.');
        errEl.style.display = 'block';
        btn.disabled = false;
        wizardResetCreateBtn(btn);
        return;
      }
      // #853: the curated folder contract needs the birth date (birth-date guard).
      if (!document.getElementById('wiz-dob').value) {
        wizSetBi(errEl,
          'Dátum narodenia je povinný: Oncofiles podľa neho rozpozná, že dátum narodenia nie je dátum dokumentu.',
          'Date of birth is required: Oncofiles uses it so that a birth date is never filed as a document date.');
        errEl.style.display = 'block';
        btn.disabled = false;
        wizardResetCreateBtn(btn);
        return;
      }

      // #857: the server refuses a patient without this consent as well.
      if (!document.getElementById('wiz-consent').checked) {
        wizSetBi(errEl,
          'Potvrďte prosím súhlas so spracovaním zdravotných údajov tejto osoby.',
          'Please confirm consent to the processing of this person’s health data.');
        errEl.style.display = 'block';
        btn.disabled = false;
        wizardResetCreateBtn(btn);
        return;
      }

      var body = JSON.stringify({
        consent: true,
        consent_version: CONSENT_VERSION,
        patient_id: pid,
        display_name: name,
        patient_type: document.getElementById('wiz-type').value || 'oncology',
        date_of_birth: document.getElementById('wiz-dob').value || null,
        diagnosis_summary: document.getElementById('wiz-diagnosis').value.trim() || null,
        caregiver_email: document.getElementById('wiz-email').value.trim() || null,
        preferred_lang: document.getElementById('wiz-lang').value
      });
      async function doCreate() {
        return await fetch('/api/patients', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: body
        });
      }
      try {
        var resp = await doCreate();
        // Auto-retry once on transient server errors (503/500).
        if (resp.status === 503 || resp.status === 500) {
          btn.textContent = dashLang === 'sk' ? 'Pripájam databázu… skúšam znova' : 'Reconnecting database… retrying';
          await new Promise(function (r) { setTimeout(r, 3500); });
          resp = await doCreate();
        }
        var data = await resp.json();
        if (isGateRefusal(resp, data)) {
          btn.disabled = false;
          wizardResetCreateBtn(btn);
          showOnboardingClosed(data);
          return;
        }
        if (resp.status === 409) {
          errEl.textContent = dashLang === 'sk'
            ? 'Tento pacient už existuje. Zvoľte iné ID.'
            : 'This patient already exists — pick a different ID.';
          errEl.style.display = 'block';
          btn.disabled = false;
          wizardResetCreateBtn(btn);
          return;
        }
        if (resp.status === 400 && data.error_sk && data.error_en) {
          // Consent refusal (#857): bilingual message from the server.
          wizSetBi(errEl, data.error_sk, data.error_en);
          errEl.style.display = 'block';
          btn.disabled = false;
          wizardResetCreateBtn(btn);
          return;
        }
        if (resp.status === 400) {
          // Validation error (slug, patient type, birth date): show it as is.
          errEl.textContent = data.error || 'Invalid input';
          errEl.style.display = 'block';
          btn.disabled = false;
          wizardResetCreateBtn(btn);
          return;
        }
        if (!resp.ok) {
          errEl.textContent = dashLang === 'sk'
            ? 'Databáza je krátko nedostupná, prosím skúste znova o ~30s. (' + (data.error || resp.status) + ')'
            : 'Database briefly unavailable, please try again in ~30s. (' + (data.error || resp.status) + ')';
          errEl.style.display = 'block';
          btn.disabled = false;
          wizardResetCreateBtn(btn);
          return;
        }
        wizardPatientId = pid;
        // No token comes back from patient creation (#824); step 2 offers a
        // "Generate token" button instead.
        wizardPatientUuid = (data.patient && data.patient.patient_id) || '';
        wizardPatientType = (data.patient && data.patient.patient_type) || '';
        wizardPatientName = name;
        wizardBearerToken = '';
        resetWizardTokenSection(true);
        setWizardContextWarning(data.context_saved === false);
        wizardGoToStep(2);
      } catch (e) {
        errEl.textContent = (dashLang === 'sk' ? 'Sieťová chyba: ' : 'Network error: ') + e.message;
        errEl.style.display = 'block';
      }
      btn.disabled = false;
      wizardResetCreateBtn(btn);
    }

    function wizardStep2Auth() {
      var url = '/oauth/authorize/drive?patient_id=' + encodeURIComponent(wizardPatientId);
      window.open(url, '_blank');
      var statusEl = document.getElementById('wiz-step2-status');
      wizSetBi(statusEl,
        'Dokončite autorizáciu v novej karte a potom kliknite nižšie.',
        'Complete the authorization in the new tab, then click below.');
      statusEl.style.display = 'block';
      document.getElementById('wiz-step2-auth-done').style.display = 'flex';
    }

    // #563 / #835: folder browser. The folder that gets connected is always the
    // one being browsed (shown in the breadcrumb); subfolders are a list of rows
    // that navigate into them, built with createElement + textContent only.
    var wizardFolderParent = 'root';
    var wizardFolderName = 'My Drive';
    var wizardFolderStack = [];
    var wizardFolderInfo = null;     // "current" from /api/gdrive-folders
    var wizardFolderNewMode = false;
    var wizardFolderPending = null;  // request body shown in the confirmation box
    var wizardFolderLoading = false;

    // Bilingual text on a dynamic element: keeps data-sk / data-en so that
    // applyDashLang() can switch it later.
    function wizSetBi(el, sk, en) {
      el.setAttribute('data-sk', sk);
      el.setAttribute('data-en', en);
      el.textContent = dashLang === 'sk' ? sk : en;
    }

    // Plain (not translated) text, e.g. a folder path or a server error.
    function wizSetPlain(el, text) {
      el.removeAttribute('data-sk');
      el.removeAttribute('data-en');
      el.textContent = text;
    }

    function wizShow(id, on) {
      var el = document.getElementById(id);
      if (el) el.style.display = on ? '' : 'none';
    }

    function wizardFolderStatus(sk, en, color) {
      var el = document.getElementById('wiz-step2-folder-status');
      el.style.color = color || 'var(--text-muted)';
      if (en === undefined) wizSetPlain(el, sk); else wizSetBi(el, sk, en);
      el.style.display = 'block';
    }

    async function wizardStep2LoadFolders(parentId, pushEntry) {
      if (wizardFolderLoading) return;
      document.getElementById('wiz-step2-auth').style.display = 'none';
      document.getElementById('wiz-step2-folder').style.display = 'block';
      var parent = parentId || 'root';
      var statusEl = document.getElementById('wiz-step2-folder-status');
      var list = document.getElementById('wiz-folder-list');
      wizardFolderLoading = true;
      list.setAttribute('aria-busy', 'true');
      wizardFolderStatus('Načítavam priečinky…', 'Loading folders…');

      try {
        var resp = await fetch(
          '/api/gdrive-folders?patient_id=' + encodeURIComponent(wizardPatientId) +
            '&parent=' + encodeURIComponent(parent),
          { headers: { 'Authorization': authHeader() } }
        );
        if (!resp.ok) {
          var err = await resp.json();
          if (err.error) wizardFolderStatus(err.error);
          else wizardFolderStatus('Priečinky sa nepodarilo načítať.', 'Failed to load folders.');
          return;
        }
        var data = await resp.json();
        if (!parentId) wizardFolderStack = [];  // fresh entry: reset breadcrumb
        if (pushEntry) wizardFolderStack.push(pushEntry);  // only after a successful load
        wizardFolderParent = data.parent_id || parent;
        wizardFolderName = data.parent_name || 'My Drive';
        wizardFolderInfo = data.current || null;
        document.getElementById('wiz-folder-path').textContent =
          wizardFolderStack.map(function(f) { return f.name; }).concat([wizardFolderName]).join(' / ');
        document.getElementById('wiz-folder-up').hidden = wizardFolderParent === 'root';

        // Subfolders only: the browsed folder itself is the breadcrumb, never a row.
        clearEl(list);
        var folders = data.folders || [];
        folders.forEach(function(f) {
          var li = document.createElement('li');
          var row = document.createElement('button');
          row.type = 'button';
          row.className = 'wizard-folder-row';
          row.dataset.action = 'wizardFolderOpen|@this';
          row.dataset.folderId = f.id;
          row.dataset.folderName = f.name;
          var name = document.createElement('span');
          name.className = 'wizard-folder-row-name';
          name.textContent = f.name;
          var open = document.createElement('span');
          open.className = 'wizard-folder-row-open';
          wizSetBi(open, 'Otvoriť ▸', 'Open ▸');
          row.appendChild(name);
          row.appendChild(open);
          li.appendChild(row);
          list.appendChild(li);
        });
        wizShow('wiz-folder-list-label', folders.length > 0);
        wizShow('wiz-folder-empty', folders.length === 0);

        var patientName = wizardPatientName || document.getElementById('wiz-name').value.trim();
        document.getElementById('wiz-folder-name').value = 'Oncofiles - ' + patientName;
        statusEl.style.display = 'none';
        wizardFolderNewMode = false;
        wizardFolderConfirmBack();
      } catch (e) {
        wizardFolderStatus((dashLang === 'sk' ? 'Chyba: ' : 'Error: ') + e.message);
      } finally {
        wizardFolderLoading = false;
        list.removeAttribute('aria-busy');
      }
    }

    function wizardFolderOpen(row) {
      if (!row || !row.dataset.folderId) return;
      wizardStep2LoadFolders(row.dataset.folderId, { id: wizardFolderParent, name: wizardFolderName });
    }

    function wizardFolderUp() {
      if (wizardFolderLoading) return;
      var prev = wizardFolderStack.pop();
      // Going up to My Drive root passes no id, which also resets the stack.
      wizardStep2LoadFolders(prev && prev.id !== 'root' ? prev.id : undefined);
    }

    function wizardFolderNewToggle() {
      wizardFolderNewMode = !wizardFolderNewMode;
      wizardFolderTargetUpdate();
      if (wizardFolderNewMode) document.getElementById('wiz-folder-name').focus();
    }

    // Says in plain words what the primary button will connect.
    function wizardFolderTargetUpdate() {
      var info = wizardFolderInfo || {};
      var link = document.getElementById('wiz-folder-link').value.trim();
      var atRoot = wizardFolderParent === 'root';
      var path = document.getElementById('wiz-folder-path').textContent;
      var label = document.getElementById('wiz-folder-target-label');
      var target = document.getElementById('wiz-folder-target-path');
      var canConnect = true;
      if (info.curated) wizardFolderNewMode = false;  // nothing is created inside a README folder
      if (link) {
        wizSetBi(label, 'Pripojí sa priečinok z vloženého odkazu.', 'The folder from the pasted link will be connected.');
        target.textContent = '';
      } else if (wizardFolderNewMode) {
        wizSetBi(label, 'Vytvorí a pripojí sa nový priečinok:', 'A new folder will be created and connected:');
        target.textContent = path + ' / ' + (document.getElementById('wiz-folder-name').value.trim() || 'Oncofiles');
      } else if (atRoot) {
        wizSetBi(label, 'Najprv otvorte priečinok. Samotný My Drive sa pripojiť nedá.', 'Open a folder first. My Drive itself cannot be connected.');
        target.textContent = '';
        canConnect = false;
      } else {
        wizSetBi(label, 'Pripojí sa tento priečinok:', 'Connect this folder:');
        target.textContent = path;
      }
      document.getElementById('wiz-folder-btn').disabled = !canConnect;
      wizShow('wiz-folder-new', wizardFolderNewMode);
      wizShow('wiz-folder-new-toggle', !info.curated);
      var toggle = document.getElementById('wiz-folder-new-toggle');
      if (wizardFolderNewMode) wizSetBi(toggle, '× Nevytvárať nový priečinok', '× Do not create a new folder');
      else wizSetBi(toggle, '+ Vytvoriť tu nový priečinok', '+ Create a new folder here');
      wizShow('wiz-folder-curated-note', !!info.curated && !link);
    }

    // In-wizard box (not a browser dialog). `info` is either "current" from
    // /api/gdrive-folders, a 409 body from the server (container_folder or
    // bootstrap_refused, #853) or a refusal built here. Variants:
    //  - hard: a subfolder is another patient's README folder -> "Back" only;
    //  - tooMany: too many subfolders to check them all -> "Back" only;
    //  - refused: the folder cannot get a README contract (has subfolders, no
    //    birth date, another patient's folder, ...) -> "Back" only;
    //  - retry: the check could not run -> "Back" + "Try again";
    //  - otherwise the confirmation before connecting (#835): says what will
    //    be written where -> "Back" + "Set up and connect" / "Connect".
    // The first four are refusals the server enforces; nothing the wizard
    // sends can lift them.
    function wizardFolderAskConfirm(body, info) {
      wizardFolderPending = body;
      var n = info.subfolder_count || 0;
      var text = document.getElementById('wiz-folder-confirm-text');
      var reason = info.reason || info.container_reason;
      var hard = reason === 'curated_children' || (info.curated_children || 0) > 0;
      var tooMany = !hard && reason === 'too_many_to_check';
      var retry = !hard && reason === 'check_failed';
      var refused = !hard && !tooMany && !retry && info.code === 'bootstrap_refused';
      var blocked = hard || tooMany || retry || !!reason;
      wizShow('wiz-folder-confirm-go', !blocked);
      wizShow('wiz-folder-confirm-retry', retry);
      // #853: no birth date in the patient context -> ask for it here; the
      // server validates and stores it, then sets the folder up.
      wizShow('wiz-folder-dob', refused && reason === 'missing_dob');
      if (hard) {
        wizSetBi(text,
          'Tento priečinok sa nedá pripojiť, pretože obsahuje priečinky iných pacientov (podpriečinky s vlastným kontraktom README). Vráťte sa a otvorte priečinok konkrétneho pacienta.',
          'This folder cannot be connected because it contains other patients\' folders (subfolders with their own README contract). Go back and open the folder of the patient you mean.');
      } else if (tooMany) {
        wizSetBi(text,
          'Tento priečinok má príliš veľa podpriečinkov na bezpečnú kontrolu. Otvorte priečinok konkrétneho pacienta alebo vložte odkaz naň.',
          'This folder has too many subfolders to check safely. Open the folder of the patient you mean, or paste its link.');
      } else if (refused) {
        wizSetBi(text, info.error_sk || info.error || '', info.error || '');
      } else if (blocked) {
        wizSetBi(text,
          'Tento priečinok sa teraz nepodarilo skontrolovať. Skúste to o chvíľu znova.',
          'Could not check this folder right now. Try again in a moment.');
      } else {
        wizardFolderConfirmText(text, body, info);
      }
      wizSetBi(document.getElementById('wiz-folder-confirm-path-label'),
        blocked ? 'Priečinok:' : 'Pripojí sa priečinok:',
        blocked ? 'Folder:' : 'Folder that will be connected:');
      var pathEl = document.getElementById('wiz-folder-confirm-path');
      if (body.folder_id && body.folder_id !== wizardFolderParent) {
        wizSetBi(pathEl, 'priečinok z vloženého odkazu', 'the folder from the pasted link');
      } else if (body.folder_name) {
        wizSetPlain(pathEl, document.getElementById('wiz-folder-path').textContent + ' / ' + body.folder_name);
      } else {
        wizSetPlain(pathEl, document.getElementById('wiz-folder-path').textContent);
      }
      document.getElementById('wiz-folder-confirm-go').disabled = false;
      document.getElementById('wiz-step2-folder-status').style.display = 'none';
      wizShow('wiz-folder-main', false);
      wizShow('wiz-folder-confirm', true);
      document.getElementById('wiz-folder-confirm').scrollIntoView({ block: 'nearest' });
    }

    // #853 / #835: what connecting will write, before anything is written.
    function wizardFolderConfirmText(text, body, info) {
      var go = document.getElementById('wiz-folder-confirm-go');
      var t = wizardPatientType;
      var setSk = 'Oncofiles do neho zapíše kontrakt README.md' +
        (t === 'general' ? ' (typ všeobecné zdravie)' : t === 'oncology' ? ' (typ onkologický)' : '') + ', súbory _index.json, _changelog.md, _actions.jsonl a pevnú sadu priečinkov (00-inbox, labs, report, …). Nič sa nezmaže ani nepresunie. Potom ho spracúva kurátorovaná pipeline (lokálne OCR).';
      var setEn = 'Oncofiles writes a README.md contract' +
        (t === 'general' ? ' (general health type)' : t === 'oncology' ? ' (oncology type)' : '') + ', _index.json, _changelog.md, _actions.jsonl and a fixed set of folders (00-inbox, labs, report, …) into it. Nothing is deleted or moved. The curated pipeline (local OCR) then processes it.';
      if (info.curated) {
        wizSetBi(text,
          'Tento priečinok má vlastný kontrakt README; pripojením sa v ňom nič nevytvorí ani nepresunie.',
          'This folder has its own README contract; nothing will be created or moved by connecting it.');
        wizSetBi(go, 'Pripojiť', 'Connect');
      } else if (body.folder_name) {
        wizSetBi(text, 'Vytvorí sa nový priečinok. ' + setSk, 'A new folder is created. ' + setEn);
        wizSetBi(go, 'Vytvoriť a pripojiť', 'Create and connect');
      } else if (body.folder_id && body.folder_id !== wizardFolderParent) {
        wizSetBi(text,
          'Ak priečinok z odkazu ešte nemá kontrakt README: ' + setSk + ' Musí byť bez podpriečinkov; súbory v ňom ostanú na mieste a pipeline ich zatriedi pri prvom spracovaní.',
          'If the linked folder has no README contract yet: ' + setEn + ' It must have no subfolders; files in it stay where they are and the pipeline files them on its first run.');
        wizSetBi(go, 'Nastaviť a pripojiť', 'Set up and connect');
      } else {
        wizSetBi(text,
          setSk + ' Súbory, ktoré už v priečinku sú, ostanú na mieste a pipeline ich zatriedi pri prvom spracovaní.',
          setEn + ' Files already in the folder stay where they are; the pipeline files them on its first run.');
        wizSetBi(go, 'Nastaviť a pripojiť', 'Set up and connect');
      }
    }

    // #853: send the same request again with the birth date typed in the box.
    // The date is data, not a flag: the server still runs every check.
    function wizardFolderDobGo() {
      if (!wizardFolderPending) return;
      var dob = document.getElementById('wiz-folder-dob-input').value;
      if (!dob) {
        wizSetBi(document.getElementById('wiz-folder-confirm-text'),
          'Zadajte dátum narodenia pacienta.', 'Enter the patient\'s date of birth.');
        return;
      }
      wizardFolderSend(Object.assign({}, wizardFolderPending, { date_of_birth: dob }));
    }

    function wizardFolderConfirmBack() {
      wizardFolderPending = null;
      wizShow('wiz-folder-dob', false);
      wizShow('wiz-folder-confirm', false);
      wizShow('wiz-folder-main', true);
      wizardFolderTargetUpdate();
    }

    // "Try again" of the check_failed variant: run the check once more. A
    // pasted link is re-sent (the server checks it); a browsed folder is
    // re-loaded, and the box comes back if its check fails again.
    async function wizardFolderConfirmRetry() {
      var body = wizardFolderPending;
      if (!body) return;
      if (body.folder_id && body.folder_id !== wizardFolderParent) {
        wizardFolderSend(body);
        return;
      }
      var parent = wizardFolderParent;
      await wizardStep2LoadFolders(parent === 'root' ? undefined : parent);
      var info = wizardFolderInfo || {};
      if (wizardFolderParent === parent && info.container_reason === 'check_failed') {
        wizardFolderAskConfirm(body, info);
      }
    }

    function wizardFolderConfirmGo() {
      if (!wizardFolderPending) return;
      // The user confirmed what will be written. No flag is sent: the server
      // decides on its own whether the folder may be connected.
      wizardFolderSend(wizardFolderPending);
    }

    function wizardStep2SetFolder() {
      var linkVal = document.getElementById('wiz-folder-link').value.trim();
      var body = { patient_id: wizardPatientId };
      var info = {};
      if (linkVal) {
        body.folder_id = linkVal;  // server parses links and bare ids (#563)
      } else if (wizardFolderNewMode) {
        body.folder_name = document.getElementById('wiz-folder-name').value.trim() || 'Oncofiles';
        body.parent_id = wizardFolderParent;
      } else {
        if (wizardFolderParent === 'root') return;
        body.folder_id = wizardFolderParent;
        info = wizardFolderInfo || {};
        if (!info.curated && !info.container_reason && info.subfolder_count > 0) {
          // #853: a README contract is only written into a folder without
          // subfolders (the server refuses the same way).
          info = {
            code: 'bootstrap_refused',
            reason: 'has_subfolders',
            subfolder_count: info.subfolder_count,
            error: 'This folder has subfolders. Oncofiles sets up its folders only in a folder without subfolders: create a new folder here (files can be moved into it later), or open one of the subfolders.',
            error_sk: 'Tento priečinok má podpriečinky. Oncofiles si svoje priečinky vytvorí iba v priečinku bez podpriečinkov: vytvorte tu nový priečinok (súbory doň môžete presunúť neskôr) alebo otvorte niektorý z podpriečinkov.'
          };
        }
      }
      // Always ask before connecting (#835): the box says what will be written.
      wizardFolderAskConfirm(body, info);
    }

    async function wizardFolderSend(body) {
      var btn = document.getElementById('wiz-folder-btn');
      var goBtn = document.getElementById('wiz-folder-confirm-go');
      var retryBtn = document.getElementById('wiz-folder-confirm-retry');
      btn.disabled = true;
      goBtn.disabled = true;
      retryBtn.disabled = true;
      wizShow('wiz-folder-confirm', false);
      wizShow('wiz-folder-main', true);
      wizardFolderStatus('Pripájam a nastavujem priečinok… (môže to trvať pol minúty)', 'Connecting and setting up the folder… (this can take half a minute)');
      function unlock() {
        goBtn.disabled = false;
        retryBtn.disabled = false;
        wizardFolderTargetUpdate();  // re-derives the primary button's state
      }

      try {
        var resp = await fetch('/api/gdrive-set-folder', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        var data = await resp.json();
        if (isGateRefusal(resp, data)) {
          unlock();
          showOnboardingClosed(data);
          return;
        }
        if (resp.status === 409 && (data.code === 'container_folder' || data.code === 'bootstrap_refused')) {
          // Server-side refusal (e.g. a pasted link the browser knows nothing about).
          wizardFolderAskConfirm(body, data);
          unlock();
          return;
        }
        if (!resp.ok) {
          if (data.error) wizardFolderStatus(data.error_sk || data.error, data.error, 'var(--red)');
          else wizardFolderStatus('Priečinok sa nepodarilo pripojiť.', 'Failed to connect the folder.', 'var(--red)');
          unlock();
          return;
        }
        if (data.bootstrapped) {
          wizardFolderStatus(
            'Priečinok je nastavený: kontrakt README.md a ' + data.folders_created + ' priečinkov.',
            'Folder set up: README.md contract and ' + data.folders_created + ' folders.',
            'var(--green)');
        } else if (data.curated) {
          wizardFolderStatus(
            'Priečinok pripojený. Má vlastný kontrakt README.md, preto sa nevytvorili žiadne priečinky.',
            'Folder connected. It has its own README.md contract, so no folders were created.',
            'var(--green)');
        } else {
          wizardFolderStatus(
            'Priečinok je pripravený. Vytvorené priečinky kategórií: ' + data.subfolders_created + '.',
            'Folder ready! ' + data.subfolders_created + ' category folders created.',
            'var(--green)');
        }
        wizardSetStep3Variant(data);
        setTimeout(function() { wizardGoToStep(3); }, 1000);
      } catch (e) {
        wizardFolderStatus((dashLang === 'sk' ? 'Chyba: ' : 'Error: ') + e.message, undefined, 'var(--red)');
        unlock();
      }
    }

    // Step 3 (#835): a curated folder is skipped by the legacy first sync, so it
    // gets its own text and a single "Continue" instead of "Start First Sync".
    function wizardSetStep3Variant(data) {
      var curated = !!(data && data.curated);
      wizShow('wiz-step3-legacy', !curated);
      wizShow('wiz-step3-curated', curated);
      // curated_root === false: the folder only overlaps a README folder
      // (lies inside one, or was connected as a parent after confirmation).
      var isRoot = !data || data.curated_root !== false;
      wizShow('wiz-step3-curated-root', isRoot);
      wizShow('wiz-step3-curated-overlap', !isRoot);
      wizardPipelineNote(isRoot && data ? data.pipeline : null, data && data.loose_files);
    }

    // #835 / #853: which pipeline processes the folder, and when.
    function wizardPipelineNote(p, looseFiles) {
      var el = document.getElementById('wiz-step3-pipeline');
      if (!el) return;
      if (!p) { el.style.display = 'none'; return; }
      var poll = Math.max(1, Math.round((p.poll_s || 120) / 60));
      var settle = Math.max(1, Math.round((p.settle_s || 120) / 60));
      var sk, en;
      if (!p.processing_enabled) {
        sk = 'Spracovanie je teraz na serveri pozastavené. Priečinok je zaradený a spracuje sa, keď ho správca zapne.';
        en = 'Processing is paused on the server right now. The folder is enrolled and will be processed once the maintainer switches it on.';
      } else {
        sk = 'Spracovanie: kurátorovaná pipeline s lokálnym OCR. Server kontroluje priečinok každé ' + poll + ' min; nové súbory spracuje, keď sú ' + settle +
          ' min bez zmeny. Prvý beh je náhľad (nič nemení), potom najviac ' + (p.docs_per_run || 5) + ' dokumentov na beh. ';
        en = 'Processing: the curated pipeline with local OCR. The server checks the folder every ' + poll + ' min and processes new files once they have been unchanged for ' + settle +
          ' min. The first run is a preview (changes nothing), then up to ' + (p.docs_per_run || 5) + ' documents per run. ';
        // #865: what happens to a new institution/doctor follows CURATED_AUTO_VOCAB.
        if (p.auto_vocab) {
          sk += 'Nového lekára alebo inštitúciu vytlačenú v dokumente pipeline sama doplní do README; nejasné prípady počkajú v 00-inbox na kontrolu.';
          en += 'A new doctor or institution printed in a document is added to the README automatically; unclear cases wait in 00-inbox for review.';
        } else {
          sk += 'Dokument s novým lekárom alebo inštitúciou, ktoré ešte nie sú v README, počká v 00-inbox na kontrolu; po doplnení do README ho zatriedi ďalší beh.';
          en += 'A document with a new doctor or institution that is not in the README yet waits in 00-inbox for review; once it is added to the README, the next run files it.';
        }
      }
      if (looseFiles > 0) {
        sk += ' Súbory, ktoré už boli v priečinku (' + looseFiles + '), sa zatriedia pri prvých behoch.';
        en += ' Files that were already in the folder (' + looseFiles + ') are filed during the first runs.';
      }
      wizSetBi(el, sk, en);
      el.style.display = '';
    }

    async function wizardStep3Sync() {
      var btn = document.getElementById('wiz-sync-btn');
      var statusEl = document.getElementById('wiz-step3-status');
      btn.disabled = true;
      statusEl.textContent = 'Syncing...';
      statusEl.style.display = 'block';

      try {
        var resp = await fetch('/api/sync-trigger', {
          method: 'POST',
          headers: {
            'Authorization': authHeader(),
            'Content-Type': 'application/json'
          },
          body: JSON.stringify({ patient_id: wizardPatientId })
        });
        var data = await resp.json();
        if (!resp.ok) {
          statusEl.textContent = isGateRefusal(resp, data)
            ? gateRefusalText(data)
            : 'Sync issue: ' + (data.error || 'unknown error');
          statusEl.style.color = 'var(--amber)';
          btn.disabled = false;
          // Still allow proceeding
          setTimeout(function() { wizardGoToStep(4); showWizardSummary(null); }, 2000);
          return;
        }
        showWizardSummary(data.stats);
        wizardGoToStep(4);
      } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
        btn.disabled = false;
      }
    }


    // ── #573 Operations panel: curated-folder runs (admin only) ──────────
    var curatedOpsPoll = null;
    var curatedArmed = {};

    function coEl(tag, text, cls) {
      var e = document.createElement(tag);
      if (text !== undefined && text !== null) e.textContent = String(text);
      if (cls) e.className = cls;
      return e;
    }

    function coBilingual(tag, sk, en, cls) {
      var e = coEl(tag, sk, cls);
      e.setAttribute('data-sk', sk);
      e.setAttribute('data-en', en);
      return e;
    }

    async function coFetch(url, opts) {
      opts = opts || {};
      var headers = { 'Authorization': authHeader() };
      if (opts.body) headers['Content-Type'] = 'application/json';
      var resp = await fetch(url, { method: opts.method || 'GET', headers: headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
      var data = null;
      try { data = await resp.json(); } catch (e) { data = null; }
      return { ok: resp.ok, status: resp.status, data: data };
    }

    // Both languages at once (the admin panel was Slovak-only and showed UTC).
    function coWhen(value) {
      var d = parseTs(value);
      if (!d) return ['–', '–'];
      var opts = { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' };
      return [d.toLocaleString('sk-SK', opts), d.toLocaleString('en-GB', opts)];
    }

    function coRunLine(run) {
      if (!run) return ['Zatiaľ žiadny beh.', 'No run yet.'];
      var when = coWhen(run.finished_at || run.started_at);
      var trig = run.trigger ? ' (' + run.trigger + ')' : '';
      var msg = run.message ? ' · ' + run.message : '';
      return [
        'Posledný beh: ' + (run.dry_run ? 'náhľad' : 'naostro') + trig + ', ' + when[0] + ' — ' + run.status +
          ' · dokumenty ' + run.documents + ' (zaradené ' + run.filed + ', na kontrolu ' + run.review + ', nespracované ' + run.not_processed + ')' + msg,
        'Last run: ' + (run.dry_run ? 'preview' : 'real') + trig + ', ' + when[1] + ' — ' + run.status +
          ' · documents ' + run.documents + ' (filed ' + run.filed + ', for review ' + run.review + ', not processed ' + run.not_processed + ')' + msg
      ];
    }

    async function loadCuratedOps() {
      var section = document.getElementById('curated-ops-section');
      if (!section) return;
      var res = await coFetch('/api/admin/curated/overview');
      if (!res.ok || !res.data) { section.style.display = 'none'; return; }
      section.style.display = 'block';
      var n = res.data.nightly || {};
      var c = res.data.continuous || {};
      var note = document.getElementById('curated-ops-nightly');
      var noteText = n.enabled && c.enabled
        ? [('Priebežné spracovanie je zapnuté: server kontroluje priečinky každé ' + Math.round(c.poll_s / 60) + ' min a nové súbory spracuje do niekoľkých minút (po ' + c.chunk_docs + ' dokumentov na beh). '
            + (n.hour === null ? 'Plný beh každú hodinu.' : 'Plný nočný beh o ' + n.hour + ':00 (Bratislava).')),
           ('Continuous processing is on: the server checks the folders every ' + Math.round(c.poll_s / 60) + ' min and processes new files within minutes (' + c.chunk_docs + ' documents per run). '
            + (n.hour === null ? 'Full run every hour.' : 'Full nightly run at ' + n.hour + ':00 (Bratislava).'))]
        : n.enabled
        ? (n.hour === null
          ? ['Beh je zapnutý na serveri: každú hodinu najviac ' + n.max_runs + ' behov naostro (náhľad raz za 24 h) pre pacientov so zapnutým prepínačom.',
             'Runs are on: every hour at most ' + n.max_runs + ' real runs (a preview once per 24 h) for patients with the switch on.']
          : ['Nočný beh je zapnutý na serveri: každú noc o ' + n.hour + ':00 (Bratislava) náhľad a potom najviac ' + n.max_runs + ' behov naostro pre pacientov so zapnutým prepínačom.',
             'The nightly run is on: every night at ' + n.hour + ':00 (Bratislava) a preview, then at most ' + n.max_runs + ' real runs for patients with the switch on.'])
        : ['Nočný beh je na serveri vypnutý (CURATED_NIGHTLY_ENABLED). Prepínače pacientov sa uložia, ale nespustia sa.',
           'The nightly run is off on the server (CURATED_NIGHTLY_ENABLED). Patient switches are saved but do not start anything.'];
      setI18n(note, noteText[0], noteText[1]);
      var body = document.getElementById('curated-ops-body');
      clearEl(body);
      var anyRunning = false;
      (res.data.patients || []).forEach(function (p) {
        var card = coEl('div', null, 'curated-ops-card');
        card.appendChild(coEl('h3', p.name + ' (' + p.slug + ', ' + p.patient_type + ')'));
        card.appendChild(coBilingual('div',
          'Priečinok: ' + (p.folder_name || '—') + (p.drive_connected ? '' : ' · Drive nepripojený'),
          'Folder: ' + (p.folder_name || '—') + (p.drive_connected ? '' : ' · Drive not connected'), 'curated-ops-meta'));
        var runLine = p.running
          ? ['Práve beží: ' + (p.running.dry_run ? 'náhľad' : 'naostro') + ' (' + (p.running.trigger || '') + ')',
             'Running now: ' + (p.running.dry_run ? 'preview' : 'real') + ' (' + (p.running.trigger || '') + ')']
          : coRunLine(p.last_run);
        if (p.running) anyRunning = true;
        var st = coBilingual('div', runLine[0], runLine[1], 'curated-ops-meta');
        if (p.last_run && !p.running) st.classList.add(p.last_run.status === 'ok' ? 'curated-ops-status-ok' : 'curated-ops-status-bad');
        card.appendChild(st);
        var actions = coEl('div', null, 'curated-ops-actions');
        var prev = coBilingual('button', 'Náhľad (dry run)', 'Preview (dry run)');
        prev.setAttribute('data-action', 'curatedRun|' + p.patient_id + '|dry|@this');
        prev.disabled = !!p.running || !p.drive_connected;
        actions.appendChild(prev);
        var real = coBilingual('button', 'Spustiť naostro', 'Run for real');
        real.setAttribute('data-action', 'curatedRun|' + p.patient_id + '|real|@this');
        real.disabled = !!p.running || !!p.real_run_blocked || !p.drive_connected;
        if (p.real_run_blocked) real.title = p.real_run_blocked;
        actions.appendChild(real);
        var rep = coBilingual('button', 'Report', 'Report');
        rep.setAttribute('data-action', 'curatedReport|' + p.patient_id);
        actions.appendChild(rep);
        var lbl = coEl('label', null, 'curated-ops-meta');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = !!p.nightly_enabled;
        cb.setAttribute('data-change', 'curatedNightly|' + p.patient_id + '|@this');
        lbl.appendChild(cb);
        lbl.appendChild(coBilingual('span', ' Spracovanie zapnuté (priebežne + nočná kontrola)', ' Processing on (continuous + nightly check)'));
        actions.appendChild(lbl);
        card.appendChild(actions);
        body.appendChild(card);
      });
      if (typeof applyDashLang === 'function') applyDashLang();
      if (curatedOpsPoll) { clearTimeout(curatedOpsPoll); curatedOpsPoll = null; }
      if (anyRunning) curatedOpsPoll = setTimeout(loadCuratedOps, 30000);
    }

    async function curatedRun(pid, mode, btn) {
      var dry = mode !== 'real';
      if (!dry) {
        // Two-step confirm without a browser dialog: first click arms, second runs.
        if (!curatedArmed[pid]) {
          curatedArmed[pid] = true;
          btn.classList.add('armed');
          btn.textContent = 'Naozaj? Klikni znova (zapisuje do Drive)';
          setTimeout(function () { curatedArmed[pid] = false; loadCuratedOps(); }, 6000);
          return;
        }
        curatedArmed[pid] = false;
      }
      btn.disabled = true;
      var res = await coFetch('/api/admin/curated/run', { method: 'POST', body: { patient_id: pid, dry_run: dry } });
      var note = document.getElementById('curated-ops-nightly');
      if (!res.ok) note.textContent = 'Nespustené: ' + ((res.data && res.data.error) || ('HTTP ' + res.status));
      loadCuratedOps();
    }

    async function curatedNightly(pid, el) {
      await coFetch('/api/admin/curated/nightly', { method: 'POST', body: { patient_id: pid, enabled: !!el.checked } });
      loadCuratedOps();
    }

    async function curatedReport(pid) {
      var box = document.getElementById('curated-ops-report');
      clearEl(box);
      var res = await coFetch('/api/admin/curated/report?patient_id=' + encodeURIComponent(pid));
      if (!res.ok || !res.data || !res.data.run) { box.appendChild(coEl('p', 'Žiadny report.', 'curated-ops-note')); return; }
      var run = res.data.run, r = run.report || {};
      box.appendChild(coEl('h4', 'Report ' + (r.run_id || run.id) + ' — ' + (run.dry_run ? 'náhľad' : 'naostro') + ' — ' + run.status));
      if (r.message) box.appendChild(coEl('p', r.message, 'curated-ops-note'));
      if (r.gates && r.gates.length) {
        box.appendChild(coEl('h4', 'Kontroly'));
        var ul = coEl('ul');
        r.gates.forEach(function (g) { ul.appendChild(coEl('li', (g.ok ? '✓ ' : (g.blocking === false ? '⚠ ' : '✗ ')) + g.name + (g.detail ? ' — ' + g.detail : ''))); });
        box.appendChild(ul);
      }
      var docs = r.documents || [];
      box.appendChild(coEl('h4', 'Dokumenty (' + docs.length + ')'));
      if (docs.length) {
        var t = coEl('table'), hr = coEl('tr');
        ['Zdroj', 'Výsledok', 'Cieľ / dôvod'].forEach(function (h) { hr.appendChild(coEl('th', h)); });
        t.appendChild(hr);
        docs.forEach(function (d) {
          var tr = coEl('tr');
          tr.appendChild(coEl('td', d.source));
          tr.appendChild(coEl('td', d.result));
          var detail = d.to || (d.reasons ? d.reasons.join('; ') : '') || (d.proposal ? 'návrh: ' + d.proposal : '') || (d.survivor ? 'duplikát: ' + d.survivor : '');
          tr.appendChild(coEl('td', detail));
          t.appendChild(tr);
        });
        box.appendChild(t);
      }
      [['Upozornenia', r.flags], ['Čaká na kontrolu', r.awaiting_review], ['Nespracované (rozpočet)', r.not_processed]].forEach(function (pair) {
        if (pair[1] && pair[1].length) {
          box.appendChild(coEl('h4', pair[0] + ' (' + pair[1].length + ')'));
          var ul2 = coEl('ul');
          pair[1].forEach(function (x) { ul2.appendChild(coEl('li', x)); });
          box.appendChild(ul2);
        }
      });
    }

    // ── #573 Verify-only re-read and re-OCR console (admin only) ─────────
    // Counts only: the API never returns fact values, dates or evidence text.
    var RR_MAX = 10;
    var rrState = { pid: '', selected: {}, docs: {}, pending: null, poll: null, loaded: false };

    function rrMsg(el, sk, en) {
      el.textContent = dashLang === 'sk' ? sk : en;
      el.setAttribute('data-sk', sk);
      el.setAttribute('data-en', en);
    }

    function rrSelectedIds() {
      return Object.keys(rrState.selected).filter(function (k) { return rrState.selected[k]; }).map(Number);
    }

    function rrUpdateButtons(running) {
      var n = rrSelectedIds().length;
      var none = !rrState.pid;
      ['rr-dry-btn', 'rr-real-btn'].forEach(function (id) {
        var b = document.getElementById(id);
        if (b) b.disabled = !!running || none || n === 0 || n > RR_MAX;
      });
      // Only a re-OCR dry run may auto-pick documents; every real run needs a selection.
      var rd = document.getElementById('rr-reocr-dry-btn');
      if (rd) rd.disabled = !!running || none || n > RR_MAX;
      var rr = document.getElementById('rr-reocr-real-btn');
      if (rr) rr.disabled = !!running || none || n === 0 || n > RR_MAX;
    }

    async function loadRereadConsole() {
      var section = document.getElementById('reread-section');
      if (!section) return;
      if (rrState.loaded) return;  // the page refresh cycle must not wipe selections
      var res = await coFetch('/api/admin/curated/overview');
      if (!res.ok || !res.data) { section.style.display = 'none'; return; }
      rrState.loaded = true;
      section.style.display = 'block';
      var sel = document.getElementById('rr-patient');
      clearEl(sel);
      var blank = coBilingual('option', '— vyber pacienta —', '— pick a patient —');
      blank.value = '';
      sel.appendChild(blank);
      (res.data.patients || []).forEach(function (p) {
        var o = coEl('option', p.name + ' (' + p.slug + ')' + (p.drive_connected ? '' : ' · Drive ✗'));
        o.value = p.slug || p.patient_id;
        sel.appendChild(o);
      });
      rrUpdateButtons(false);
      if (typeof applyDashLang === 'function') applyDashLang();
    }

    async function rrSelectPatient(value) {
      rrState.pid = value || '';
      rrState.selected = {};
      rrState.docs = {};
      rrConfirmCancel();
      clearEl(document.getElementById('rr-candidates'));
      clearEl(document.getElementById('rr-status'));
      rrMsg(document.getElementById('rr-note'), '', '');
      if (!rrState.pid) { rrUpdateButtons(false); return; }
      await rrLoadCandidates();
      await rrLoadStatus();
    }

    async function rrLoadCandidates() {
      var box = document.getElementById('rr-candidates');
      clearEl(box);
      var res = await coFetch('/api/admin/reread-candidates?patient_slug=' + encodeURIComponent(rrState.pid));
      if (!res.ok || !res.data) {
        box.appendChild(coBilingual('p', 'Kandidátov sa nepodarilo načítať (HTTP ' + res.status + ').', 'Could not load candidates (HTTP ' + res.status + ').', 'curated-ops-note'));
        return;
      }
      var docs = res.data.documents || [];
      if (!docs.length) {
        box.appendChild(coBilingual('p', 'Žiadne dokumenty s neoverenými hodnotami z Cowork OCR.', 'No documents with unverified Cowork-OCR values.', 'curated-ops-note'));
        rrUpdateButtons(false);
        return;
      }
      box.appendChild(coBilingual('h4', 'Kandidáti (' + docs.length + ') — vyber najviac ' + RR_MAX, 'Candidates (' + docs.length + ') — pick at most ' + RR_MAX));
      var t = coEl('table'), hr = coEl('tr');
      hr.appendChild(coEl('th', ''));
      [['ID', 'ID'], ['Súbor', 'File'], ['Kategória', 'Category'], ['Neoverené (Cowork)', 'Unverified (Cowork)']].forEach(function (h) { hr.appendChild(coBilingual('th', h[0], h[1])); });
      t.appendChild(hr);
      docs.forEach(function (d) {
        rrState.docs[d.document_id] = d;
        var tr = coEl('tr');
        var td = coEl('td');
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.setAttribute('data-change', 'rrToggle|' + d.document_id + '|@this');
        cb.setAttribute('aria-label', 'doc ' + d.document_id);
        td.appendChild(cb);
        tr.appendChild(td);
        tr.appendChild(coEl('td', d.document_id, 'rr-num'));
        tr.appendChild(coEl('td', d.filename));
        tr.appendChild(coEl('td', d.category || '—'));
        tr.appendChild(coEl('td', d.cowork_unverified, 'rr-num'));
        t.appendChild(tr);
      });
      box.appendChild(t);
      rrUpdateButtons(false);
      if (typeof applyDashLang === 'function') applyDashLang();
    }

    function rrToggle(docId, el) {
      if (el.checked && rrSelectedIds().length >= RR_MAX) {
        el.checked = false;
        rrMsg(document.getElementById('rr-note'), 'Najviac ' + RR_MAX + ' dokumentov naraz.', 'At most ' + RR_MAX + ' documents at a time.');
        return;
      }
      rrState.selected[docId] = !!el.checked;
      rrConfirmCancel();
      rrUpdateButtons(false);
    }

    function rrStart(kind, mode) {
      if (!rrState.pid) return;
      var ids = rrSelectedIds();
      if (ids.length > RR_MAX) return;
      if (kind === 'reread' && ids.length === 0) return;
      if (mode !== 'real') { rrSubmit(kind, ids, true); return; }
      // Real store: explicit selection only (the server answers 400 otherwise),
      // then a typed confirmation built into the page (no window.confirm).
      if (ids.length === 0) return;
      var expected = ids.length;
      rrState.pending = { kind: kind, ids: ids, expected: expected };
      var box = document.getElementById('rr-confirm');
      var txt = document.getElementById('rr-confirm-text');
      if (kind === 'reread') {
        rrMsg(txt, 'Uložiť overenie pre ' + expected + ' dokumentov do databázy. Najprv over, že infra urobila čerstvý snapshot DB. Na potvrdenie napíš počet dokumentov (' + expected + ').',
          'Store the verification of ' + expected + ' documents in the database. First make sure infra took a fresh DB snapshot. To confirm, type the number of documents (' + expected + ').');
      } else {
        rrMsg(txt, 'Prepísať naostro: ' + expected + ' vybraných dokumentov — mení sidecary v Drive (so zálohou) a databázu. Najprv čerstvý snapshot DB. Na potvrdenie napíš ' + expected + '.',
          'Re-OCR for real: ' + expected + ' selected documents — rewrites sidecars in Drive (with a backup) and the database. Fresh DB snapshot first. To confirm, type ' + expected + '.');
      }
      var input = document.getElementById('rr-confirm-input');
      input.value = '';
      document.getElementById('rr-confirm-go').disabled = true;
      box.style.display = 'block';
      input.focus();
    }

    function rrConfirmCheck(value) {
      var go = document.getElementById('rr-confirm-go');
      if (go) go.disabled = !rrState.pending || String(value).trim() !== String(rrState.pending.expected);
    }

    function rrConfirmGo() {
      var p = rrState.pending;
      var input = document.getElementById('rr-confirm-input');
      if (!p || !input || input.value.trim() !== String(p.expected)) return;
      rrConfirmCancel();
      rrSubmit(p.kind, p.ids, false);
    }

    function rrConfirmCancel() {
      rrState.pending = null;
      var box = document.getElementById('rr-confirm');
      if (box) box.style.display = 'none';
    }

    async function rrSubmit(kind, ids, dry) {
      rrUpdateButtons(true);
      var url = kind === 'reocr' ? '/api/admin/reocr' : '/api/admin/reread';
      var body = { patient_slug: rrState.pid, dry_run: dry };
      if (ids.length) body.document_ids = ids;
      var res = await coFetch(url, { method: 'POST', body: body });
      var note = document.getElementById('rr-note');
      if (res.status === 202) {
        rrMsg(note, (dry ? 'Skúšobný beh' : 'Beh naostro') + ' spustený.', (dry ? 'Dry run' : 'Real run') + ' started.');
      } else if (res.ok && res.data && res.data.status) {
        rrMsg(note, 'Nič na spracovanie.', 'Nothing to do.');
      } else {
        var err = (res.data && res.data.error) || ('HTTP ' + res.status);
        rrMsg(note, 'Nespustené: ' + err, 'Not started: ' + err);
      }
      await rrLoadStatus();
    }

    function rrCell(v) {
      return (v === undefined || v === null) ? '—' : v;
    }

    async function rrLoadStatus() {
      if (rrState.poll) { clearTimeout(rrState.poll); rrState.poll = null; }
      if (!rrState.pid) return;
      var pidAtCall = rrState.pid;
      var res = await coFetch('/api/admin/reocr-status?patient_slug=' + encodeURIComponent(pidAtCall));
      if (pidAtCall !== rrState.pid) return;  // patient changed meanwhile
      var box = document.getElementById('rr-status');
      clearEl(box);
      if (!res.ok || !res.data) {
        box.appendChild(coBilingual('p', 'Stav sa nepodarilo načítať (HTTP ' + res.status + ').', 'Could not load status (HTTP ' + res.status + ').', 'curated-ops-note'));
        rrUpdateButtons(false);
        return;
      }
      var j = res.data;
      var running = j.state === 'running';
      var kindSk = j.kind === 'reread' ? 'overenie' : (j.kind ? 're-OCR' : '—');
      var kindEn = j.kind === 'reread' ? 'verification' : (j.kind ? 're-OCR' : '—');
      var when = (j.finished_at || j.started_at || '').replace('T', ' ').slice(0, 19);
      var modeSk = j.dry_run === undefined ? '' : (j.dry_run ? ', skúšobne' : ', naostro');
      var modeEn = j.dry_run === undefined ? '' : (j.dry_run ? ', dry run' : ', real');
      box.appendChild(coBilingual('h4', 'Posledná úloha: ' + kindSk + modeSk + ' — ' + j.state + (when ? ' (' + when + ' UTC)' : ''),
        'Last job: ' + kindEn + modeEn + ' — ' + j.state + (when ? ' (' + when + ' UTC)' : '')));
      if (j.error) box.appendChild(coBilingual('p', 'Chyba: ' + j.error, 'Error: ' + j.error, 'curated-ops-note'));
      if (j.pre_store) box.appendChild(coBilingual('p', 'Bezpečnostná kópia DB pred uložením: ' + j.pre_store, 'Pre-store DB safety copy: ' + j.pre_store, 'curated-ops-note'));
      if (running) box.appendChild(coBilingual('p', 'Beží… lokálny model číta minúty na stranu; stav sa obnovuje každých 15 s.', 'Running… the local model takes minutes per page; status refreshes every 15 s.', 'curated-ops-note'));
      var names = {};
      (j.documents || []).forEach(function (d) { names[d.document_id] = d.filename; });
      var results = j.results || [];
      if (results.length) {
        var t = coEl('table'), hr = coEl('tr');
        [['Dokument', 'Document'], ['Výsledok', 'Result'], ['Potvrdené', 'Confirmed'], ['Bez dátumu', 'Date unconfirmed'],
         ['Nepotvrdené', 'Unconfirmed'], ['Kandidáti', 'Candidates'], ['Sekundy', 'Seconds'], ['Chyba', 'Error']].forEach(function (h) { hr.appendChild(coBilingual('th', h[0], h[1])); });
        t.appendChild(hr);
        results.forEach(function (r) {
          var tr = coEl('tr');
          tr.appendChild(coEl('td', r.document_id + ' ' + (r.filename || names[r.document_id] || '')));
          tr.appendChild(coEl('td', r.result || '—'));
          ['confirmed', 'date_unconfirmed', 'unconfirmed', 'candidates', 'seconds'].forEach(function (k) { tr.appendChild(coEl('td', rrCell(r[k]), 'rr-num')); });
          tr.appendChild(coEl('td', r.error || (r.lost_count ? ('lost ' + r.lost_count) : '')));
          t.appendChild(tr);
        });
        box.appendChild(t);
      } else if ((j.documents || []).length) {
        var ul = coEl('ul');
        j.documents.forEach(function (d) { ul.appendChild(coEl('li', d.document_id + ' ' + (d.filename || ''))); });
        box.appendChild(ul);
      }
      if (j.verification && j.verification.coded !== undefined) {
        var v = j.verification;
        box.appendChild(coBilingual('p', 'Po uložení: potvrdené re-readom ' + rrCell(v.reread_applied) + ', neoverené ' + v.unverified + ' z ' + v.coded + ' (' + v.unverified_pct + ' %).',
          'After store: confirmed by re-read ' + rrCell(v.reread_applied) + ', unverified ' + v.unverified + ' of ' + v.coded + ' (' + v.unverified_pct + ' %).', 'curated-ops-note'));
      }
      rrUpdateButtons(running);
      if (typeof applyDashLang === 'function') applyDashLang();
      if (running) rrState.poll = setTimeout(rrLoadStatus, 15000);
    }

    async function triggerManualSync() {
      var btn = document.getElementById('ip-sync-btn');
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      var origText = btn.textContent;
      btn.textContent = dashLang === 'sk' ? 'Synchronizujem\u2026' : 'Syncing\u2026';
      try {
        var pid = currentPatientId || '';
        var resp = await fetch('/api/sync-trigger', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ patient_id: pid })
        });
        if (resp.ok) {
          btn.textContent = '\u2713';
          setTimeout(function() { btn.textContent = origText; btn.disabled = false; refresh(); }, 1500);
        } else {
          var data = await resp.json();
          btn.textContent = isGateRefusal(resp, data) ? gateRefusalText(data) : (data.error || 'Error');
          setTimeout(function() { btn.textContent = origText; btn.disabled = false; }, 3000);
        }
      } catch (e) {
        btn.textContent = 'Error';
        setTimeout(function() { btn.textContent = origText; btn.disabled = false; }, 3000);
      }
    }

    function wizardSkipToEnd() {
      showWizardSummary(null);
      wizardGoToStep(4);
    }

    function showWizardSummary(stats) {
      var el = document.getElementById('wiz-success-summary');
      // #835: bilingual; textContent only (wizSetBi), so no esc() needed.
      var sk = 'Pacient „' + wizardPatientId + '“ je vytvorený.';
      var en = 'Patient "' + wizardPatientId + '" created.';
      if (stats && stats.new > 0) {
        sk += ' Importované dokumenty: ' + stats.new + '.';
        en += ' ' + stats.new + ' documents imported.';
      }
      wizSetBi(el, sk, en);
      // Populate token for copy in step 4 (step-2 box uses same variable but different element)
      var tokenEl = document.getElementById('wiz-token-display-4');
      if (tokenEl && wizardBearerToken) {
        wizSetPlain(tokenEl, wizardBearerToken);  // no data-sk/en: applyDashLang must not overwrite it
      } else if (tokenEl) {
        wizSetBi(tokenEl, '(vygenerujte cez tlačidlo Zdieľať)', '(generate via Share button)');
      }
    }

    function wizCopy(elementId) {
      var el = document.getElementById(elementId);
      if (el && navigator.clipboard) {
        navigator.clipboard.writeText(el.textContent).then(function() {
          var btn = el.nextElementSibling;
          if (btn) {
            // #835: bilingual; the button's own data-sk / data-en label comes back.
            btn.textContent = dashLang === 'sk' ? 'Skopírované!' : 'Copied!';
            setTimeout(function() {
              btn.textContent = btn.getAttribute(dashLang === 'sk' ? 'data-sk' : 'data-en') || 'Copy';
            }, 2000);
          }
        });
      }
    }

    async function wizardFinish() {
      hideOnboarding();
      // Switch to new patient and reload. #835: by UUID (what /api/patients
      // lists and the select holds); the slug only when no UUID came back.
      currentPatientId = wizardPatientUuid || wizardPatientId;
      localStorage.setItem('oncofiles_patient_id', currentPatientId);
      await loadPatients();
      document.getElementById('patient-select').value = currentPatientId;
      refresh();
    }

    // ── Share modal ─────────────────────────────────────────────────────

    var MCP_URL = 'https://oncofiles.com/mcp';

    function showShareModal() {
      document.getElementById('share-code-box').style.display = 'none';
      var genBtn = document.getElementById('share-gen-btn');
      genBtn.disabled = false;
      setI18n(genBtn, 'Vygenerovať token', 'Generate a token');
      setI18n(document.getElementById('share-token'), 'zatiaľ nevygenerovaný', 'not generated yet');
      updateShareInstructions('');
      document.getElementById('share-overlay').classList.add('active');
    }

    function hideShareModal() {
      document.getElementById('share-overlay').classList.remove('active');
    }

    function copyText(elementId) {
      var text = document.getElementById(elementId).textContent;
      navigator.clipboard.writeText(text).then(function() {
        // Brief visual feedback
        var btn = document.getElementById(elementId).parentNode.querySelector('.copy-btn');
        if (btn) {
          setI18n(btn, 'Skopírované', 'Copied');
          setTimeout(function() { setI18n(btn, 'Kopírovať', 'Copy'); }, 1500);
        }
      });
    }

    async function generateShareCode() {
      var btn = document.getElementById('share-gen-btn');
      btn.disabled = true;
      setI18n(btn, 'Generujem…', 'Generating…');

      try {
        var fetchOpts = {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ patient_id: currentPatientId })
        };
        var resp = await fetch('/api/share-link', fetchOpts);
        // Retry once on 500 (Turso contention during startup)
        if (resp.status === 500) {
          await new Promise(function(r){ setTimeout(r, 2000); });
          resp = await fetch('/api/share-link', fetchOpts);
        }
        var data = await resp.json();
        if (!resp.ok) {
          setI18n(btn, 'Nepodarilo sa. Skúsiť znova', 'It did not work. Try again');
          btn.disabled = false;
          return;
        }

        // Show the code
        document.getElementById('share-code-value').textContent = data.code;
        document.getElementById('share-code-box').style.display = 'block';
        setI18n(btn, 'Token vygenerovaný', 'Token generated');

        // Now redeem it immediately to get the token for display
        var redeemResp = await fetch('/api/share-link/' + data.code);
        if (redeemResp.ok) {
          var info = await redeemResp.json();
          var tokenEl = document.getElementById('share-token');
          tokenEl.removeAttribute('data-sk');
          tokenEl.removeAttribute('data-en');
          tokenEl.textContent = info.bearer_token;
          updateShareInstructions(info.bearer_token);
        }
      } catch (e) {
        setI18n(btn, 'Nepodarilo sa. Skúsiť znova', 'It did not work. Try again');
        btn.disabled = false;
      }
    }

    function updateShareInstructions(token) {
      // Desktop config
      var config = {
        mcpServers: {
          oncofiles: { url: MCP_URL }
        }
      };
      if (token) {
        config.mcpServers.oncofiles.headers = { Authorization: 'Bearer ' + token };
      }
      document.getElementById('share-desktop-config').textContent = JSON.stringify(config, null, 2);

      // Claude Code command
      var cmd = 'claude mcp add oncofiles ' + MCP_URL;
      if (token) cmd += " --header 'Authorization: Bearer " + token + "'";
      document.getElementById('share-code-cmd').textContent = cmd;
    }

    function switchShareTab(tabId, btn) {
      document.querySelectorAll('.share-tab-content').forEach(function(el) { el.style.display = 'none'; });
      document.querySelectorAll('.share-tab').forEach(function(el) { el.classList.remove('active'); });
      document.getElementById('share-tab-' + tabId).style.display = 'block';
      if (btn) btn.classList.add('active');
    }

    function setConnectionState(state) {
      var dot = document.getElementById('connection-dot');
      dot.className = 'connection-dot ' + (state === 'ok' ? '' : state);
      dot.title = state === 'ok' ? 'Connected' : state === 'loading' ? 'Loading...' : 'Offline';
    }

    var TILE_IDS = ['card-total', 'card-ready', 'card-progress', 'card-attention'];

    function showSkeletons() {
      TILE_IDS.forEach(function(id) {
        var el = document.getElementById(id);
        if (!el) return;
        el.textContent = '';
        el.className = 'value skeleton';
      });
    }

    function hideSkeletons() {
      TILE_IDS.forEach(function(id) {
        var el = document.getElementById(id);
        if (el) el.className = 'value';
      });
    }

    async function refresh() {
      hideError();
      setConnectionState('loading');

      // Stale-while-revalidate: render last known state instantly so the UI
      // stays populated during the fetch. Only fall back to skeletons on
      // first-ever load (no cache yet). See #469 Phase 5.
      var hydrated = _hydrateFromSwrCache();
      if (!hydrated) showSkeletons();

      var anyOk = false;
      var errors = [];
      // First 503 wins — we show one friendly banner + auto-retry at Retry-After
      // instead of the red "Partial load failure" cascade. See #469.
      var breakerError = null;
      function trackBreaker(e) {
        if (e && e.status === 503 && e.retryAfterMs) {
          if (!breakerError) breakerError = e;
          return true;
        }
        return false;
      }
      // Sequential fetches: the server holds one DB connection.
      // Each section is independent: one failure doesn't block the others.
      try {
        var status = await apiFetch('/status');
        renderStatus(status);
        _swrSet('status', status);
        anyOk = true;
      } catch (e) {
        if (e.message === 'Unauthorized') { setConnectionState('offline'); hideSkeletons(); return; }
        if (!trackBreaker(e)) errors.push('status: ' + e.message);
      }
      try {
        // The whole list in one call (the per-patient cap is far below 2000);
        // tiles, search and sorting then work in the browser without a round trip.
        var docs = await apiFetch('/api/documents?filter=all&limit=2000');
        renderDocs(docs);
        _swrSet('documents', docs);
        anyOk = true;
      } catch (e) {
        if (!trackBreaker(e)) errors.push('documents: ' + e.message);
      }
      try {
        var activity = await apiFetch('/api/activity?limit=30');
        renderActivity(activity);
        _swrSet('activity', activity);
        anyOk = true;
      } catch (e) {
        if (!trackBreaker(e)) { console.warn('Activity fetch failed:', e); renderActivity(null); }
      }
      // #950: the document e-mail address (once per patient; hides itself on any error).
      await loadInboundAddress(false);
      // Budget pill (#442) — fire-and-forget, hides itself when tier is admin/paid_*.
      updateBudgetPill();
      // Admin panels load only when an admin has opened them.
      updateAdminArea();
      hideSkeletons();

      // 503 — show friendly SK/EN countdown + auto-retry instead of a
      // red cascade. Any data we DID render during the partial load stays visible.
      if (breakerError) {
        setConnectionState('loading');
        showBreakerBanner(breakerError.retryAfterMs);
        return;
      }

      setConnectionState(anyOk ? 'ok' : 'offline');
      if (anyOk) updateLastRefreshed();
      if (errors.length) {
        showError((dashLang === 'sk' ? 'Časť údajov sa nepodarilo načítať: ' : 'Some data could not be loaded: ') + errors.join('; '));
      }
    }

    // ── Renderers ────────────────────────────────────────────────────────

    function fmtUptime(sec) {
      if (!sec || sec <= 0) return '-';
      var days = Math.floor(sec / 86400);
      var hours = Math.floor((sec % 86400) / 3600);
      var mins = Math.floor((sec % 3600) / 60);
      return days > 0 ? days + 'd ' + hours + 'h' : hours > 0 ? hours + 'h ' + mins + 'm' : mins + 'm';
    }

    function renderStatus(s) {
      window._lastStatus = s;
      var versionEl = document.getElementById('version-label');
      if (versionEl) versionEl.textContent = 'v' + (s.version || '?');

      // Google service connection badges
      var gs = s.google_services || {};
      ['drive', 'gmail', 'calendar'].forEach(function(svc) {
        var id = svc === 'calendar' ? 'svc-cal' : 'svc-' + svc;
        var el = document.getElementById(id);
        if (el) el.className = 'svc-badge ' + (gs[svc] ? 'on' : 'off');
      });
      // Integration popover
      var ipEmail = document.getElementById('ip-email');
      if (ipEmail) ipEmail.textContent = gs.owner_email || '—';
      [['drive', 'drive'], ['gmail', 'gmail'], ['cal', 'calendar']].forEach(function(pair) {
        var dot = document.getElementById('ip-' + pair[0] + '-dot');
        var txt = document.getElementById('ip-' + pair[0] + '-text');
        var on = !!gs[pair[1]];
        if (dot) dot.className = 'ip-status ' + (on ? 'on' : 'off');
        if (txt) setI18n(txt, on ? 'pripojené' : 'nepripojené', on ? 'connected' : 'not connected');
      });
      // Per-service last sync times (#276)
      var sst = s.service_sync_times || {};
      ['drive', 'gmail', 'cal'].forEach(function(key) {
        var el = document.getElementById('ip-' + key + '-sync');
        if (!el) return;
        var t = sst[key === 'cal' ? 'calendar' : key];
        el.textContent = t ? new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '';
      });
      var ipFolder = document.getElementById('ip-folder');
      if (ipFolder) {
        ipFolder.textContent = '';
        var folderName = loc(gs, 'folder_name');
        if (gs.folder_id) {
          var folderLink = document.createElement('a');
          folderLink.href = 'https://drive.google.com/drive/folders/' + encodeURIComponent(gs.folder_id);
          folderLink.target = '_blank';
          folderLink.rel = 'noopener';
          folderLink.title = folderName || gs.folder_id;
          folderLink.textContent = folderName || (gs.folder_id.substring(0, 12) + '…');
          ipFolder.appendChild(folderLink);
        } else {
          ipFolder.textContent = folderName || '—';
        }
      }
      // Show GDrive connect banner if no folder set for this patient
      var gdriveBar = document.getElementById('gdrive-connect-bar');
      if (gdriveBar) gdriveBar.style.display = (!gs.drive || (!gs.folder_id && !gs.folder_name)) ? 'flex' : 'none';

      var serverLine = document.getElementById('admin-server-line');
      if (serverLine) {
        setI18n(serverLine,
          'Server beží ' + fmtUptime(s.uptime_s) + ' · verzia ' + (s.version || '?') + ' · pamäť ' + (s.memory_rss_mb || '?') + ' MB. Panely nižšie sa načítajú až po rozbalení.',
          'Server up ' + fmtUptime(s.uptime_s) + ' · version ' + (s.version || '?') + ' · memory ' + (s.memory_rss_mb || '?') + ' MB. The panels below load when opened.');
      }
    }

    // ── Documents: one status per document ───────────────────────────────

    var docFilter = 'all';           // all | ready | in_progress | attention (the tiles)
    var docsSignature = '';          // skip a re-render when a refresh brought nothing new
    var openDoc = { id: null, data: null };  // the expanded row survives refreshes

    var STAGE_RANK = { attention: 0, in_inbox: 1, waiting_text: 2, waiting_name: 3, waiting_filing: 4, ready: 5 };
    var STAGE_TEXT = {
      ready: ['Pripravený na použitie', 'Ready to use'],
      in_inbox: ['Čaká na spracovanie', 'Waiting to be processed'],
      waiting_text: ['Čaká na vyťaženie', 'Waiting for extraction'],
      waiting_name: ['Čaká na premenovanie', 'Waiting to be renamed'],
      waiting_filing: ['Čaká na zaradenie', 'Waiting to be filed'],
      attention: ['Vyžaduje pozornosť', 'Needs attention']
    };
    var ATTENTION_TEXT = {
      review_needed: ['Čaká na vašu kontrolu', 'Needs your review'],
      missing_in_drive: ['Súbor chýba na Drive', 'File missing in Drive'],
      outside_folder: ['Súbor je mimo priečinka pacienta', 'File is outside the patient folder'],
      missing_date: ['Nenašiel sa dátum dokumentu', 'No document date found']
    };
    var ATTENTION_HELP = {
      review_needed: ['Pri čítaní si systém nebol istý (napríklad rukopis alebo otočená strana). Porovnajte prečítaný text a údaje s originálom.', 'The system was not sure when reading it (for example handwriting or a rotated page). Compare the text and data read with the original.'],
      missing_in_drive: ['Súbor sa na Google Drive už nenašiel. Ak ste ho presunuli alebo zmazali, obnovte ho z koša Drive.', 'The file is no longer found on Google Drive. If you moved or deleted it, restore it from the Drive trash.'],
      outside_folder: ['Súbor existuje, ale nie je v priečinku tohto pacienta. Presuňte ho späť do priečinka pacienta.', 'The file exists but is not in this patient\'s folder. Move it back into the patient folder.'],
      missing_date: ['Text je prečítaný, ale nie je v ňom vytlačený dátum dokumentu, preto ho nevieme zaradiť na časovú os.', 'The text has been read, but no document date is printed in it, so it cannot be placed on the timeline.']
    };
    // #919: concrete help per review reason code (`review_reasons` on a row,
    // codes only); an unknown code falls back to ATTENTION_HELP.review_needed.
    var REVIEW_REASON_HELP = {
      name_not_standard: ['Z prečítaných údajov sa nedal zostaviť štandardný názov súboru. Skontrolujte dátum, typ a inštitúciu.', 'A standard file name could not be built from the data read. Check the date, type and institution.'],
      no_date: ['V texte sa nenašiel dátum vyšetrenia alebo správy. Doplňte ho, ak je na origináli vytlačený.', 'No exam or report date was found in the text. Add it if it is printed on the original.'],
      birth_date: ['Jediný nájdený dátum je dátum narodenia, nie dátum dokumentu.', 'The only date found is the date of birth, not the document date.'],
      future_date: ['Prečítaný dátum je v budúcnosti. Porovnajte ho s originálom.', 'The date read is in the future. Compare it with the original.'],
      implausible_date: ['Prečítaný dátum nie je možný. Porovnajte ho s originálom.', 'The date read is not plausible. Compare it with the original.'],
      date_not_printed: ['Navrhnutý dátum nie je v texte dokumentu vytlačený. Porovnajte ho s originálom.', 'The proposed date is not printed in the document text. Compare it with the original.'],
      date_too_old: ['Dátum je veľmi starý; mohol byť prečítaný z inej časti strany. Overte ho.', 'The date is very old; it may have been read from another part of the page. Check it.'],
      unknown_type: ['Typ dokumentu sa nepodarilo určiť. Zvoľte správny typ.', 'The document type could not be determined. Choose the right type.'],
      institution_unknown: ['Inštitúcia sa v texte nenašla.', 'The institution was not found in the text.'],
      new_vocabulary: ['Dokument uvádza novú inštitúciu alebo lekára. Potvrďte, či je zápis správny.', 'The document names a new institution or doctor. Confirm that the spelling is correct.'],
      near_vocabulary: ['Nová inštitúcia alebo lekár sa podobá na už známy záznam. Potvrďte, či ide o ten istý.', 'A new institution or doctor looks like a known entry. Confirm whether it is the same.'],
      split_needed: ['Súbor zrejme obsahuje viac dokumentov. Treba ho rozdeliť.', 'The file seems to hold several documents. It needs to be split.'],
      low_confidence: ['Model si pri zaradení nebol istý. Porovnajte údaje s originálom.', 'The model was not sure about the filing. Compare the data with the original.'],
      other_person: ['Dokument zrejme patrí inej osobe s rovnakým priezviskom. Overte, komu patrí.', 'The document seems to belong to another person with the same surname. Check whose it is.'],
      chart_page: ['Na strane je graf; jeho hodnoty sa ako údaje nečítajú. Overte text s originálom.', 'A page holds a chart; its values are never read as data. Check the text against the original.'],
      rotated_region: ['Časť strany je otočená a neprečítala sa. Overte text s originálom.', 'Part of a page is rotated and was not read. Check the text against the original.'],
      orientation_unsure: ['Nebolo isté, ako je strana otočená. Overte text s originálom.', 'It was not certain how the page is turned. Check the text against the original.'],
      extraction_failed: ['Text sa nepodarilo prečítať. Systém to skúsi znova.', 'The text could not be read. The system will try again.'],
      classification_failed: ['Zaradenie zlyhalo technicky. Systém to skúsi znova.', 'Filing failed for a technical reason. The system will try again.'],
      no_text: ['V súbore sa nenašiel žiadny text.', 'No text was found in the file.'],
      raw_genotype_excluded: ['Surové genetické dáta sa modelu neposielajú; súbor zaraďte ručne.', 'Raw genetic data is never sent to the model; file it by hand.'],
      target_folder_missing: ['Cieľový priečinok pre tento typ chýba.', 'The target folder for this type is missing.']
    };
    function attentionHelp(doc) {
      if (doc.attention_reason === 'review_needed' && Array.isArray(doc.review_reasons)) {
        var sk = [], en = [];
        doc.review_reasons.forEach(function (code) {
          var t = REVIEW_REASON_HELP[code];
          if (t && sk.indexOf(t[0]) === -1) { sk.push(t[0]); en.push(t[1]); }
        });
        if (sk.length) return [sk.join(' '), en.join(' ')];
      }
      return ATTENTION_HELP[doc.attention_reason] || null;
    }
    var STEP_TEXT = {
      extracted: ['Vyťažený', 'Extracted'],
      named: ['Premenovaný', 'Renamed'],
      filed: ['Zaradený', 'Filed']
    };
    var CATEGORY_TEXT = {
      labs: ['Laboratórne výsledky', 'Lab results'], report: ['Lekárska správa', 'Medical report'],
      imaging: ['Zobrazovacie vyšetrenie', 'Imaging'], pathology: ['Patológia', 'Pathology'],
      genetics: ['Genetika', 'Genetics'], surgery: ['Operácia', 'Surgery'],
      surgical_report: ['Operačný protokol', 'Surgical report'], prescription: ['Recept', 'Prescription'],
      referral: ['Odporúčanie', 'Referral'], discharge: ['Prepustenie', 'Discharge'],
      discharge_summary: ['Prepúšťacia správa', 'Discharge summary'], chemo_sheet: ['Chemoterapia', 'Chemotherapy'],
      reference: ['Referenčný materiál', 'Reference'], advocate: ['Poznámky', 'Notes'],
      vaccination: ['Očkovanie', 'Vaccination'], dental: ['Zubné', 'Dental'],
      preventive: ['Prevencia', 'Preventive care'], other: ['Iné', 'Other']
    };

    /** Demo rows carry an English twin of a few text fields (`*_en`); real rows never do. */
    function loc(obj, key) {
      if (!obj) return '';
      if (dashLang === 'en' && obj[key + '_en']) return obj[key + '_en'];
      return obj[key] == null ? '' : obj[key];
    }

    function parseTs(value) {
      if (!value) return null;
      var s = String(value);
      // SQLite timestamps are UTC without a zone marker
      if (s.length > 10 && s.indexOf('T') === -1) s = s.replace(' ', 'T');
      if (s.length > 10 && !/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += 'Z';
      var d = new Date(s.length === 10 ? s + 'T00:00:00' : s);
      return isNaN(d.getTime()) ? null : d;
    }

    function fmtDate(value) {
      var d = parseTs(value);
      if (!d) return '–';
      return d.toLocaleDateString(dashLang === 'sk' ? 'sk-SK' : 'en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
    }

    function fmtDateTime(value) {
      var d = parseTs(value);
      if (!d) return '–';
      return d.toLocaleString(dashLang === 'sk' ? 'sk-SK' : 'en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    }

    function stateMark(kind) {
      var m = document.createElement('span');
      m.className = 'state-mark ' + kind;
      m.setAttribute('aria-hidden', 'true');
      return m;
    }

    function stageKind(d) {
      return d.stage === 'ready' ? 'ready' : d.stage === 'attention' ? 'attention' : 'progress';
    }

    function stageLabel(d) {
      if (d.stage === 'attention' && ATTENTION_TEXT[d.attention_reason]) return ATTENTION_TEXT[d.attention_reason];
      return STAGE_TEXT[d.stage] || STAGE_TEXT.waiting_text;
    }

    function matchesFilter(d) {
      if (docFilter === 'all') return true;
      if (docFilter === 'ready') return d.stage === 'ready';
      if (docFilter === 'attention') return d.stage === 'attention';
      return d.stage !== 'ready' && d.stage !== 'attention';
    }

    // A new patient starts on the full list: no tile filter, no search, no open
    // row, no quick look, and none of the previous patient's previews.
    function resetDocView() {
      closeQuickLook();
      resetThumbs();
      openDoc = { id: null, data: null };
      docsSignature = '';
      var search = document.getElementById('doc-search');
      if (search) search.value = '';
      setDocFilter('all');
    }

    function setDocFilter(filter) {
      docFilter = filter;
      document.querySelectorAll('#summary-cards .card').forEach(function(card) {
        var on = card.dataset.filter === filter;
        card.classList.toggle('is-active', on);
        card.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      renderTable();
    }

    function renderDocs(data) {
      var summary = data.summary || {};
      var rows = data.documents || [];
      var total = summary.total != null ? summary.total : rows.length;
      var ready = summary.ready != null ? summary.ready : rows.filter(function(d) { return d.stage === 'ready'; }).length;
      var attention = summary.attention != null ? summary.attention : rows.filter(function(d) { return d.stage === 'attention'; }).length;
      var progress = summary.in_progress != null ? summary.in_progress : Math.max(0, total - ready - attention);
      document.getElementById('card-total').textContent = total;
      document.getElementById('card-ready').textContent = ready;
      document.getElementById('card-progress').textContent = progress;
      document.getElementById('card-attention').textContent = attention;
      var attentionTile = document.querySelector('#summary-cards .card[data-filter="attention"]');
      if (attentionTile) attentionTile.classList.toggle('has-items', attention > 0);

      // An unchanged list draws nothing again: no flicker, no preview asked twice.
      var signature = JSON.stringify(rows);
      if (signature === docsSignature && docsDrawn()) return;
      docsSignature = signature;
      docsData = rows.map(function(d) {
        d.stage_rank = STAGE_RANK[d.stage] !== undefined ? STAGE_RANK[d.stage] : STAGE_RANK.waiting_text;
        return d;
      });
      renderTable();
    }

    /** Whether the visible view (list or grid) already shows documents. */
    function docsDrawn() {
      return !!document.querySelector(docView === 'grid' ? '#doc-grid .doc-card' : '#doc-tbody .doc-row');
    }

    function tableMessage(sk, en) {
      var tbody = document.getElementById('doc-tbody');
      clearEl(tbody);
      var tr = document.createElement('tr');
      var td = document.createElement('td');
      td.setAttribute('colspan', '8');
      td.className = 'table-empty';
      setI18n(td, sk, en);
      tr.appendChild(td);
      tbody.appendChild(tr);
      var grid = document.getElementById('doc-grid');
      if (grid) {
        clearEl(grid);
        var li = document.createElement('li');
        li.className = 'grid-empty';
        grid.appendChild(setI18n(li, sk, en));
      }
    }

    /** The documents the tiles and the search let through, in the current sort order
        (one list for the table, the grid and the quick look's previous / next). */
    function docListView() {
      var searchEl = document.getElementById('doc-search');
      var query = (searchEl ? searchEl.value : '').toLowerCase().trim();
      var filtered = docsData.filter(matchesFilter);
      if (query) {
        filtered = filtered.filter(function(d) {
          var cat = CATEGORY_TEXT[d.category] || [d.category || '', d.category || ''];
          return [loc(d, 'filename'), d.category, cat[0], cat[1], loc(d, 'institution'), d.date, fmtDate(d.date)]
            .join(' ').toLowerCase().indexOf(query) !== -1;
        });
      }
      // Missing values always sort last, whatever the direction.
      var sorted = filtered.slice().sort(function(a, b) {
        var va = sortCol === 'filename' ? loc(a, 'filename') : sortCol === 'institution' ? loc(a, 'institution') : a[sortCol];
        var vb = sortCol === 'filename' ? loc(b, 'filename') : sortCol === 'institution' ? loc(b, 'institution') : b[sortCol];
        var ea = va == null || va === '';
        var eb = vb == null || vb === '';
        if (ea || eb) return ea && eb ? 0 : ea ? 1 : -1;
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
      });
      return { query: query, filtered: filtered, sorted: sorted };
    }

    function renderTable() {
      var tbody = document.getElementById('doc-tbody');
      var countEl = document.getElementById('docs-count');
      var view = docListView();
      var query = view.query;
      var filtered = view.filtered;
      // the previous tiles are gone: watch only the ones drawn now
      resetThumbObserver();
      if (quickLook.id !== null) renderQuickLookNav();
      if (countEl) {
        if (docsData.length === 0) { countEl.removeAttribute('data-sk'); countEl.removeAttribute('data-en'); countEl.textContent = ''; }
        else setI18n(countEl, 'zobrazených ' + filtered.length + ' z ' + docsData.length, 'showing ' + filtered.length + ' of ' + docsData.length);
      }

      if (docsData.length === 0) {
        tableMessage('Zatiaľ tu nie sú žiadne dokumenty. Pridajte ich tlačidlom „Pridať dokumenty“ alebo ich nahrajte do priečinka pacienta na Google Drive a Oncofiles ich spracuje.',
          'No documents yet. Add them with “Add documents” or put files into the patient folder on Google Drive and Oncofiles will process them.');
        return;
      }
      if (filtered.length === 0) {
        tableMessage(query ? 'Hľadaniu nezodpovedá žiadny dokument.' : 'V tomto stave nie je žiadny dokument.',
          query ? 'No document matches the search.' : 'No document is in this state.');
        return;
      }

      if (docView === 'grid') { renderGrid(view.sorted); return; }

      clearEl(tbody);
      var reopen = null;
      view.sorted.forEach(function(d) {
        var tr = buildDocRow(d);
        tbody.appendChild(tr);
        if (openDoc.id !== null && d.id === openDoc.id) reopen = { doc: d, tr: tr };
      });
      if (reopen) openDocDetail(reopen.doc, reopen.tr, true);
      else openDoc = { id: null, data: null };
    }

    function buildDocRow(d) {
      var tr = document.createElement('tr');
      tr.className = 'doc-row';
      tr.tabIndex = 0;
      tr.setAttribute('role', 'button');
      tr.setAttribute('aria-expanded', 'false');

      // #948: the preview opens the quick look; the rest of the row opens the detail
      var tdThumb = document.createElement('td');
      tdThumb.className = 'col-thumb';
      var thumbBtn = document.createElement('button');
      thumbBtn.type = 'button';
      thumbBtn.className = 'thumb-btn';
      thumbBtn.setAttribute('data-doc-id', String(d.id));
      thumbBtn.setAttribute('aria-haspopup', 'dialog');
      thumbBtn.appendChild(buildThumb(d));
      var thumbLabel = document.createElement('span');
      thumbLabel.className = 'sr-only';
      thumbBtn.appendChild(setI18n(thumbLabel, 'Zväčšený náhľad', 'Large preview'));
      thumbBtn.addEventListener('click', function(e) { e.stopPropagation(); openQuickLook(d.id, thumbBtn); });
      tdThumb.appendChild(thumbBtn);
      tr.appendChild(tdThumb);

      var tdToggle = document.createElement('td');
      tdToggle.className = 'col-toggle';
      var chev = document.createElement('span');
      chev.className = 'row-chevron';
      chev.setAttribute('aria-hidden', 'true');
      tdToggle.appendChild(chev);
      tr.appendChild(tdToggle);

      var name = loc(d, 'filename');
      var tdFn = document.createElement('td');
      tdFn.className = 'filename';
      tdFn.appendChild(filenameText(name, 'filename-text'));
      tdFn.title = name;
      tr.appendChild(tdFn);

      var tdCat = document.createElement('td');
      if (d.category) tdCat.appendChild(categoryBadge(d.category));
      else tdCat.textContent = '–';  // an inbox file is not classified yet
      tr.appendChild(tdCat);

      var tdInst = document.createElement('td');
      tdInst.className = 'cell-inst';
      tdInst.textContent = loc(d, 'institution') || '–';
      tr.appendChild(tdInst);

      var tdDate = document.createElement('td');
      tdDate.className = 'cell-date';
      tdDate.textContent = fmtDate(d.date);
      tr.appendChild(tdDate);

      var tdUp = document.createElement('td');
      tdUp.className = 'cell-date';
      tdUp.textContent = fmtDate(d.uploaded_at);
      if (d.uploaded_at) tdUp.title = fmtDateTime(d.uploaded_at);
      tr.appendChild(tdUp);

      var tdStage = document.createElement('td');
      tdStage.className = 'cell-stage';
      var stageBox = document.createElement('div');
      stageBox.className = 'stage-box';
      stageBox.appendChild(stageChip(d));
      stageBox.appendChild(buildStepTrack(d));
      tdStage.appendChild(stageBox);
      tr.appendChild(tdStage);

      tr.addEventListener('click', function() { toggleDocDetail(d, tr); });
      tr.addEventListener('keydown', function(e) {
        if (e.target !== tr) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleDocDetail(d, tr); }
      });
      return tr;
    }

    /** A file name that may break after _ and - (standard names have no spaces). */
    function filenameText(name, cls) {
      var span = document.createElement('span');
      span.className = cls;
      String(name).split(/(?<=[_-])/).forEach(function(part, i) {
        if (i) span.appendChild(document.createElement('wbr'));
        span.appendChild(document.createTextNode(part));
      });
      return span;
    }

    function categoryBadge(category) {
      var badge = document.createElement('span');
      badge.className = 'category-badge';
      var cat = CATEGORY_TEXT[category] || [category || '', category || ''];
      return setI18n(badge, cat[0], cat[1]);
    }

    /** In place of a category badge: an inbox file is not classified yet. */
    function noCategoryMark() {
      var dash = document.createElement('span');
      dash.className = 'category-none';
      dash.textContent = '–';
      return dash;
    }

    /** An inbox row (`inbox:<drive id>`) has no document behind it, so no preview to ask for. */
    function hasPreview(d) { return !!d && !d.inbox && /^\d+$/.test(String(d.id)); }

    /** The tile or card of one document; ids are compared as text, never put into a selector. */
    function findById(selector, attr, id) {
      var want = String(id);
      var found = null;
      document.querySelectorAll(selector).forEach(function (el) {
        if (!found && el.getAttribute(attr) === want) found = el;
      });
      return found;
    }

    /** The status as a shape plus a label (never colour alone); `short` for the grid cards. */
    function stageChip(d, short) {
      var kind = stageKind(d);
      var chip = document.createElement('span');
      chip.className = 'stage-chip ' + kind;
      chip.appendChild(stateMark(kind));
      var label = short && d.stage === 'ready' ? ['Pripravený', 'Ready'] : stageLabel(d);
      chip.appendChild(setI18n(document.createElement('span'), label[0], label[1]));
      return chip;
    }

    /** Three small marks, one per step: filled = done, hollow = waiting. */
    function buildStepTrack(d) {
      var steps = d.steps || {};
      var track = document.createElement('span');
      track.className = 'step-track';
      var done = 0;
      ['extracted', 'named', 'filed'].forEach(function(key, i) {
        var mark = document.createElement('span');
        mark.className = 'step-mark' + (steps[key] ? ' done' : '');
        mark.title = (i + 1) + '. ' + STEP_TEXT[key][dashLang === 'sk' ? 0 : 1];
        if (steps[key]) done += 1;
        track.appendChild(mark);
      });
      track.setAttribute('role', 'img');
      track.setAttribute('aria-label', (dashLang === 'sk' ? 'Hotové kroky: ' : 'Steps done: ') + done + ' / 3');
      return track;
    }

    function closeDocDetail() {
      document.querySelectorAll('.doc-detail-row').forEach(function(el) { el.remove(); });
      document.querySelectorAll('#doc-tbody .doc-row.is-open').forEach(function(el) {
        el.classList.remove('is-open');
        el.setAttribute('aria-expanded', 'false');
      });
      openDoc = { id: null, data: null };
    }

    function toggleDocDetail(doc, tr) {
      if (tr.classList.contains('is-open')) { closeDocDetail(); return; }
      openDocDetail(doc, tr, false);
    }

    /** Expand a row in place. `fromRefresh` reuses the detail already loaded. */
    function openDocDetail(doc, tr, fromRefresh) {
      var cached = fromRefresh && openDoc.id === doc.id ? openDoc.data : null;
      closeDocDetail();
      openDoc = { id: doc.id, data: cached };
      tr.classList.add('is-open');
      tr.setAttribute('aria-expanded', 'true');

      var detailRow = document.createElement('tr');
      detailRow.className = 'doc-detail-row';
      var detailTd = document.createElement('td');
      detailTd.setAttribute('colspan', '8');
      detailRow.appendChild(detailTd);
      tr.parentNode.insertBefore(detailRow, tr.nextSibling);

      if (doc.inbox) { renderInboxDetail(doc, detailTd); return; }  // no document row: nothing to fetch
      if (cached) { renderDocDetail(doc, cached, detailTd); return; }

      var loadingDiv = document.createElement('div');
      loadingDiv.className = 'doc-detail-loading';
      loadingDiv.appendChild(setI18n(document.createElement('span'), 'Načítavam detail…', 'Loading detail…'));
      detailTd.appendChild(loadingDiv);

      apiFetch('/api/doc-detail/' + doc.id).then(function(data) {
        if (openDoc.id !== doc.id || !detailRow.parentNode) return;  // closed or replaced meanwhile
        if (data.error) {
          clearEl(loadingDiv);
          loadingDiv.appendChild(setI18n(document.createElement('span'), 'Detail sa nepodarilo načítať.', 'The detail could not be loaded.'));
          return;
        }
        openDoc.data = data;
        renderDocDetail(doc, data, detailTd);
      }).catch(function() {
        clearEl(loadingDiv);
        loadingDiv.appendChild(setI18n(document.createElement('span'), 'Detail sa nepodarilo načítať.', 'The detail could not be loaded.'));
      });
    }

    /** The five-step explanation at the top of the detail. */
    function buildStepList(doc, data) {
      var steps = doc.steps || {};
      var pages = (data.pages || []).length;
      var box = document.createElement('div');
      box.className = 'detail-steps';
      box.appendChild(setI18n(document.createElement('h4'), 'Stav spracovania', 'Processing status'));
      var list = document.createElement('ol');
      list.className = 'step-list';

      function add(done, title, text, extraClass) {
        var li = document.createElement('li');
        li.className = (done ? 'done' : 'todo') + (extraClass ? ' ' + extraClass : '');
        var mark = document.createElement('span');
        mark.className = 'step-mark' + (done ? ' done' : '');
        mark.setAttribute('aria-hidden', 'true');
        li.appendChild(mark);
        var body = document.createElement('span');
        body.appendChild(setI18n(document.createElement('strong'), title[0], title[1]));
        body.appendChild(document.createTextNode(' '));
        body.appendChild(setI18n(document.createElement('span'), text[0], text[1]));
        li.appendChild(body);
        list.appendChild(li);
      }

      var up = doc.uploaded_at ? fmtDate(doc.uploaded_at) : '';
      add(true, ['Nahratý', 'Uploaded'],
        up ? ['– Oncofiles o dokumente vie od ' + up + '.', '– Oncofiles has known the document since ' + up + '.']
           : ['– Oncofiles o dokumente vie.', '– Oncofiles knows the document.']);
      add(!!steps.extracted, STEP_TEXT.extracted,
        steps.extracted
          ? (pages ? ['– text je prečítaný (strán: ' + pages + ').', '– the text has been read (pages: ' + pages + ').'] : ['– pri tomto type súboru sa text nečíta.', '– no text is read for this file type.'])
          : ['– čaká, kým text prečíta model na našom serveri.', '– waiting for the model on our server to read the text.']);
      add(!!steps.named, STEP_TEXT.named,
        steps.named ? ['– názov súboru zodpovedá štandardu.', '– the file name follows the standard.']
                    : ['– súbor ešte nemá štandardný názov.', '– the file does not have the standard name yet.']);
      add(!!steps.filed, STEP_TEXT.filed,
        steps.filed ? ['– je v priečinku pacienta na Google Drive.', '– it sits in the patient folder on Google Drive.']
                    : ['– ešte nie je zaradený v priečinku na Google Drive.', '– it is not filed in the Google Drive folder yet.']);
      if (doc.stage === 'attention') {
        var help = attentionHelp(doc);
        add(false, ATTENTION_TEXT[doc.attention_reason] || STAGE_TEXT.attention,
          help ? ['– ' + help[0], '– ' + help[1]] : ['', ''], 'final attention');
      } else {
        add(doc.stage === 'ready', STAGE_TEXT.ready,
          doc.stage === 'ready' ? ['– dokument je spracovaný, pre vás aj pre AI asistenta.', '– the document is processed, for you and for the AI assistant.']
                                : ['– bude po dokončení krokov vyššie.', '– once the steps above are done.'], 'final');
      }
      box.appendChild(list);
      return box;
    }

    /** A file still in the patient's inbox on Drive: a short explanation, no detail fetch. */
    function renderInboxDetail(doc, detailTd, opts) {
      opts = opts || {};
      clearEl(detailTd);
      var panel = document.createElement('div');
      panel.className = 'doc-detail-panel inbox-detail' + (opts.overlay ? ' is-overlay' : '');
      if (!opts.overlay) {  // the quick look has its own close button
        var closeBtn = document.createElement('button');
        closeBtn.className = 'detail-close';
        closeBtn.textContent = '×';
        closeBtn.title = dashLang === 'sk' ? 'Zavrieť' : 'Close';
        closeBtn.setAttribute('aria-label', closeBtn.title);
        closeBtn.onclick = function(e) { e.stopPropagation(); closeDocDetail(); };
        panel.appendChild(closeBtn);
      }
      var label = stageLabel(doc);
      panel.appendChild(setI18n(document.createElement('h4'), label[0], label[1]));
      var p = document.createElement('p');
      if (doc.stage === 'attention') {
        setI18n(p, 'Súbor je v priečinku pre nové súbory na Google Drive. Pri čítaní si systém nebol istý (napríklad rukopis alebo otočená strana), preto súbor čaká na kontrolu a zatiaľ sa nezaradí. Kým ho niekto neskontroluje, zostáva v tomto priečinku nezmenený.',
          'The file is in the inbox folder on Google Drive. The system was not sure when reading it (for example handwriting or a rotated page), so it waits for a review and is not filed yet. Until someone reviews it, it stays unchanged in that folder.');
      } else {
        setI18n(p, 'Súbor je v priečinku pre nové súbory na Google Drive. Model na našom serveri ho o niekoľko minút prečíta, pomenuje a zaradí; potom sa tu zobrazí ako bežný dokument s textom a údajmi.',
          'The file is in the inbox folder on Google Drive. Within a few minutes the model on our server reads, names and files it; it then shows here as an ordinary document with its text and data.');
      }
      panel.appendChild(p);
      var meta = [];
      if (doc.size) meta.push(fmtSize(doc.size));
      if (doc.uploaded_at) meta.push(fmtDateTime(doc.uploaded_at));
      if (meta.length) {
        var small = document.createElement('p');
        small.className = 'inbox-detail-meta';
        small.textContent = meta.join(' · ');
        panel.appendChild(small);
      }
      detailTd.appendChild(panel);
    }

    /** The document detail. In the quick look (`opts.overlay`) the overlay has its
        own close button and shows the first page itself, so the detail brings no
        close button and no Drive preview frame; the Drive link goes to
        `opts.linkHost` under the large preview. */
    function renderDocDetail(doc, data, detailTd, opts) {
      opts = opts || {};
      var overlay = !!opts.overlay;
      clearEl(detailTd);

      var panel = document.createElement('div');
      panel.className = 'doc-detail-panel' + (overlay ? ' is-overlay' : '');

      function driveLink() {
        if (!data.gdrive_url || !/^https:\/\//.test(data.gdrive_url)) return null;
        var gdriveLink = document.createElement('a');
        gdriveLink.href = data.gdrive_url;
        gdriveLink.target = '_blank';
        gdriveLink.rel = 'noopener';
        gdriveLink.className = 'detail-drive-link';
        var gdriveLinkSpan = document.createElement('span');
        gdriveLinkSpan.setAttribute('data-sk', 'Otvoriť v Google Drive');
        gdriveLinkSpan.setAttribute('data-en', 'Open in Google Drive');
        gdriveLinkSpan.textContent = 'Open in Google Drive';
        gdriveLink.appendChild(gdriveLinkSpan);
        return gdriveLink;
      }

      // Close button (the quick look has its own)
      var closeBtn = document.createElement('button');
      closeBtn.className = 'detail-close';
      closeBtn.textContent = '\u00d7';
      closeBtn.title = dashLang === 'sk' ? 'Zavrieť' : 'Close';
      closeBtn.setAttribute('aria-label', closeBtn.title);
      closeBtn.onclick = function(e) { e.stopPropagation(); closeDocDetail(); };
      if (!overlay) panel.appendChild(closeBtn);
      panel.appendChild(buildStepList(doc, data));
      var cols = document.createElement('div');
      cols.className = 'doc-detail-cols';
      panel.appendChild(cols);

      if (overlay) {
        var qlLink = driveLink();
        if (qlLink && opts.linkHost) opts.linkHost.appendChild(qlLink);
        cols.classList.add('single');
      } else if (data.preview_url || data.gdrive_url) {
        // ── Left column: preview (skip if no preview available) ──
        var left = document.createElement('div');
        left.className = 'doc-detail-left';

        if (data.preview_url && data.preview_url.startsWith('https://')) {
          var iframe = document.createElement('iframe');
          iframe.src = data.preview_url;
          iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-popups');
          iframe.setAttribute('allowfullscreen', '');
          iframe.setAttribute('loading', 'lazy');
          left.appendChild(iframe);
        } else {
          var noPreview = document.createElement('div');
          noPreview.className = 'doc-detail-no-preview';
          var noPreviewSpan = document.createElement('span');
          noPreviewSpan.setAttribute('data-sk', 'N\u00e1h\u013ead nie je dostupn\u00fd');
          noPreviewSpan.setAttribute('data-en', 'Preview not available');
          noPreviewSpan.textContent = 'Preview not available';
          noPreview.appendChild(noPreviewSpan);
          left.appendChild(noPreview);
        }

        var leftLink = driveLink();
        if (leftLink) left.appendChild(leftLink);

        cols.appendChild(left);
      } else {
        // No preview — make right column full width
        cols.classList.add('single');
      }

      // ── Right column: OCR + metadata ──
      var right = document.createElement('div');
      right.className = 'doc-detail-right';

      // OCR pages
      if (data.pages && data.pages.length > 0) {
        data.pages.forEach(function(page) {
          var pageDiv = document.createElement('div');
          pageDiv.className = 'doc-ocr-page';

          var header = document.createElement('div');
          header.className = 'doc-ocr-page-header';
          var pgLabel = dashLang === 'sk' ? 'Strana' : 'Page';
          var chLabel = dashLang === 'sk' ? 'znakov' : 'chars';
          header.textContent = pgLabel + ' ' + page.page_number + ' (' + page.char_count + ' ' + chLabel + ')';
          pageDiv.appendChild(header);

          var textDiv = document.createElement('div');
          textDiv.className = 'doc-ocr-text';
          textDiv.textContent = loc(page, 'text');
          pageDiv.appendChild(textDiv);

          right.appendChild(pageDiv);
        });
      } else {
        var noOcr = document.createElement('div');
        noOcr.className = 'doc-ocr-page';
        var noOcrSpan = document.createElement('span');
        noOcrSpan.style.cssText = 'color:var(--text-muted);font-size:0.8125rem';
        noOcrSpan.setAttribute('data-sk', 'Text e\u0161te nie je pre\u010d\u00edtan\u00fd.');
        noOcrSpan.setAttribute('data-en', 'The text has not been read yet.');
        noOcrSpan.textContent = 'The text has not been read yet.';
        noOcr.appendChild(noOcrSpan);
        right.appendChild(noOcr);
      }

      // ── Metadata section ──
      var metaSection = document.createElement('div');
      metaSection.className = 'doc-meta-section';

      function addMetaRow(labelSk, labelEn, value) {
        var row = document.createElement('div');
        row.className = 'doc-meta-row';
        var lbl = document.createElement('span');
        lbl.className = 'doc-meta-label';
        lbl.setAttribute('data-sk', labelSk);
        lbl.setAttribute('data-en', labelEn);
        lbl.textContent = dashLang === 'sk' ? labelSk : labelEn;
        row.appendChild(lbl);
        var val = document.createElement('span');
        val.className = 'doc-meta-value';
        if (typeof value === 'string') {
          val.textContent = value;
        } else {
          val.appendChild(value);
        }
        row.appendChild(val);
        metaSection.appendChild(row);
      }

      // Category badge
      var catBadge = document.createElement('span');
      catBadge.className = 'category-badge';
      var catText = CATEGORY_TEXT[data.category] || CATEGORY_TEXT.other;
      setI18n(catBadge, catText[0], catText[1]);
      addMetaRow('Kateg\u00f3ria', 'Category', catBadge);

      addMetaRow('D\u00e1tum dokumentu', 'Document date', fmtDate(data.document_date));
      addMetaRow('In\u0161tit\u00facia', 'Institution', loc(data, 'institution') || '\u2013');
      addMetaRow('S\u00fabor', 'File', loc(data, 'filename') || '\u2013');
      addMetaRow('ID', 'ID', '#' + data.id);

      // Handwritten badge
      var sm = data.structured_metadata || {};
      if (sm.handwritten) {
        var hwBadge = document.createElement('span');
        hwBadge.className = 'handwritten-badge';
        var hwSpan = document.createElement('span');
        hwSpan.setAttribute('data-sk', 'Ru\u010dne p\u00edsan\u00e9');
        hwSpan.setAttribute('data-en', 'Handwritten');
        hwSpan.textContent = 'Handwritten';
        hwBadge.appendChild(hwSpan);
        addMetaRow('Form\u00e1t', 'Format', hwBadge);
      }

      // AI Summary
      if (data.ai_summary) {
        addMetaRow('Zhrnutie', 'Summary', loc(data, 'ai_summary'));
      }

      // Plain summary
      if (sm.plain_summary) {
        var summaryText = dashLang === 'sk' && sm.plain_summary_sk ? sm.plain_summary_sk : sm.plain_summary;
        addMetaRow('Zhrnutie', 'Summary', summaryText);
      }

      // Providers
      if (sm.providers && sm.providers.length > 0) {
        var provTags = document.createElement('span');
        provTags.className = 'doc-meta-tags';
        sm.providers.forEach(function(p) {
          var tag = document.createElement('span');
          tag.className = 'doc-meta-tag';
          tag.textContent = typeof p === 'string' ? p : (p.name || JSON.stringify(p));
          provTags.appendChild(tag);
        });
        addMetaRow('Poskytovatelia', 'Providers', provTags);
      }

      // Doctors
      if (sm.doctors && sm.doctors.length > 0) {
        var docTags = document.createElement('span');
        docTags.className = 'doc-meta-tags';
        sm.doctors.forEach(function(dr) {
          var tag = document.createElement('span');
          tag.className = 'doc-meta-tag';
          tag.textContent = typeof dr === 'string' ? dr : (dr.name || JSON.stringify(dr));
          docTags.appendChild(tag);
        });
        addMetaRow('Lek\u00e1ri', 'Doctors', docTags);
      }

      // Diagnoses
      if (sm.diagnoses && sm.diagnoses.length > 0) {
        var diagTags = document.createElement('span');
        diagTags.className = 'doc-meta-tags';
        sm.diagnoses.forEach(function(dx) {
          var tag = document.createElement('span');
          tag.className = 'doc-meta-tag';
          tag.textContent = typeof dx === 'string' ? dx : (dx.code ? dx.code + (dx.name ? ': ' + dx.name : '') : JSON.stringify(dx));
          diagTags.appendChild(tag);
        });
        addMetaRow('Diagn\u00f3zy', 'Diagnoses', diagTags);
      }

      // Medications
      if (sm.medications && sm.medications.length > 0) {
        var medTags = document.createElement('span');
        medTags.className = 'doc-meta-tags';
        sm.medications.forEach(function(m) {
          var tag = document.createElement('span');
          tag.className = 'doc-meta-tag';
          tag.textContent = typeof m === 'string' ? m : (m.name || JSON.stringify(m));
          medTags.appendChild(tag);
        });
        addMetaRow('Lieky', 'Medications', medTags);
      }

      // Findings
      if (sm.findings && sm.findings.length > 0) {
        addMetaRow('N\u00e1lezy', 'Findings', sm.findings.join(', '));
      }

      // Tags
      if (data.ai_tags && data.ai_tags.length > 0) {
        var tagContainer = document.createElement('span');
        tagContainer.className = 'doc-meta-tags';
        data.ai_tags.forEach(function(t) {
          var tag = document.createElement('span');
          tag.className = 'doc-meta-tag';
          tag.textContent = t;
          tagContainer.appendChild(tag);
        });
        addMetaRow('\u0160t\u00edtky', 'Tags', tagContainer);
      }

      right.appendChild(metaSection);
      cols.appendChild(right);

      detailTd.appendChild(panel);
      applyDashLang();
    }

    // ── Previews, grid and quick look (#948) ─────────────────────────────
    /* Previews come from GET /api/doc-thumbnails?ids=…&size=s|l (the demo answers
       from its own data): only for tiles near the viewport, up to 24 ids per call,
       ONE request at a time (the server holds one DB connection), kept for the
       page's lifetime and dropped when the patient changes. A document without a
       preview (null) keeps its flat file-type mark: never an emoji, never a
       broken image. */
    var THUMB_BATCH = 24;
    var DOC_VIEW_KEY = 'oncofiles_doc_view';
    var SVG_NS = 'http://www.w3.org/2000/svg';
    var docView = readDocView();       // list | grid, remembered per browser
    var thumbCache = new Map();        // 'id:s' | 'id:l' -> {src, src_en?} | null (no preview)
    var thumbWanted = [];              // small previews to ask for, in order
    var thumbInFlight = [];            // cache keys ('id:size') of the one request under way
    var thumbLargeWanted = null;       // the one large preview the quick look waits for
    var thumbBusy = false;
    var thumbGen = 0;                  // bumped when the patient changes: late answers are dropped
    var thumbsOff = false;             // the route is not there (404): marks only
    var thumbPumpTimer = null;
    var thumbObserver = null;
    var quickLook = { id: null, opener: null, details: {} };
    var IMAGE_EXT = { jpg: 1, jpeg: 1, png: 1, gif: 1, webp: 1, heic: 1, heif: 1, tif: 1, tiff: 1, bmp: 1 };
    var TEXT_DOC_EXT = { doc: 1, docx: 1, odt: 1, rtf: 1, txt: 1, md: 1, pages: 1 };
    var SORT_DIR_TEXT = {
      date: [['od najstarších', 'oldest first'], ['od najnovších', 'newest first']],
      text: [['A–Z', 'A–Z'], ['Z–A', 'Z–A']],
      stage: [['najprv vyžadujúce pozornosť', 'needs attention first'], ['najprv pripravené', 'ready first']]
    };

    function readDocView() {
      try { return localStorage.getItem(DOC_VIEW_KEY) === 'grid' ? 'grid' : 'list'; }
      catch (e) { return 'list'; }
    }

    function findDoc(id) {
      for (var i = 0; i < docsData.length; i++) if (String(docsData[i].id) === String(id)) return docsData[i];
      return null;
    }

    function indexOfDoc(list, id) {
      for (var i = 0; i < list.length; i++) if (String(list[i].id) === String(id)) return i;
      return -1;
    }

    function thumbKey(id, size) { return String(id) + ':' + size; }

    /** pdf | image | doc | other, from the file name's extension (rows carry no mime type). */
    function fileKind(d) {
      var m = /\.([a-z0-9]{1,5})$/i.exec(String(d.filename || ''));
      var ext = m ? m[1].toLowerCase() : '';
      if (ext === 'pdf') return { kind: 'pdf', label: 'PDF' };
      if (IMAGE_EXT[ext]) return { kind: 'image', label: ext === 'jpeg' ? 'JPG' : ext.toUpperCase().slice(0, 4) };
      if (TEXT_DOC_EXT[ext]) return { kind: 'doc', label: ext === 'docx' ? 'DOC' : ext.toUpperCase().slice(0, 4) };
      return { kind: 'other', label: ext ? ext.toUpperCase().slice(0, 4) : '' };
    }

    function svgEl(tag, attrs) {
      var el = document.createElementNS(SVG_NS, tag);
      Object.keys(attrs).forEach(function (k) { el.setAttribute(k, attrs[k]); });
      return el;
    }

    /** The flat file-type mark: shown while a preview loads, and for good when there is none. */
    function typeMark(d) {
      var info = fileKind(d);
      var svg = svgEl('svg', { viewBox: '0 0 40 52', 'class': 'type-mark type-' + info.kind, 'aria-hidden': 'true', focusable: 'false' });
      if (info.kind === 'image') {
        svg.appendChild(svgEl('rect', { x: 7, y: 9, width: 26, height: 20, rx: 2, 'class': 'tm-frame' }));
        svg.appendChild(svgEl('circle', { cx: 26, cy: 15, r: 2.6, 'class': 'tm-sun' }));
        svg.appendChild(svgEl('path', { d: 'M8.5 27.5l7-8.5 5 6 3-3.5 8 6z', 'class': 'tm-hill' }));
      } else {
        [[9, 24], [14, 18], [19, 22], [24, 14]].forEach(function (l) {
          svg.appendChild(svgEl('rect', { x: 8, y: l[0], width: l[1], height: 2.2, rx: 1.1, 'class': 'tm-line' }));
        });
      }
      if (info.label) {
        svg.appendChild(svgEl('rect', { x: 8, y: 34, width: 24, height: 11, rx: 2.5, 'class': 'tm-badge' }));
        var label = svgEl('text', { x: 20, y: 42.1, 'text-anchor': 'middle', 'class': 'tm-label' });
        label.textContent = info.label;
        svg.appendChild(label);
      }
      return svg;
    }

    /** The picture of a preview; if it cannot be drawn, the file-type mark takes its place. */
    function thumbImg(entry, d) {
      var img = document.createElement('img');
      img.alt = '';
      img.decoding = 'async';
      img.addEventListener('error', function () {
        var box = img.parentNode;
        if (box) { box.replaceChild(typeMark(d), img); box.classList.remove('has-img'); }
      });
      img.src = loc(entry, 'src');
      return img;
    }

    /** A preview tile: the cached picture, else the file-type mark (asked for when it scrolls near). */
    function buildThumb(d) {
      var box = document.createElement('span');
      box.className = 'doc-thumb';
      box.setAttribute('data-thumb-id', String(d.id));
      var entry = hasPreview(d) ? thumbCache.get(thumbKey(d.id, 's')) : null;  // inbox: the mark for good
      if (entry) {
        box.appendChild(thumbImg(entry, d));
        box.classList.add('has-img');
      } else {
        box.appendChild(typeMark(d));
        if (entry === undefined && !thumbsOff) observeThumb(box);
      }
      return box;
    }

    function observeThumb(box) {
      if (typeof IntersectionObserver === 'undefined') { wantThumb(box.getAttribute('data-thumb-id')); return; }
      if (!thumbObserver) {
        thumbObserver = new IntersectionObserver(function (entries, observer) {
          entries.forEach(function (en) {
            if (!en.isIntersecting) return;
            observer.unobserve(en.target);
            wantThumb(en.target.getAttribute('data-thumb-id'));
          });
        }, { rootMargin: '320px 0px' });
      }
      thumbObserver.observe(box);
    }

    function resetThumbObserver() {
      if (thumbObserver) { thumbObserver.disconnect(); thumbObserver = null; }
    }

    function wantThumb(id) {
      id = String(id);
      if (!/^\d+$/.test(id)) return;  // only integer document ids go to /api/doc-thumbnails
      if (thumbsOff || thumbCache.has(thumbKey(id, 's'))) return;
      if (thumbWanted.indexOf(id) !== -1 || thumbInFlight.indexOf(thumbKey(id, 's')) !== -1) return;
      thumbWanted.push(id);
      scheduleThumbPump();
    }

    function scheduleThumbPump() {
      if (thumbPumpTimer) return;
      // a short pause collects the tiles of one scroll into one request
      thumbPumpTimer = setTimeout(function () { thumbPumpTimer = null; pumpThumbs(); }, 40);
    }

    function isPreview(entry) {
      function ok(src) { return typeof src === 'string' && src.indexOf('data:image/') === 0; }
      return !!entry && ok(entry.src) && (entry.src_en == null || ok(entry.src_en));
    }

    /** One request at a time: the quick look's large preview first, else the next small batch. */
    async function pumpThumbs() {
      if (thumbBusy || thumbsOff) return;
      var size, ids;
      if (thumbLargeWanted !== null) { size = 'l'; ids = [thumbLargeWanted]; thumbLargeWanted = null; }
      else if (thumbWanted.length) { size = 's'; ids = thumbWanted.splice(0, THUMB_BATCH); }
      else return;
      thumbBusy = true;
      thumbInFlight = ids.map(function (id) { return thumbKey(id, size); });
      var gen = thumbGen;
      var answer = {};
      try {
        var data = await apiFetch('/api/doc-thumbnails?ids=' + ids.map(encodeURIComponent).join(',') + '&size=' + size);
        answer = (data && data.thumbnails) || {};
      } catch (e) {
        if (e && e.status === 404) thumbsOff = true;  // marks stay; nothing is asked again
      }
      thumbBusy = false;
      thumbInFlight = [];
      if (gen === thumbGen) {
        ids.forEach(function (id) {
          var entry = answer[id];
          thumbCache.set(thumbKey(id, size), isPreview(entry) ? entry : null);
          if (size === 's') applyThumb(id);
          else if (quickLook.id !== null && String(quickLook.id) === id) showLargePreview();
        });
      }
      pumpThumbs();
    }

    /** A preview arrived: swap it into every tile of that document once it is decoded. */
    function applyThumb(id) {
      var entry = thumbCache.get(thumbKey(id, 's'));
      var d = findDoc(id);
      if (!entry || !d) return;  // null: the file-type mark stays
      document.querySelectorAll('.doc-thumb[data-thumb-id]').forEach(function (box) {
        if (box.getAttribute('data-thumb-id') !== String(id) || box.querySelector('img')) return;
        var img = thumbImg(entry, d);
        var swap = function () { clearEl(box); box.appendChild(img); box.classList.add('has-img'); };
        if (img.decode) img.decode().then(swap, function () { thumbCache.set(thumbKey(id, 's'), null); });
        else swap();
      });
      if (quickLook.id !== null && String(quickLook.id) === String(id)) showLargePreview();
    }

    function resetThumbs() {
      thumbGen += 1;
      thumbCache.clear();
      thumbWanted = [];
      thumbLargeWanted = null;
      thumbsOff = false;
      resetThumbObserver();
    }

    // ── Grid view ──

    function setDocView(view) {
      docView = view === 'grid' ? 'grid' : 'list';
      try { localStorage.setItem(DOC_VIEW_KEY, docView); } catch (e) { /* no storage: the choice lasts this page */ }
      updateDocViewControls();
      renderTable();
    }

    function updateDocViewControls() {
      var grid = docView === 'grid';
      [['view-btn-list', !grid], ['view-btn-grid', grid]].forEach(function (pair) {
        var btn = document.getElementById(pair[0]);
        if (!btn) return;
        btn.classList.toggle('is-active', pair[1]);
        btn.setAttribute('aria-pressed', pair[1] ? 'true' : 'false');
      });
      var tableWrap = document.getElementById('doc-table-wrap');
      var gridWrap = document.getElementById('doc-grid-wrap');
      var legend = document.querySelector('.steps-legend');  // explains the list's step marks
      if (tableWrap) tableWrap.hidden = grid;
      if (gridWrap) gridWrap.hidden = !grid;
      if (legend) legend.hidden = grid;
      syncGridSort();
    }

    /** The grid's "sorted by" line follows the table's sort column and direction. */
    function syncGridSort() {
      var select = document.getElementById('grid-sort-col');
      if (select) select.value = sortCol;
      var arrow = document.getElementById('grid-sort-arrow');
      if (arrow) arrow.textContent = sortAsc ? '▲' : '▼';
      var label = document.getElementById('grid-sort-dir-label');
      if (label) {
        var kind = (sortCol === 'date' || sortCol === 'uploaded_at') ? 'date' : sortCol === 'stage_rank' ? 'stage' : 'text';
        var text = SORT_DIR_TEXT[kind][sortAsc ? 0 : 1];
        setI18n(label, text[0], text[1]);
      }
    }

    function defaultSortAsc(col) { return !(col === 'uploaded_at' || col === 'date'); }

    /** One sort for the table and the grid: header arrows, the grid line, the list. */
    function setSort(col, asc) {
      sortCol = col;
      sortAsc = asc;
      document.querySelectorAll('th[data-sort]').forEach(function (th) {
        var on = th.dataset.sort === col;
        if (on) th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        else th.removeAttribute('aria-sort');
        th.querySelector('.sort-arrow').textContent = on ? (asc ? '▲' : '▼') : '';
      });
      syncGridSort();
      renderTable();
    }

    function setGridSort(col) { setSort(col, defaultSortAsc(col)); }
    function toggleSortDir() { setSort(sortCol, !sortAsc); }

    function renderGrid(sorted) {
      var grid = document.getElementById('doc-grid');
      clearEl(grid);
      sorted.forEach(function (d) { grid.appendChild(buildDocCard(d)); });
    }

    /** A card: preview, name (two lines), category and date, status shape and label. */
    function buildDocCard(d) {
      var li = document.createElement('li');
      li.className = 'doc-card-item';
      var card = document.createElement('button');
      card.type = 'button';
      card.className = 'doc-card is-' + stageKind(d);
      card.setAttribute('data-doc-id', String(d.id));
      card.setAttribute('aria-haspopup', 'dialog');
      var name = loc(d, 'filename');
      card.title = name;

      var shot = document.createElement('span');
      shot.className = 'doc-card-shot';
      shot.appendChild(buildThumb(d));
      card.appendChild(shot);

      var body = document.createElement('span');
      body.className = 'doc-card-body';
      body.appendChild(filenameText(name, 'doc-card-name'));
      var meta = document.createElement('span');
      meta.className = 'doc-card-meta';
      meta.appendChild(d.category ? categoryBadge(d.category) : noCategoryMark());
      var date = document.createElement('span');
      date.className = 'doc-card-date';
      date.textContent = fmtDate(d.date);
      meta.appendChild(date);
      body.appendChild(meta);
      var foot = document.createElement('span');
      foot.className = 'doc-card-stage';
      foot.appendChild(stageChip(d, true));  // the steps are in the quick look's detail
      body.appendChild(foot);
      card.appendChild(body);

      card.addEventListener('click', function () { openQuickLook(d.id, card); });
      li.appendChild(card);
      return li;
    }

    // ── Quick look: the first page large, the document detail beside it ──

    function openQuickLook(id, opener) {
      var d = findDoc(id);
      var overlay = document.getElementById('quicklook');
      if (!d || !overlay) return;
      var wasOpen = quickLook.id !== null;
      quickLook.id = d.id;
      if (!wasOpen) {
        quickLook.opener = opener || document.activeElement;
        overlay.hidden = false;
        lockPageScroll(true);
      }
      renderQuickLook();
      if (!wasOpen) document.getElementById('ql-panel').focus();
    }

    function renderQuickLook() {
      var d = findDoc(quickLook.id);
      if (!d) return;
      renderQuickLookNav();
      showLargePreview();
      renderQuickLookDetail(d);
    }

    /** Title, category and date, position in the list, previous / next. */
    function renderQuickLookNav() {
      var d = findDoc(quickLook.id);
      if (!d) return;
      document.getElementById('ql-title').textContent = loc(d, 'filename');
      var meta = document.getElementById('ql-meta');
      clearEl(meta);
      meta.appendChild(d.category ? categoryBadge(d.category) : noCategoryMark());
      var date = document.createElement('span');
      date.className = 'ql-date';
      date.textContent = fmtDate(d.date);
      meta.appendChild(date);
      meta.appendChild(stageChip(d));
      var list = docListView().sorted;
      var idx = indexOfDoc(list, d.id);
      document.getElementById('ql-pos').textContent = idx >= 0 ? (idx + 1) + ' / ' + list.length : '';
      var prev = document.getElementById('ql-prev');
      var next = document.getElementById('ql-next');
      prev.disabled = idx <= 0;
      next.disabled = idx < 0 || idx >= list.length - 1;
      // a button that just became disabled must not keep (and lose) the focus
      if (document.activeElement === prev || document.activeElement === next) {
        if (document.activeElement.disabled) document.getElementById('ql-panel').focus();
      }
    }

    /** The large first page; until it arrives, the small preview or the file-type mark. */
    function showLargePreview() {
      var d = findDoc(quickLook.id);
      var page = document.getElementById('ql-page');
      if (!d || !page) return;
      var preview = hasPreview(d);  // an inbox row shows its file-type mark as the page
      var large = preview ? thumbCache.get(thumbKey(d.id, 'l')) : null;
      var entry = large || (preview ? thumbCache.get(thumbKey(d.id, 's')) : null);
      clearEl(page);
      if (entry) {
        var img = thumbImg(entry, d);
        img.alt = (dashLang === 'sk' ? 'Prvá strana: ' : 'First page: ') + loc(d, 'filename');
        page.appendChild(img);
      } else {
        page.appendChild(typeMark(d));
      }
      page.classList.toggle('has-img', !!entry);
      var waiting = large === undefined && !thumbsOff;
      page.classList.toggle('is-loading', waiting);
      if (waiting && thumbInFlight.indexOf(thumbKey(d.id, 'l')) === -1) {
        thumbLargeWanted = String(d.id);  // only the latest: arrows held down ask for one page
        scheduleThumbPump();
      }
    }

    function renderQuickLookDetail(d) {
      var host = document.getElementById('ql-detail');
      var links = document.getElementById('ql-links');
      clearEl(links);
      if (d.inbox) { renderInboxDetail(d, host, { overlay: true }); return; }  // nothing to fetch
      var cached = quickLook.details[d.id];
      if (cached) { renderDocDetail(d, cached, host, { overlay: true, linkHost: links }); return; }
      clearEl(host);
      var loading = document.createElement('div');
      loading.className = 'doc-detail-loading';
      loading.appendChild(setI18n(document.createElement('span'), 'Načítavam detail…', 'Loading detail…'));
      host.appendChild(loading);
      var id = d.id;
      function failed() {
        if (quickLook.id !== id) return;
        clearEl(loading);
        loading.appendChild(setI18n(document.createElement('span'), 'Detail sa nepodarilo načítať.', 'The detail could not be loaded.'));
      }
      apiFetch('/api/doc-detail/' + id).then(function (data) {
        if (!data || data.error) { failed(); return; }
        quickLook.details[id] = data;
        if (quickLook.id === id) renderDocDetail(findDoc(id) || d, data, host, { overlay: true, linkHost: links });
      }).catch(failed);
    }

    /** Left / Right: the previous or next document of the list as it is filtered and sorted now. */
    function quickLookStep(delta) {
      if (quickLook.id === null) return;
      var list = docListView().sorted;
      var idx = indexOfDoc(list, quickLook.id);
      var next = idx < 0 ? null : list[idx + Number(delta)];
      if (next) openQuickLook(next.id);
    }

    function closeQuickLook() {
      var overlay = document.getElementById('quicklook');
      if (!overlay || quickLook.id === null) return;
      var lastId = quickLook.id;
      var opener = quickLook.opener;
      quickLook = { id: null, opener: null, details: {} };
      thumbLargeWanted = null;
      overlay.hidden = true;
      ['ql-page', 'ql-detail', 'ql-links'].forEach(function (elId) { clearEl(document.getElementById(elId)); });
      lockPageScroll(false);
      // Focus goes back where the person was: the tile or card of the document
      // shown last (the opener itself unless they moved on with the arrows).
      var sel = docView === 'grid' ? '#doc-grid .doc-card' : '#doc-tbody .thumb-btn';
      var target = findById(sel, 'data-doc-id', lastId);
      if (!target && opener && opener.isConnected) target = opener;
      if (target) target.focus();
    }

    function lockPageScroll(on) {
      var root = document.documentElement;
      if (on) {
        var gap = window.innerWidth - root.clientWidth;  // keep the page from shifting
        root.classList.add('ql-lock');
        if (gap > 0) document.body.style.paddingRight = gap + 'px';
      } else {
        root.classList.remove('ql-lock');
        document.body.style.paddingRight = '';
      }
    }

    /** Tab stays inside the open quick look. */
    function trapQuickLookFocus(e) {
      var panel = document.getElementById('ql-panel');
      var items = Array.prototype.filter.call(
        panel.querySelectorAll('button, a[href], [tabindex]:not([tabindex="-1"])'),
        function (el) { return !el.disabled && el.offsetParent !== null; });
      if (!items.length) { e.preventDefault(); panel.focus(); return; }
      var first = items[0];
      var last = items[items.length - 1];
      var active = document.activeElement;
      if (e.shiftKey && (active === first || active === panel || !panel.contains(active))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (active === last || !panel.contains(active))) { e.preventDefault(); first.focus(); }
    }

    document.addEventListener('keydown', function (e) {
      if (quickLook.id === null) return;
      if (e.key === 'Tab') { trapQuickLookFocus(e); return; }
      if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        e.preventDefault();
        quickLookStep(e.key === 'ArrowLeft' ? -1 : 1);
      }
    });

    updateDocViewControls();

    // ── One event log ────────────────────────────────────────────────────

    var activityData = null;
    var activityShowAll = false;
    var ACTIVITY_SHORT = 6;
    var TRIGGER_TEXT = {
      auto: ['automaticky', 'automatic'], nightly: ['nočná kontrola', 'nightly check'],
      manual: ['ručne', 'manual'], admin: ['ručne', 'manual'], scheduled: ['podľa plánu', 'scheduled'],
      startup: ['po štarte servera', 'after server start']
    };

    function activityText(ev) {
      var c = ev.counts || {};
      if (ev.kind === 'drive_sync') {
        if (ev.status === 'failed') return ['Synchronizácia s Google Drive zlyhala', 'Google Drive sync failed'];
        var extra = c.errors ? [', chyby: ' + c.errors, ', errors: ' + c.errors] : ['', ''];
        return c.new
          ? ['Synchronizácia s Google Drive: nové dokumenty ' + c.new + extra[0], 'Google Drive sync: new documents ' + c.new + extra[1]]
          : ['Synchronizácia s Google Drive: nič nové' + extra[0], 'Google Drive sync: nothing new' + extra[1]];
      }
      if (ev.kind === 'text_reread') {
        if (ev.status === 'running') return ['Práve sa znova číta text dokumentov', 'Re-reading document text now'];
        if (ev.status !== 'ok') return ['Opätovné čítanie textu sa nedokončilo', 'Re-reading document text did not finish'];
        var parts = [[], []];
        if (c.rewritten) { parts[0].push('lepší text uložený ' + c.rewritten); parts[1].push('better text saved ' + c.rewritten); }
        if (c.kept) { parts[0].push('pôvodný text ponechaný ' + c.kept); parts[1].push('original text kept ' + c.kept); }
        if (!parts[0].length) return ['Opätovné čítanie textu: bez zmeny', 'Text re-read: no change'];
        return ['Opätovné čítanie textu: ' + parts[0].join(', '), 'Text re-read: ' + parts[1].join(', ')];
      }
      if (ev.status === 'running') return ['Práve prebieha spracovanie priečinka', 'Folder processing is running now'];
      if (ev.status === 'failed') return ['Spracovanie priečinka sa nedokončilo', 'Folder processing did not finish'];
      if (ev.status === 'interrupted') return ['Spracovanie bolo prerušené; dokončí sa pri ďalšom behu', 'Processing was interrupted; the next run finishes it'];
      if (!c.documents) return ['Kontrola priečinka: nič nové', 'Folder check: nothing new'];
      // only what happened: zero counts are left out
      var sk = [], en = [];
      if (c.filed) { sk.push('zaradené ' + c.filed); en.push('filed ' + c.filed); }
      if (c.review) { sk.push('na vašu kontrolu ' + c.review); en.push('for your review ' + c.review); }
      if (c.not_processed) { sk.push('nedokončené ' + c.not_processed); en.push('not finished ' + c.not_processed); }
      return ['Spracované dokumenty: ' + c.documents + (sk.length ? ' (' + sk.join(', ') + ')' : ''),
        'Documents processed: ' + c.documents + (en.length ? ' (' + en.join(', ') + ')' : '')];
    }

    function renderActivity(data) {
      if (data) activityData = data;
      var box = document.getElementById('activity-rows');
      var more = document.getElementById('activity-more');
      var note = document.getElementById('pending-note');
      if (!box) return;
      clearEl(box);
      var events = (activityData && activityData.events) || [];

      var inbox = activityData && activityData.pending ? activityData.pending.inbox : null;
      if (note) {
        note.hidden = !(inbox > 0);
        if (inbox > 0) {
          clearEl(note);
          note.appendChild(stateMark('progress'));
          note.appendChild(setI18n(document.createElement('span'),
            'Nové súbory v priečinku, ktoré čakajú na spracovanie: ' + inbox + '.',
            'New files in the folder waiting to be processed: ' + inbox + '.'));
        }
      }

      if (events.length === 0) {
        var empty = document.createElement('div');
        empty.className = 'activity-empty';
        setI18n(empty, activityData ? 'Zatiaľ žiadne spracovanie.' : 'Záznam sa nepodarilo načítať.',
          activityData ? 'No processing yet.' : 'The log could not be loaded.');
        box.appendChild(empty);
        if (more) more.hidden = true;
        return;
      }
      var shown = activityShowAll ? events : events.slice(0, ACTIVITY_SHORT);
      shown.forEach(function(ev) {
        var row = document.createElement('div');
        row.className = 'activity-row';
        var kind = ev.status === 'ok' ? 'ready' : ev.status === 'running' ? 'progress' : 'attention';
        row.appendChild(stateMark(kind));
        var time = document.createElement('span');
        time.className = 'activity-time';
        time.textContent = fmtDateTime(ev.at);
        row.appendChild(time);
        var text = activityText(ev);
        var main = setI18n(document.createElement('span'), text[0], text[1]);
        main.className = 'activity-text';
        row.appendChild(main);
        var trig = TRIGGER_TEXT[ev.trigger];
        if (trig) {
          var t = setI18n(document.createElement('span'), trig[0], trig[1]);
          t.className = 'activity-meta';
          row.appendChild(t);
        }
        box.appendChild(row);
      });
      if (more) {
        more.hidden = events.length <= ACTIVITY_SHORT;
        setI18n(more, activityShowAll ? 'Zobraziť menej' : 'Zobraziť staršie (' + (events.length - ACTIVITY_SHORT) + ')',
          activityShowAll ? 'Show fewer' : 'Show older (' + (events.length - ACTIVITY_SHORT) + ')');
      }
    }

    function toggleActivityAll() {
      activityShowAll = !activityShowAll;
      renderActivity(null);
    }

    // ── Administration (admin sessions only; a panel loads when opened) ──

    var adminOpen = {};

    function updateAdminArea() {
      // The manual Drive sync is an admin-only trigger on the server (#852):
      // nobody else gets a button that can only refuse.
      var syncBtn = document.getElementById('ip-sync-btn');
      if (syncBtn) syncBtn.style.display = isGlobalAdmin ? '' : 'none';
      var area = document.getElementById('admin-area');
      if (!area) return;
      area.style.display = isGlobalAdmin ? 'block' : 'none';
      if (!isGlobalAdmin) return;
      Object.keys(adminOpen).forEach(function(key) { if (adminOpen[key]) loadAdminPanel(key); });
    }

    function toggleAdminPanel(key, headEl) {
      var body = document.getElementById('admin-body-' + key);
      if (!body) return;
      var open = body.hidden;
      body.hidden = !open;
      if (headEl) headEl.setAttribute('aria-expanded', open ? 'true' : 'false');
      adminOpen[key] = open;
      if (open) loadAdminPanel(key);
    }

    async function loadAdminPanel(key) {
      if (!isGlobalAdmin) return;
      try {
        if (key === 'curated') { await loadCuratedOps(); return; }
        if (key === 'reread') { await loadRereadConsole(); return; }
        if (key === 'inbound') {
          var unmatched = await apiFetch('/api/inbound-unmatched');
          if (unmatched) renderInboundUnmatched(unmatched);
          return;
        }
        if (key === 'schedules') {
          var sched = await apiFetch('/api/schedules');
          if (sched) renderSchedules(sched);
          return;
        }
        if (key === 'ai') {
          // sequential: one DB connection on the server
          var analytics = await apiFetch('/api/usage-analytics');
          if (analytics) renderAnalytics(analytics);
          var leaderboard = await apiFetch('/api/cost-leaderboard?days=30&limit=50');
          if (leaderboard) renderCostLeaderboard(leaderboard);
          var prompts = await apiFetch('/api/prompt-log?limit=50');
          if (prompts) renderPromptLog(prompts);
        }
      } catch (e) {
        console.warn('Admin panel failed to load:', key, e);
      }
    }

    // ── Processing Schedules (#287) ──────────────────────────────────
    var schedFilter = 'all';
    var schedData = null;

    var schedGroupsEl = document.getElementById('sched-groups');  // admin markup; absent on the demo page
    if (schedGroupsEl) schedGroupsEl.addEventListener('click', function(e) {
      if (!e.target.classList.contains('pill')) return;
      e.target.parentElement.querySelectorAll('.pill').forEach(function(p) { p.classList.remove('active'); });
      e.target.classList.add('active');
      schedFilter = e.target.dataset.schedFilter;
      if (schedData) renderSchedules(schedData);
    });

    // Why a job (or the whole scheduler / the curated loop) is not active here.
    var SCHED_REASONS = {
      sync_disabled: ['Plánovač je na tomto serveri vypnutý', 'The scheduler is off on this server'],
      scheduler_not_running: ['Plánovač nebeží', 'The scheduler is not running'],
      ai_nightly_only: ['AI beží len v noci; úloha patrí do starého režimu', 'AI runs nightly only; this job belongs to the legacy mode'],
      legacy_mode: ['Starý režim (AI nie je len nočné)', 'Legacy mode (AI is not nightly-only)'],
      run_on_startup_off: ['Jednorazový beh po štarte je vypnutý', 'The one-off run after start is off'],
      gmail_calendar_disabled: ['Gmail a kalendár sú vypnuté', 'Gmail and Calendar are off'],
      integrity_check_off: ['Kontrola integrity je vypnutá', 'The integrity check is off'],
      not_registered: ['Na tomto serveri nie je zaregistrovaná', 'Not registered on this server'],
      curated_disabled: ['Kurátorská slučka je vypnutá', 'The curated loop is off'],
      google_disabled: ['Google integrácia je vypnutá', 'Google integration is off'],
      not_running: ['Nebeží', 'Not running']
    };

    /** A server timestamp in the viewer's local time, as [sk, en]. */
    function schedTimeBoth(value) {
      var d = parseTs(value);
      if (!d) return ['–', '–'];
      var opts = { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' };
      return [d.toLocaleString('sk-SK', opts), d.toLocaleString('en-GB', opts)];
    }

    function schedReasonText(code, sw) {
      var r = SCHED_REASONS[code] || [code || '', code || ''];
      var suffix = sw ? ' (' + sw + ')' : '';
      return [r[0] + suffix, r[1] + suffix];
    }

    /** How often a job runs, as [sk, en]; daily/weekly times from next_run_at in local time. */
    function schedCadence(j) {
      var t = j.trigger;
      if (!t) return ['jednorazovo, už prebehla', 'once, already ran'];
      if (t.kind === 'interval' && t.seconds) {
        if (t.seconds % 3600 === 0) {
          var h = t.seconds / 3600;
          return h === 1 ? ['každú hodinu', 'every hour'] : ['každých ' + h + ' h', 'every ' + h + ' h'];
        }
        var m = Math.round(t.seconds / 60);
        return ['každých ' + m + ' min', 'every ' + m + ' min'];
      }
      if (t.kind === 'date') return ['jednorazovo', 'once'];
      var next = parseTs(j.next_run_at);
      if (t.kind === 'cron' && next && (t.cadence === 'daily' || t.cadence === 'weekly')) {
        var tOpts = { hour: '2-digit', minute: '2-digit' };
        var skT = next.toLocaleTimeString('sk-SK', tOpts), enT = next.toLocaleTimeString('en-GB', tOpts);
        if (t.cadence === 'daily') return ['denne o ' + skT, 'daily at ' + enT];
        return [next.toLocaleDateString('sk-SK', { weekday: 'long' }) + ' o ' + skT,
                next.toLocaleDateString('en-GB', { weekday: 'long' }) + ' at ' + enT];
      }
      return [t.text || '', t.text || ''];
    }

    function schedLine(parent, sk, en, cls) {
      var el = document.createElement('div');
      if (cls) el.className = cls;
      el.style.cssText = 'font-size:0.7rem;color:var(--text-muted);margin-top:0.125rem';
      setI18n(el, sk, en);
      parent.appendChild(el);
      return el;
    }

    function renderSchedHeader(container, data) {
      var s = data.scheduler || {};
      var head = document.createElement('div');
      head.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.5rem';
      head.appendChild(stateMark(s.running ? 'ready' : 'attention'));
      var txt = document.createElement('span');
      if (s.running) {
        var since = schedTimeBoth(s.started_at);
        setI18n(txt, 'Plánovač beží od ' + since[0] + ' · úloh: ' + (s.job_count || 0),
                'Scheduler running since ' + since[1] + ' · jobs: ' + (s.job_count || 0));
      } else {
        var off = s.off_reason || {};
        var r = schedReasonText(off.reason || 'scheduler_not_running', off.switch);
        setI18n(txt, r[0], r[1]);
      }
      head.appendChild(txt);
      container.appendChild(head);
    }

    function renderCuratedLoop(container, c) {
      if (!c) return;
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid var(--border)';
      var mark = stateMark(c.running ? 'ready' : 'attention');
      mark.style.marginTop = '0.25rem';
      row.appendChild(mark);
      var info = document.createElement('div');
      info.style.cssText = 'flex:1;min-width:0';
      var nameEl = document.createElement('div');
      nameEl.style.cssText = 'font-weight:600;color:var(--text);font-size:0.8125rem';
      setI18n(nameEl, (c.name || {}).sk || 'Kurátorská slučka', (c.name || {}).en || 'Curated loop');
      info.appendChild(nameEl);
      schedLine(info, (c.desc || {}).sk || '', (c.desc || {}).en || '');
      if (!c.running) {
        var r = schedReasonText(c.reason, c.switch);
        schedLine(info, r[0], r[1]);
      } else {
        var mode = c.continuous
          ? ['priebežne: kontrola každých ' + c.poll_s + ' s, ' + c.chunk_docs + ' dok./beh',
             'continuous: probe every ' + c.poll_s + ' s, ' + c.chunk_docs + ' docs/run']
          : ['priebežný režim vypnutý', 'continuous mode off'];
        var sweep = c.nightly_hour == null
          ? ['súhrnný beh každú hodinu', 'sweep every hour']
          : ['nočný súhrnný beh o ' + c.nightly_hour + ':00 (Bratislava)', 'nightly sweep at ' + c.nightly_hour + ':00 (Bratislava)'];
        schedLine(info, mode[0] + ' · ' + sweep[0], mode[1] + ' · ' + sweep[1]);
        var probe = schedTimeBoth(c.last_probe_at);
        var auto = schedTimeBoth(c.last_auto_run_at);
        schedLine(info,
          'posledná kontrola: ' + probe[0] + ' · kontrol: ' + c.probes_total + ' · pacientov: ' + c.patients_probed,
          'last probe: ' + probe[1] + ' · probes: ' + c.probes_total + ' · patients: ' + c.patients_probed);
        schedLine(info,
          'automatických behov: ' + c.auto_runs_total + ' (posledný ' + auto[0] + ') · čaká v inboxe: ' + c.pending_inbox_total + ' · zablokovaných: ' + c.runs_blocked_total,
          'auto runs: ' + c.auto_runs_total + ' (last ' + auto[1] + ') · pending in inbox: ' + c.pending_inbox_total + ' · blocked: ' + c.runs_blocked_total);
      }
      row.appendChild(info);
      container.appendChild(row);
    }

    function renderSchedJob(container, j) {
      var row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:flex-start;gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid var(--border)';
      var kind = j.running ? 'progress' : ((j.last_error && (!j.last_ok || j.last_error > j.last_ok)) ? 'attention' : (j.last_ok ? 'ready' : 'progress'));
      var mark = stateMark(kind);
      mark.style.marginTop = '0.25rem';
      row.appendChild(mark);

      var info = document.createElement('div');
      info.style.cssText = 'flex:1;min-width:0';
      var nameEl = document.createElement('div');
      nameEl.style.cssText = 'font-weight:600;color:var(--text);font-size:0.8125rem';
      setI18n(nameEl, (j.name || {}).sk || j.id, (j.name || {}).en || j.id);
      info.appendChild(nameEl);
      if (j.desc && (j.desc.sk || j.desc.en)) schedLine(info, j.desc.sk, j.desc.en);
      var cad = schedCadence(j);
      schedLine(info, cad[0], cad[1]);
      row.appendChild(info);

      var status = document.createElement('div');
      status.style.cssText = 'text-align:right;font-size:0.7rem;color:var(--text-muted);min-width:9rem;flex-shrink:0';
      var last = document.createElement('div');
      if (j.running) setI18n(last, 'Beží…', 'Running…');
      else if (j.last_ok) {
        var ok = schedTimeBoth(j.last_ok);
        var dur = j.last_duration_s != null ? ' (' + j.last_duration_s + ' s)' : '';
        setI18n(last, 'naposledy OK ' + ok[0] + dur, 'last OK ' + ok[1] + dur);
      } else setI18n(last, 'ešte nebežala', 'not run yet');
      status.appendChild(last);
      if (j.next_run_at) {
        var nx = schedTimeBoth(j.next_run_at);
        var nextEl = document.createElement('div');
        setI18n(nextEl, 'ďalší beh ' + nx[0], 'next run ' + nx[1]);
        status.appendChild(nextEl);
      }
      if (j.last_error) {
        var er = schedTimeBoth(j.last_error);
        var errLine = document.createElement('div');
        errLine.style.cssText = 'color:var(--err);margin-top:0.125rem';
        var type = j.last_error_type ? ': ' + j.last_error_type : '';
        setI18n(errLine, 'chyba ' + er[0] + type, 'error ' + er[1] + type);
        status.appendChild(errLine);
      }
      row.appendChild(status);
      container.appendChild(row);
    }

    function renderSchedInactive(container, inactive) {
      if (!inactive || !inactive.length) return;
      var det = document.createElement('details');
      det.style.cssText = 'margin-top:0.75rem';
      var sum = document.createElement('summary');
      sum.style.cssText = 'cursor:pointer;font-size:0.75rem;color:var(--text-muted)';
      setI18n(sum, 'Na tomto serveri neaktívne (' + inactive.length + ')', 'Not active on this server (' + inactive.length + ')');
      det.appendChild(sum);
      inactive.forEach(function(j) {
        var row = document.createElement('div');
        row.style.cssText = 'padding:0.375rem 0;border-bottom:1px solid var(--border)';
        var nameEl = document.createElement('div');
        nameEl.style.cssText = 'font-size:0.75rem;color:var(--text)';
        setI18n(nameEl, (j.name || {}).sk || j.id, (j.name || {}).en || j.id);
        row.appendChild(nameEl);
        var r = schedReasonText(j.reason, j.switch);
        schedLine(row, r[0], r[1]);
        det.appendChild(row);
      });
      container.appendChild(det);
    }

    function renderSchedules(data) {
      schedData = data;
      var container = document.getElementById('sched-jobs');
      clearEl(container);

      renderSchedHeader(container, data);
      if (schedFilter === 'all' || schedFilter === 'curated') renderCuratedLoop(container, data.curated_loop);

      var jobs = (data.jobs || []).filter(function(j) {
        return schedFilter === 'all' || j.group === schedFilter;
      });
      jobs.forEach(function(j) { renderSchedJob(container, j); });
      if (!jobs.length && schedFilter !== 'curated') {
        schedLine(container, 'Žiadne naplánované úlohy v tejto skupine.', 'No scheduled jobs in this group.');
      }
      renderSchedInactive(container, (data.inactive || []).filter(function(j) {
        return schedFilter === 'all' || j.group === schedFilter;
      }));

      // Prompt stats by call type
      var psContainer = document.getElementById('sched-prompt-stats');
      clearEl(psContainer);
      var byType = data.prompt_stats ? data.prompt_stats.by_call_type || {} : {};
      var typeLabels = {
        'ocr': 'OCR Extraction', 'summary_tags': 'AI Summary + Tags',
        'structured_metadata': 'Metadata Extraction', 'filename_description': 'Filename Description',
        'email_classify': 'Email Classification', 'calendar_classify': 'Calendar Classification'
      };
      var maxCalls = Math.max.apply(null, Object.values(byType).map(function(v) { return v.count || 0; }).concat([1]));

      Object.keys(byType).sort(function(a, b) { return (byType[b].count || 0) - (byType[a].count || 0); }).forEach(function(ct) {
        var v = byType[ct];
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.375rem';

        var label = document.createElement('span');
        label.style.cssText = 'width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.75rem;color:var(--text-muted)';
        label.textContent = typeLabels[ct] || ct;
        row.appendChild(label);

        var barBg = document.createElement('div');
        barBg.style.cssText = 'flex:1;height:14px;background:var(--border);border-radius:7px;overflow:hidden';
        var bar = document.createElement('div');
        bar.style.cssText = 'height:100%;border-radius:7px;background:var(--purple);transition:width 0.3s';
        bar.style.width = ((v.count / maxCalls) * 100) + '%';
        barBg.appendChild(bar);
        row.appendChild(barBg);

        var stats = document.createElement('span');
        stats.style.cssText = 'font-size:0.7rem;color:var(--text);min-width:120px;text-align:right;white-space:nowrap';
        stats.textContent = v.count + ' calls · $' + (v.cost_usd || 0).toFixed(3);
        if (v.errors > 0) stats.textContent += ' · ' + v.errors + ' err';
        row.appendChild(stats);

        psContainer.appendChild(row);
      });
    }

    async function downloadManifest() {
      try {
        var data = await apiFetch('/api/manifest');
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'oncofiles-export.json';
        a.click();
        URL.revokeObjectURL(url);
      } catch (e) {
        showError((dashLang === 'sk' ? 'Export sa nepodaril: ' : 'The export failed: ') + e.message);
      }
    }

    function updateLastRefreshed() {
      var el = document.getElementById('last-refreshed');
      el.textContent = new Date().toLocaleTimeString();
    }

    function renderPromptLog(data) {
      var statsEl = document.getElementById('prompt-stats');
      var rowsEl = document.getElementById('prompt-log-rows');
      clearEl(statsEl);
      clearEl(rowsEl);

      // Stats cards
      var stats = data.stats || {};
      var totalCalls = 0;
      var totalIn = 0;
      var totalOut = 0;
      Object.keys(stats).forEach(function(ct) {
        var s = stats[ct];
        totalCalls += s.count || 0;
        totalIn += s.total_input_tokens || 0;
        totalOut += s.total_output_tokens || 0;
      });

      var statItems = [
        { label: 'Total Calls', value: totalCalls },
        { label: 'Input Tokens', value: totalIn.toLocaleString() },
        { label: 'Output Tokens', value: totalOut.toLocaleString() },
      ];
      statItems.forEach(function(si) {
        var card = document.createElement('span');
        card.style.cssText = 'font-size:0.75rem;color:var(--text-muted)';
        var val = document.createElement('strong');
        val.style.color = 'var(--accent)';
        val.textContent = si.value;
        card.appendChild(val);
        card.appendChild(document.createTextNode(' ' + si.label));
        statsEl.appendChild(card);
      });

      // Entry rows
      var entries = data.entries || [];
      if (entries.length === 0) {
        var empty = document.createElement('div');
        empty.style.cssText = 'padding:0.5rem 0;color:var(--text-muted);font-size:0.8125rem';
        empty.textContent = 'No prompt log entries yet';
        rowsEl.appendChild(empty);
        return;
      }

      entries.forEach(function(e) {
        var row = document.createElement('div');
        row.className = 'sync-row';
        row.style.cursor = 'pointer';

        var time = document.createElement('span');
        time.className = 'sync-time';
        var ts = e.created_at || '';
        if (ts && !ts.endsWith('Z') && !ts.includes('+')) ts += 'Z';
        time.textContent = ts ? new Date(ts).toLocaleString() : '-';
        row.appendChild(time);

        var type = document.createElement('span');
        type.style.cssText = 'width:120px;flex-shrink:0;font-size:0.75rem';
        type.textContent = e.call_type || '-';
        row.appendChild(type);

        var tokens = document.createElement('span');
        tokens.style.cssText = 'width:80px;flex-shrink:0;font-size:0.75rem;color:var(--text-muted)';
        tokens.textContent = (e.input_tokens || 0) + '/' + (e.output_tokens || 0) + 't';
        row.appendChild(tokens);

        var dur = document.createElement('span');
        dur.className = 'sync-dur';
        dur.textContent = e.duration_ms ? e.duration_ms + 'ms' : '-';
        row.appendChild(dur);

        var summary = document.createElement('span');
        summary.style.cssText = 'flex:1;font-size:0.75rem;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        summary.textContent = e.result_summary || '';
        row.appendChild(summary);

        if (e.status === 'error') {
          var err = document.createElement('span');
          err.style.cssText = 'color:var(--red);font-size:0.75rem';
          err.textContent = 'ERR';
          row.appendChild(err);
        }

        row.onclick = (function(entry) { return function() {
          var existing = this.nextElementSibling;
          if (existing && existing.className === 'prompt-detail') {
            existing.remove();
            return;
          }
          var detail = document.createElement('div');
          detail.className = 'prompt-detail';
          detail.style.cssText = 'padding:0.75rem;background:rgba(20,184,166,0.05);border-radius:0.375rem;margin:0.25rem 0 0.5rem;font-size:0.75rem;white-space:pre-wrap;word-break:break-all;max-height:300px;overflow-y:auto';
          var content = '';
          if (entry.call_type) content += 'Type: ' + entry.call_type + '\n';
          if (entry.model) content += 'Model: ' + entry.model + '\n';
          if (entry.document_id) content += 'Document: #' + entry.document_id + '\n';
          content += 'Tokens: ' + (entry.input_tokens||0) + ' in / ' + (entry.output_tokens||0) + ' out\n';
          if (entry.duration_ms) content += 'Duration: ' + entry.duration_ms + 'ms\n';
          if (entry.system_prompt) content += '\n--- System Prompt ---\n' + entry.system_prompt;
          if (entry.user_prompt) content += '\n--- Input ---\n' + entry.user_prompt;
          content += '\n--- Result ---\n' + (entry.result_summary || 'N/A');
          detail.textContent = content;
          this.parentNode.insertBefore(detail, this.nextSibling);
        }; })(e);

        rowsEl.appendChild(row);
      });
    }

    // ── Analytics renderer ─────────────────────────────────────────────

    function renderAnalytics(data) {
      var p = data.prompts || {};
      var t = data.tools || {};
      var pipe = data.pipeline || {};
      var lat = data.latency || {};

      // Show explicit timeframe
      var days = data.days || 30;
      var endDate = new Date();
      var startDate = new Date(endDate - days * 86400000);
      var lang = dashLang || 'sk';
      var tfEl = document.getElementById('ana-timeframe');
      if (tfEl) {
        var fmt = function(d) { return d.toLocaleDateString(lang === 'sk' ? 'sk-SK' : 'en-US', {month:'short', day:'numeric'}); };
        // #482: label reflects the actual scope the server applied. Regular
        // users only ever see their own patient's stats; the old static
        // "všetci pacienti" label leaked the false impression they were
        // seeing aggregated cross-patient data.
        var scope = data.scope || 'all_patients';
        var scopeLabel;
        if (scope === 'all_patients') {
          scopeLabel = lang === 'sk' ? ' dní, všetci pacienti)' : ' days, all patients)';
        } else {
          scopeLabel = lang === 'sk' ? ' dní, tento pacient)' : ' days, this patient)';
        }
        tfEl.textContent = (lang === 'sk' ? 'Obdobie: ' : 'Period: ') + fmt(startDate) + ' – ' + fmt(endDate) + ' (' + days + scopeLabel;
      }

      // Mini-cards
      document.getElementById('ana-calls').textContent = p.total_calls || 0;
      var totalTok = ((p.total_input_tokens || 0) + (p.total_output_tokens || 0)) / 1000;
      document.getElementById('ana-tokens').textContent = totalTok < 10 ? totalTok.toFixed(1) : Math.round(totalTok);
      document.getElementById('ana-cost').textContent = '$' + (p.estimated_cost_usd || 0).toFixed(2);
      var errPct = ((p.error_rate || 0) * 100).toFixed(1) + '%';
      document.getElementById('ana-errors').textContent = errPct;
      var errCard = document.getElementById('ana-error-card');
      errCard.className = 'card' + (p.error_rate > 0.05 ? ' danger' : p.error_rate > 0 ? ' warning' : '');

      // Top tools bar chart
      var toolsEl = document.getElementById('ana-top-tools');
      clearEl(toolsEl);
      var tools = (t.top_tools || []).slice(0, 8);
      var maxCount = tools.length > 0 ? tools[0].count : 1;
      tools.forEach(function(tt) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:0.5rem;margin-bottom:0.25rem';
        var name = document.createElement('span');
        name.style.cssText = 'width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.75rem;color:var(--text-muted)';
        var toolNames = {
          'search_documents': 'Search Docs',
          'get_patient_context': 'Patient Context',
          'analyze_labs': 'Lab Analysis',
          'search_conversations': 'Search Notes',
          'get_lab_trends': 'Lab Trends',
          'compare_labs': 'Lab Compare',
          'view_document': 'View Document',
          'store_lab_values': 'Store Labs',
          'search_research_entries': 'Research Search',
          'get_clinical_protocol': 'Protocol',
        };
        name.textContent = toolNames[tt.tool] || tt.tool;
        row.appendChild(name);
        var barWrap = document.createElement('div');
        barWrap.style.cssText = 'flex:1;height:14px;background:var(--border);border-radius:3px;overflow:hidden';
        var bar = document.createElement('div');
        bar.style.cssText = 'height:100%;background:var(--accent);border-radius:3px;width:' + Math.round(tt.count / maxCount * 100) + '%';
        barWrap.appendChild(bar);
        row.appendChild(barWrap);
        var cnt = document.createElement('span');
        cnt.style.cssText = 'font-size:0.75rem;color:var(--text-muted);width:40px;text-align:right';
        cnt.textContent = tt.count;
        row.appendChild(cnt);
        row.style.cursor = 'pointer';
        row.title = 'Click for details';
        row.onclick = (function(tool) { return function() {
          var existing = this.nextElementSibling;
          if (existing && existing.className === 'tool-detail') {
            existing.remove(); return;
          }
          var detail = document.createElement('div');
          detail.className = 'tool-detail';
          detail.style.cssText = 'padding:0.5rem;background:rgba(20,184,166,0.05);border-radius:4px;margin:0.25rem 0;font-size:0.7rem;color:var(--text-muted)';
          detail.textContent = 'Tool: ' + tool.tool
            + '\nTotal calls: ' + tool.count
            + (tool.avg_duration_ms ? '\nAvg latency: ' + tool.avg_duration_ms + 'ms' : '')
            + (tool.error_count ? '\nErrors: ' + tool.error_count : '\nErrors: 0')
            + (tool.last_called ? '\nLast called: ' + new Date(tool.last_called).toLocaleString() : '');
          this.parentNode.insertBefore(detail, this.nextSibling);
        }; })(tt);
        toolsEl.appendChild(row);
      });
      if (tools.length === 0) {
        toolsEl.textContent = 'No data';
        toolsEl.style.color = 'var(--text-muted)';
      }

      // Pipeline stats
      var pipeEl = document.getElementById('ana-pipeline');
      clearEl(pipeEl);
      var pipeItems = [
        ['Syncs', pipe.total_syncs, null],
        ['Success', pipe.successful_syncs, 'var(--green)'],
        ['Failed', pipe.failed_syncs, pipe.failed_syncs > 0 ? 'var(--red)' : null],
        ['Imported', pipe.total_docs_imported, null],
        ['Enhanced', pipe.docs_enhanced, 'var(--green)'],
        ['Pending', pipe.docs_pending, pipe.docs_pending > 0 ? 'var(--amber)' : null],
        ['Avg sync', (pipe.avg_sync_duration_s || 0).toFixed(1) + 's', null],
      ];
      pipeItems.forEach(function(it) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;padding:0.125rem 0;font-size:0.8125rem;cursor:pointer';
        row.title = 'Click for details';
        var lbl = document.createElement('span');
        lbl.style.color = 'var(--text-muted)';
        lbl.textContent = it[0];
        row.appendChild(lbl);
        var val = document.createElement('span');
        if (it[2]) val.style.color = it[2];
        val.textContent = it[1];
        row.appendChild(val);
        row.onclick = (function(label, value) { return function() {
          var existing = this.nextElementSibling;
          if (existing && existing.className === 'pipe-detail') {
            existing.remove(); return;
          }
          var detail = document.createElement('div');
          detail.className = 'pipe-detail';
          detail.style.cssText = 'padding:0.375rem;background:rgba(20,184,166,0.05);border-radius:4px;margin:0.125rem 0;font-size:0.7rem;color:var(--text-muted)';
          var info = label + ': ' + value;
          if (label === 'Syncs') info += '\nTotal sync cycles run in the last 30 days.';
          if (label === 'Failed') info += '\nSync cycles that encountered errors.';
          if (label === 'Enhanced') info += '\nDocuments processed by AI metadata extraction.';
          if (label === 'Avg sync') info += '\nAverage duration of a full sync cycle.';
          detail.textContent = info;
          this.parentNode.insertBefore(detail, this.nextSibling);
        }; })(it[0], it[1]);
        pipeEl.appendChild(row);
      });

      // Latency (handles both p50/p95 and p50_ms/p95_ms formats)
      var latEl = document.getElementById('ana-latency');
      clearEl(latEl);
      function fmtLat(l) {
        if (!l) return '— / —';
        var p50 = l.p50_ms || l.p50 || 0;
        var p95 = l.p95_ms || l.p95 || 0;
        return (p50 / 1000).toFixed(1) + 's / ' + (p95 / 1000).toFixed(1) + 's';
      }
      var byType = lat.by_type || {};
      Object.keys(byType).forEach(function(ct) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;justify-content:space-between;padding:0.125rem 0;font-size:0.8125rem';
        var lbl = document.createElement('span');
        lbl.style.color = 'var(--text-muted)';
        lbl.textContent = ct;
        row.appendChild(lbl);
        var val = document.createElement('span');
        val.textContent = fmtLat(byType[ct]);
        row.appendChild(val);
        latEl.appendChild(row);
      });
      var overall = lat._overall || lat.overall;
      if (overall) {
        var ov = document.createElement('div');
        ov.style.cssText = 'display:flex;justify-content:space-between;padding:0.25rem 0 0;font-size:0.8125rem;border-top:1px solid var(--border);margin-top:0.25rem';
        var lbl = document.createElement('span');
        lbl.style.cssText = 'color:var(--text);font-weight:600';
        lbl.textContent = 'Overall';
        ov.appendChild(lbl);
        var val = document.createElement('span');
        val.style.fontWeight = '600';
        val.textContent = fmtLat(overall);
        ov.appendChild(val);
        latEl.appendChild(ov);
      }
      if (Object.keys(lat).length === 0) {
        latEl.textContent = 'No data';
        latEl.style.color = 'var(--text-muted)';
      }
    }

    // ── Cost leaderboard renderer (#411 Part A — admin only) ─────────────

    function renderCostLeaderboard(data) {
      var section = document.getElementById('cost-leaderboard-section');
      var body = document.getElementById('cost-leaderboard-body');
      if (!section || !body) return;
      // Hide the whole widget for non-admin callers — the endpoint also
      // 403s in that case, but we don't even fetch it then.
      if (!isGlobalAdmin) {
        section.style.display = 'none';
        return;
      }
      section.style.display = 'block';
      clearEl(body);

      var rows = (data && data.leaderboard) || [];
      if (rows.length === 0) {
        body.textContent = (dashLang === 'sk' ? 'Žiadne dáta' : 'No data');
        body.style.color = 'var(--text-muted)';
        return;
      }

      // Header row — XSS-safe DOM construction (#469 dashboard guard).
      var header = document.createElement('div');
      header.style.cssText =
        'display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:0.5rem;' +
        'padding:0.25rem 0.5rem;font-size:0.7rem;color:var(--text-muted);' +
        'text-transform:uppercase;letter-spacing:0.04em;border-bottom:1px solid var(--border)';
      ['Patient', 'Calls', 'Tokens (K)', 'Cost USD', 'Top tool'].forEach(function (label) {
        var col = document.createElement('span');
        col.textContent = label;
        header.appendChild(col);
      });
      body.appendChild(header);

      var maxCost = Math.max.apply(null, rows.map(function (r) { return r.total_cost_usd || 0; }));
      rows.forEach(function (r) {
        var row = document.createElement('div');
        row.style.cssText =
          'display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:0.5rem;' +
          'padding:0.375rem 0.5rem;font-size:0.8125rem;border-bottom:1px solid var(--border);' +
          'cursor:pointer';
        // Drill-in: clicking a row switches the dropdown to that patient.
        // Skip the `__system_no_patient__` sentinel and any other __-prefixed
        // sentinel — it's not a real caregiver patient and switchPatient()
        // would 404 on the dropdown.
        var pid = r.patient_id;
        var isSentinel = !pid || String(pid).indexOf('__') === 0;
        if (!isSentinel) {
          row.title = (dashLang === 'sk' ? 'Klikni pre detail' : 'Click to drill in');
          row.onclick = (function (clickedPid) { return function () { switchPatient(clickedPid); }; })(pid);
        } else {
          row.style.cursor = 'default';
          row.style.color = 'var(--text-muted)';
        }

        var name = document.createElement('span');
        name.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        name.textContent = r.display_name || r.slug || r.patient_id || '?';
        row.appendChild(name);

        var calls = document.createElement('span');
        calls.textContent = (r.total_calls || 0).toLocaleString();
        row.appendChild(calls);

        var tokens = document.createElement('span');
        var tokTotal = ((r.total_input_tokens || 0) + (r.total_output_tokens || 0)) / 1000;
        tokens.textContent = tokTotal < 10 ? tokTotal.toFixed(1) : Math.round(tokTotal).toLocaleString();
        row.appendChild(tokens);

        // Cost cell — bar + value, anomaly-flag color when >2x median (rough proxy).
        var costWrap = document.createElement('span');
        costWrap.style.cssText = 'display:flex;align-items:center;gap:0.375rem';
        var barWrap = document.createElement('span');
        barWrap.style.cssText = 'flex:1;height:10px;background:var(--border);border-radius:2px;overflow:hidden;display:inline-block';
        var bar = document.createElement('span');
        var pct = maxCost > 0 ? Math.round((r.total_cost_usd || 0) / maxCost * 100) : 0;
        var costColor = (r.total_cost_usd || 0) > 5 ? 'var(--red)' : (r.total_cost_usd || 0) > 1 ? 'var(--amber)' : 'var(--accent)';
        bar.style.cssText = 'display:block;height:100%;width:' + pct + '%;background:' + costColor + ';border-radius:2px';
        barWrap.appendChild(bar);
        costWrap.appendChild(barWrap);
        var costVal = document.createElement('span');
        costVal.style.cssText = 'min-width:60px;text-align:right;font-variant-numeric:tabular-nums';
        costVal.textContent = '$' + (r.total_cost_usd || 0).toFixed(2);
        costWrap.appendChild(costVal);
        row.appendChild(costWrap);

        var top = document.createElement('span');
        top.style.cssText = 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-muted);font-size:0.75rem';
        top.textContent = r.top_call_type || '—';
        row.appendChild(top);

        body.appendChild(row);
      });

      // Total footer
      var footer = document.createElement('div');
      footer.style.cssText =
        'display:grid;grid-template-columns:2fr 1fr 1fr 1fr 1fr;gap:0.5rem;' +
        'padding:0.5rem 0.5rem 0;margin-top:0.25rem;font-size:0.8125rem;font-weight:600;' +
        'border-top:1px solid var(--border)';
      var totalLabel = document.createElement('span');
      totalLabel.textContent = (dashLang === 'sk' ? 'Spolu' : 'Total') +
        ' (' + (data.patient_count || rows.length) + ' patients, ' + (data.days || 30) + 'd)';
      footer.appendChild(totalLabel);
      footer.appendChild(document.createElement('span'));
      footer.appendChild(document.createElement('span'));
      var totalCost = document.createElement('span');
      totalCost.style.cssText = 'text-align:right;font-variant-numeric:tabular-nums';
      totalCost.textContent = '$' + (data.total_cost_usd || 0).toFixed(2);
      footer.appendChild(totalCost);
      footer.appendChild(document.createElement('span'));
      body.appendChild(footer);
    }

    // ── Event Listeners ──────────────────────────────────────────────────

    document.querySelectorAll('th[data-sort]').forEach(function(th) {
      th.addEventListener('click', function() {
        var col = th.dataset.sort;
        // the same column flips; a new one opens dates newest first, text A to Z
        setSort(col, sortCol === col ? !sortAsc : defaultSortAsc(col));
      });
    });

    // Escape or a click on the backdrop closes the quick look and the Connect-AI
    // dialog (they hold nothing a person could lose). The patient wizard stays
    // open on purpose.
    document.addEventListener('keydown', function(e) {
      if (e.key !== 'Escape') return;
      if (quickLook.id !== null) { closeQuickLook(); return; }
      var share = document.getElementById('share-overlay');
      if (share && share.classList.contains('active')) { hideShareModal(); return; }
      var pop = document.getElementById('integration-popover');
      if (pop && pop.style.display === 'block') pop.style.display = 'none';
    });
    document.getElementById('share-overlay').addEventListener('click', function(e) {
      if (e.target === this) hideShareModal();
    });

    /* ── Bug Report ──────────────────────────────────────────────────── */
    (function() {
      var fab = document.getElementById('bug-fab');
      if (!fab) return;  // the demo page has no bug report
      var overlay = document.getElementById('bug-overlay');
      var screenshot = document.getElementById('bug-screenshot');
      var pasteZone = document.getElementById('bug-paste-zone');
      var fileInput = document.getElementById('bug-file');
      var titleInput = document.getElementById('bug-title');
      var descInput = document.getElementById('bug-desc');
      var submitBtn = document.getElementById('bug-submit');
      var cancelBtn = document.getElementById('bug-cancel');
      var statusEl = document.getElementById('bug-status');
      var screenshotB64 = '';
      var consoleErrors = [];

      var origError = console.error;
      console.error = function() {
        consoleErrors.push(Array.prototype.slice.call(arguments).join(' '));
        if (consoleErrors.length > 20) consoleErrors.shift();
        origError.apply(console, arguments);
      };
      window.addEventListener('error', function(e) {
        consoleErrors.push(e.message + ' at ' + e.filename + ':' + e.lineno);
      });

      function collectPageState() {
        // Only collect safe numeric/structural metadata — never raw text content
        return JSON.stringify({
          activity_rows: document.querySelectorAll('.activity-row').length,
          document_rows: document.querySelectorAll('#doc-tbody .doc-row').length,
          current_filter: typeof docFilter !== 'undefined' ? docFilter : '',
          card_count: document.querySelectorAll('.card').length,
          viewport: window.innerWidth + 'x' + window.innerHeight
        }, null, 2);
      }

      function handleImage(file) {
        if (!file || !file.type.startsWith('image/')) return;
        var reader = new FileReader();
        reader.onload = function(e) {
          screenshotB64 = e.target.result.split(',')[1];
          screenshot.src = e.target.result;
          // never shown or sent (#621)
          pasteZone.style.display = 'none';
        };
        reader.readAsDataURL(file);
      }

      // Paste handler (Ctrl+V screenshot)
      document.addEventListener('paste', function(e) {
        if (overlay.style.display !== 'flex') return;
        var items = (e.clipboardData || {}).items || [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].type.indexOf('image') !== -1) {
            handleImage(items[i].getAsFile());
            e.preventDefault();
            return;
          }
        }
      });

      // File upload handler
      if (fileInput) {
        fileInput.addEventListener('change', function() {
          if (fileInput.files.length > 0) handleImage(fileInput.files[0]);
        });
      }

      fab.addEventListener('click', function() {
        overlay.style.display = 'flex';
        screenshot.style.display = 'none';
        pasteZone.style.display = 'block';
        titleInput.value = '';
        descInput.value = '';
        statusEl.style.display = 'none';
        submitBtn.disabled = false;
        screenshotB64 = '';
      });

      cancelBtn.addEventListener('click', function() { overlay.style.display = 'none'; });
      overlay.addEventListener('click', function(e) { if (e.target === overlay) overlay.style.display = 'none'; });

      function showStatus(text, color) {
        statusEl.textContent = text;
        statusEl.style.color = color;
        statusEl.style.display = 'block';
      }

      submitBtn.addEventListener('click', function() {
        var title = titleInput.value.trim();
        if (!title) { titleInput.focus(); return; }
        submitBtn.disabled = true;
        submitBtn.textContent = 'Submitting...';
        showStatus('Creating GitHub issue...', 'var(--text-muted)');

        var payload = {
          title: title,
          description: descInput.value.trim(),
          // Privacy (#621): no screenshot, no page state, no query/fragment —
          // the dashboard shows patient data.
          page_url: location.pathname,
          page_section: 'dashboard',
          console_errors: consoleErrors.join('\n'),
          user_agent: navigator.userAgent
        };

        var headers = { 'Content-Type': 'application/json' };
        if (authMode === 'bearer') headers['Authorization'] = 'Bearer ' + TOKEN;
        else if (TOKEN) headers['X-Dashboard-Token'] = TOKEN;

        fetch('/api/bug-report', { method: 'POST', headers: headers, body: JSON.stringify(payload) })
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.ok && data.issue_url) {
              statusEl.textContent = '';
              statusEl.style.color = 'var(--green)';
              statusEl.appendChild(document.createTextNode('Bug reported! '));
              var link = document.createElement('a');
              link.href = data.issue_url;
              link.target = '_blank';
              link.rel = 'noreferrer';
              link.style.color = 'var(--accent)';
              link.textContent = 'View issue';
              statusEl.appendChild(link);
              submitBtn.textContent = 'Done';
            } else {
              showStatus('Error: ' + (data.error || 'Unknown error'), 'var(--red)');
              submitBtn.textContent = 'Submit Bug';
              submitBtn.disabled = false;
            }
          })
          .catch(function(e) {
            showStatus('Network error: ' + e.message, 'var(--red)');
            submitBtn.textContent = 'Submit Bug';
            submitBtn.disabled = false;
          });
      });
    })();

