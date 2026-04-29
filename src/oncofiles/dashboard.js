    /* All data rendered via safe DOM methods — textContent for strings, createElement for structure.
       The esc() helper is used only as a safety net for the few cases where we build HTML strings
       from our own authenticated API responses (numeric/boolean fields). */

    function toggleSection(bodyId, headerEl) {
      var el = document.getElementById(bodyId);
      if (!el) return;
      var expanded = el.style.display !== 'none';
      el.style.display = expanded ? 'none' : 'block';
      var chevron = headerEl.querySelector('.chevron');
      if (chevron) chevron.style.transform = expanded ? '' : 'rotate(90deg)';
    }

    let authMode = '';  // 'bearer' or 'session'
    let TOKEN = '';     // bearer token or session token
    let currentFilter = 'all';
    let sortCol = 'date';
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
    }

    function toggleDashLang() {
      setDashLang(dashLang === 'sk' ? 'en' : 'sk');
    }

    function applyDashLang() {
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

    /** Create a status dot element. */
    function makeDot(value) {
      const span = document.createElement('span');
      span.className = 'dot ' + (value ? 'green' : 'red');
      return span;
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
      // Demo mode: skip auth, load sample data
      if (window.DEMO_MODE) {
        document.getElementById('auth-gate').style.display = 'none';
        document.getElementById('dashboard').style.display = 'block';
        // Hide patient controls and auth-related UI in demo
        var patientControls = document.querySelector('.patient-controls');
        if (patientControls) patientControls.style.display = 'none';
        var userInfo = document.getElementById('user-info');
        if (userInfo) userInfo.textContent = 'Demo Mode';
        // Replace logout with navigation links (#233)
        var logoutBtn = document.querySelector('.logout-btn');
        if (logoutBtn) {
          logoutBtn.textContent = '\u2190 oncofiles.com';
          logoutBtn.onclick = function(){ window.location.href = '/'; };
          logoutBtn.removeAttribute('data-sk');
          logoutBtn.removeAttribute('data-en');
          // Add oncoteam link next to it
          var oncoteamLink = document.createElement('a');
          oncoteamLink.href = 'https://oncoteam.cloud';
          oncoteamLink.textContent = 'oncoteam.cloud \u2192';
          oncoteamLink.style.cssText = 'font-size:0.75rem;color:var(--accent);margin-left:0.75rem;text-decoration:none;opacity:0.8';
          logoutBtn.parentNode.insertBefore(oncoteamLink, logoutBtn.nextSibling);
        }
        // Disable service badges in demo — explain what each does (#233)
        var badgeExplanations = {
          'gdrive': 'Google Drive \u2014 syncs your medical documents. Sign in to connect.',
          'gmail': 'Gmail \u2014 imports appointment confirmations. Sign in to connect.',
          'gcal': 'Google Calendar \u2014 tracks appointments. Sign in to connect.'
        };
        var badges = document.querySelectorAll('.svc-badge');
        badges.forEach(function(b) {
          var svcName = (b.dataset.svc || b.textContent || '').toLowerCase().trim();
          b.title = badgeExplanations[svcName] || (b.title + ' \u2014 sign in to connect');
          b.onclick = null;
          b.style.cursor = 'help';
          b.style.opacity = '0.5';
        });
        // Hide GDrive connect bar in demo
        var gdriveBar = document.getElementById('gdrive-connect-bar');
        if (gdriveBar) gdriveBar.style.display = 'none';
        // Add signup CTA banner at bottom (#268)
        var ctaBanner = document.createElement('div');
        ctaBanner.id = 'demo-cta-banner';
        ctaBanner.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:1000;background:var(--surface);border-top:1px solid var(--border);padding:0.75rem 1.5rem;display:flex;align-items:center;justify-content:center;gap:1rem;box-shadow:0 -2px 12px rgba(0,0,0,0.08)';
        var ctaText = document.createElement('span');
        ctaText.style.cssText = 'font-size:0.875rem;color:var(--text-dim)';
        ctaText.setAttribute('data-sk', 'Nastavenie trvá 2 minúty: Prihláste sa cez Google \u2192 Vyberte priečinok \u2192 Začnite sa pýtať AI');
        ctaText.setAttribute('data-en', 'Setup takes 2 minutes: Sign in with Google \u2192 Pick a Drive folder \u2192 Start asking AI');
        ctaText.textContent = 'Setup takes 2 minutes: Sign in with Google \u2192 Pick a Drive folder \u2192 Start asking AI';
        var ctaBtn = document.createElement('a');
        ctaBtn.href = '/dashboard';
        ctaBtn.style.cssText = 'padding:0.5rem 1.25rem;background:var(--accent);color:#fff;border-radius:6px;text-decoration:none;font-size:0.8125rem;font-weight:600;white-space:nowrap';
        ctaBtn.setAttribute('data-sk', 'Začať zadarmo');
        ctaBtn.setAttribute('data-en', 'Start free');
        ctaBtn.textContent = 'Start free';
        ctaBanner.appendChild(ctaText);
        ctaBanner.appendChild(ctaBtn);
        document.body.appendChild(ctaBanner);
        // Re-apply language after inserting CTA
        if (typeof applyDashLang === 'function') applyDashLang();
        loadDemoData();
        return;
      }

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
      TOKEN = sessionStorage.getItem('oncofiles_token') || '';
      authMode = sessionStorage.getItem('oncofiles_auth_mode') || '';

      if (TOKEN && authMode) {
        showDashboard();
      } else {
        initGoogleSignIn();
      }
    })();

    async function loadDemoData() {
      setConnectionState('loading');
      showSkeletons();
      try {
        var resp = await fetch('/api/demo-data');
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var data = await resp.json();
        renderStatus(data.status);
        renderDocs(data.documents);
        if (data.prompt_log) renderPromptLog(data.prompt_log);
        if (data.analytics) renderAnalytics(data.analytics);
        // Show conversation previews in demo
        var demoConv = document.getElementById('demo-conversations');
        if (demoConv) demoConv.style.display = 'block';
        setConnectionState('ok');
        hideSkeletons();
        document.getElementById('version-label').textContent = 'v' + data.status.version + ' demo';
        // Auto-expand the Labs doc to showcase rich OCR + metadata
        setTimeout(function() {
          var rows = document.querySelectorAll('#doc-table tbody tr');
          var target = null;
          rows.forEach(function(r) {
            if (r.textContent.indexOf('BloodCount') !== -1 || r.textContent.indexOf('Labs_Blood') !== -1) target = r;
          });
          if (!target) {
            // Fallback: find any Labs row
            rows.forEach(function(r) { if (!target && r.textContent.indexOf('Labs') !== -1) target = r; });
          }
          if (!target && rows.length) target = rows[0];
          if (target) {
            target.click();
            setTimeout(function() { target.scrollIntoView({behavior:'smooth', block:'center'}); }, 100);
          }
        }, 1500);
      } catch (e) {
        console.error('Demo data load failed:', e);
        setConnectionState('error');
        showError('Failed to load demo data');
      }
    }

    /** Sample doc detail for demo mode — shows OCR and metadata without API call. */
    function getDemoDocDetail(doc) {
      var demoDetails = {
        1: {
          filename: '20260315_Patient_NOU_Labs_BloodCount.pdf',
          category: 'labs', date: '2026-03-15', institution: 'NOU',
          gdrive_url: null, preview_url: null,
          ai_summary: 'Complete blood count before cycle 5 mFOLFOX6. WBC 4.2, HGB 112 g/L (mild anemia), PLT 268, ANC 2.1. Liver enzymes stable. CEA trending down from 18.4 to 12.1 — favorable response.',
          ai_tags: ['CBC', 'pre-chemo', 'mFOLFOX6', 'CEA declining', 'mild anemia'],
          metadata: { diagnosis: 'C18.7 — mCRC', treatment: 'mFOLFOX6 cycle 5', provider: 'Národný onkologický ústav' },
          pages: [
            { page_number: 1, char_count: 1842, text: 'NÁRODNÝ ONKOLOGICKÝ ÚSTAV\nKlenová 1, 833 10 Bratislava\n\nLABORATÓRNY NÁLEZ\nDátum: 15.03.2026\nPacient: [REDACTED]\n\nKRVNÝ OBRAZ:\nWBC: 4.2 x10^9/L (ref: 4.0-10.0)\nRBC: 3.89 x10^12/L (ref: 3.80-5.20)\nHGB: 112 g/L (ref: 120-160) ↓\nHCT: 0.34 L/L (ref: 0.35-0.47) ↓\nPLT: 268 x10^9/L (ref: 150-400)\nANC: 2.1 x10^9/L\n\nBIOCHÉMIA:\nALT: 0.45 µkat/L (ref: 0.10-0.83)\nAST: 0.52 µkat/L (ref: 0.10-0.72)\nGGT: 0.89 µkat/L (ref: 0.10-1.00)\nKreatinín: 78 µmol/L (ref: 53-115)\n\nNÁDOROVÉ MARKERY:\nCEA: 12.1 ng/mL (ref: <5.0) ↑\nCA 19-9: 28.4 U/mL (ref: <37.0)\n\nZáver: Mierna anémia, neutrofily v norme — pacient spôsobilý na chemoterapiu.' }
          ]
        },
        3: {
          filename: '20260301_Patient_NOU_Pathology_ColonBiopsy.pdf',
          category: 'pathology', date: '2026-03-01', institution: 'NOU',
          gdrive_url: null, preview_url: null,
          ai_summary: 'Pathology report confirming adenocarcinoma G3, sigmoid colon. KRAS G12D mutation detected. MSS/pMMR. BRAF wild-type.',
          ai_tags: ['adenocarcinoma', 'G3', 'KRAS G12D', 'MSS', 'pMMR', 'BRAF WT'],
          metadata: { diagnosis: 'C18.7 — adenocarcinoma sigmoid', biomarkers: 'KRAS G12D, MSS, BRAF WT', provider: 'Národný onkologický ústav' },
          pages: [
            { page_number: 1, char_count: 2156, text: 'NÁRODNÝ ONKOLOGICKÝ ÚSTAV\nÚstav patológie\n\nHISTOPATOLOGICKÝ NÁLEZ\nDátum: 01.03.2026\n\nMateriál: Biopsia colon sigmoideum\nDiagnóza: Adenokarcinóm, grade 3 (G3)\nLokalizácia: Colon sigmoideum\n\nMikroskopický nález:\nInvazívny adenokarcinóm s tubulárnym a kribriformným usporiadaním.\nVýrazná jadrová atypia, vysoká mitotická aktivita.\n\nMolekulárna genetika:\nKRAS: mutácia G12D detegovaná\nNRAS: wild-type\nBRAF V600E: wild-type\nMMR/MSI: pMMR / MSS (stabilný)\n\nZáver: Adenokarcinóm hrubého čreva G3, KRAS G12D pozitívny.' }
          ]
        }
      };
      var detail = demoDetails[doc.id];
      if (detail) return detail;
      // Generic fallback for other demo docs
      return {
        filename: doc.filename, category: doc.category || 'other',
        date: doc.date, institution: doc.institution || 'Hospital',
        gdrive_url: null, preview_url: null,
        ai_summary: 'AI-extracted summary of medical document. Categories, dates, and institutions are automatically detected from OCR text.',
        ai_tags: [doc.category || 'document', 'auto-processed', 'OCR'],
        metadata: {},
        pages: [{ page_number: 1, char_count: 450, text: 'Sample OCR text from ' + (doc.filename || 'document') + '.\n\nOncofiles automatically extracts text via OCR, identifies the document category, date, and institution, then generates an AI summary with structured metadata.\n\nAll data stays in your Google Drive.' }]
      };
    }

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
      document.getElementById('auth-gate').style.display = 'none';
      document.getElementById('dashboard').style.display = 'block';
      // Show MCP connection bar (not in demo mode)
      if (!window.DEMO_MODE) {
        document.getElementById('mcp-connection-bar').style.display = 'flex';
      }
      // Show user info
      var email = sessionStorage.getItem('oncofiles_email') || '';
      var name = sessionStorage.getItem('oncofiles_name') || '';
      if (email) {
        var userEl = document.getElementById('user-info');
        userEl.textContent = name || email;
      }
      loadPatients().then(function() { refresh(); });
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
    // tearing down to skeletons every tick. On a breaker trip the cached
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
        if (iconEl) iconEl.textContent = resp.in_onboarding ? '🎁' : '€';
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
        // Silent — pill stays at last known state; breaker banner handles UX
      }
    }

    // Admin-only breaker widget (#469 Phase 7) — fetches /readiness (no auth)
    // and reflects state + lifetime trip count in the header. Hidden unless
    // isAdminMode. Polls piggyback on the refresh() cycle; no separate timer.
    async function updateBreakerWidget() {
      if (!isAdminMode) return;
      var widget = document.getElementById('breaker-widget');
      if (!widget) return;
      try {
        var resp = await fetch('/readiness', { cache: 'no-store' });
        if (!resp.ok) return;
        var payload = await resp.json();
        var cb = payload && payload.circuit_breaker;
        if (!cb) return;
        widget.classList.add('admin-visible');
        widget.classList.remove('state-closed', 'state-open', 'state-half_open');
        widget.classList.add('state-' + String(cb.state));
        widget.classList.toggle('has-trips', (cb.trip_count_total || 0) > 0);
        var tripsEl = document.getElementById('bw-trips');
        if (tripsEl) tripsEl.textContent = String(cb.trip_count_total || 0);
        // Tooltip with full detail (state, cause, last trip timestamp)
        var tip = 'state=' + cb.state + ' · cooldown_remaining=' + (cb.cooldown_remaining_s || 0) + 's';
        if (cb.last_trip_at) tip += ' · last_trip=' + cb.last_trip_at;
        if (cb.last_trip_cause) tip += ' · cause=' + cb.last_trip_cause;
        widget.title = tip;
      } catch (e) { /* no-op — widget stays at last known state */ }
    }

    function _hydrateFromSwrCache() {
      // Try to populate the 3 critical sections from cache. Returns true if
      // at least one section was hydrated — caller skips skeletons in that case.
      var hydrated = false;
      var s = _swrGet('status');
      if (s) { try { renderStatus(s.data); hydrated = true; } catch (e) { console.warn('SWR status render failed:', e); } }
      var d = _swrGet('documents');
      if (d) { try { renderDocs(d.data); hydrated = true; } catch (e) { console.warn('SWR documents render failed:', e); } }
      var p = _swrGet('prompt-log');
      if (p) { try { renderPromptLog(p.data); hydrated = true; } catch (e) { console.warn('SWR prompt-log render failed:', e); } }
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

    // ── Breaker-aware retry + friendly countdown banner (#469) ───────────

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
     * Fetch with circuit-breaker awareness.
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

          // Breaker-open — surface to caller immediately. The caller renders a
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

    // ── Patient selector ────────────────────────────────────────────────

    var adminPatientIds = new Set();
    var isAdminMode = false;
    // #411: caller is global admin (DASHBOARD_ADMIN_EMAILS / static bearer).
    // Distinct from `isAdminMode` which means "current selected patient is one
    // I'm admin-viewing read-only". The leaderboard widget on the analytics
    // panel hides itself unless this is true.
    var isGlobalAdmin = false;

    async function loadPatients() {
      try {
        var resp = await fetch('/api/patients', {
          headers: { 'Authorization': authHeader() }
        });
        if (!resp.ok) return;
        var data = await resp.json();

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
          emptyOpt.textContent = 'No patients';
          select.appendChild(emptyOpt);
          return;
        }

        // Render own patients
        ownPatients.forEach(function(p) {
          var opt = document.createElement('option');
          opt.value = p.patient_id;
          opt.textContent = p.display_name + (p.is_active ? '' : ' (archived)');
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
            opt.textContent = '\u{1F512} ' + p.display_name + (p.is_active ? '' : ' (archived)');
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
      if (addPatientBtn && isAdminMode) addPatientBtn.style.display = 'none';
    }

    function switchPatient(patientId) {
      currentPatientId = patientId;
      localStorage.setItem('oncofiles_patient_id', patientId);
      updateAdminMode();
      updatePatientDiagnosis();
      updateArchiveButton();
      clearAllPatientData();
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
      if (!banner || !list || !currentPatientId) { if (banner) banner.style.display = 'none'; return; }
      try {
        var resp = await fetch('/api/patient-context?patient_id=' + encodeURIComponent(currentPatientId), {
          headers: { 'Authorization': 'Bearer ' + authToken }
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

    async function togglePatientArchive() {
      if (!currentPatientId) return;
      var p = patientsCache.find(function(x) { return x.patient_id === currentPatientId; });
      if (!p) return;
      var newActive = !p.is_active;
      var msg = newActive
        ? (dashLang === 'sk' ? 'Obnoviť pacienta?' : 'Restore patient?')
        : (dashLang === 'sk' ? 'Archivovať pacienta? Zastaví sa synchronizácia.' : 'Archive patient? This will stop syncing.');
      if (!confirm(msg)) return;
      try {
        var resp = await fetch('/api/patients/' + currentPatientId, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken },
          body: JSON.stringify({ is_active: newActive })
        });
        if (!resp.ok) throw new Error('Failed');
        await loadPatients();
        updateArchiveButton();
      } catch (e) {
        console.error('Archive/restore failed:', e);
        alert(dashLang === 'sk' ? 'Chyba pri zmene stavu.' : 'Failed to change status.');
      }
    }

    function clearAllPatientData() {
      // Clear stat cards immediately to prevent stale data showing
      showSkeletons();
      // Clear document table — safe static string, no user input
      var tbody = document.getElementById('doc-tbody');
      if (tbody) {
        while (tbody.firstChild) tbody.removeChild(tbody.firstChild);
        var tr = document.createElement('tr');
        var td = document.createElement('td');
        td.setAttribute('colspan', '11');
        td.style.textAlign = 'center';
        td.style.color = '#888';
        td.textContent = 'Loading...';
        tr.appendChild(td);
        tbody.appendChild(tr);
      }
      // Clear pipeline funnel
      document.querySelectorAll('.funnel-bar').forEach(function(el) {
        el.style.width = '0%';
      });
      document.querySelectorAll('.funnel-count').forEach(function(el) {
        el.textContent = '0';
      });
      // Clear reconciliation
      var reconEl = document.getElementById('reconciliation-content');
      if (reconEl) { while (reconEl.firstChild) reconEl.removeChild(reconEl.firstChild); }
      // Clear usage analytics
      var analyticsEl = document.getElementById('analytics-content');
      if (analyticsEl) { while (analyticsEl.firstChild) analyticsEl.removeChild(analyticsEl.firstChild); }
      // Clear cost leaderboard body — keep section visibility decision to renderCostLeaderboard.
      var leaderboardEl = document.getElementById('cost-leaderboard-body');
      if (leaderboardEl) { while (leaderboardEl.firstChild) leaderboardEl.removeChild(leaderboardEl.firstChild); }
      // Clear prompt log
      var promptEl = document.getElementById('prompt-log-entries');
      if (promptEl) { while (promptEl.firstChild) promptEl.removeChild(promptEl.firstChild); }
      // Clear filter badge counts to prevent stale numbers from previous patient
      document.querySelectorAll('.pill .pill-count').forEach(function(el) {
        el.textContent = '0';
      });
    }

    function updatePatientDiagnosis() {
      var el = document.getElementById('patient-diagnosis');
      var p = patientsCache.find(function(p) {
        return p.patient_id === currentPatientId;
      });
      el.textContent = p && p.diagnosis_summary ? p.diagnosis_summary : '';
      el.title = el.textContent;
    }

    // ── Onboarding wizard ───────────────────────────────────────────────

    var wizardPatientId = '';
    var wizardBearerToken = '';
    var wizardCurrentStep = 1;

    function switchDemoChat(tab, btn) {
      document.querySelectorAll('.demo-chat-panel').forEach(function(el) { el.style.display = 'none'; });
      var panel = document.getElementById('demo-chat-' + tab);
      if (panel) panel.style.display = 'flex';
      document.querySelectorAll('.demo-chat-tab').forEach(function(b) {
        b.style.background = 'var(--bg)'; b.style.color = 'var(--text)';
      });
      if (btn) { btn.style.background = 'var(--green)'; btn.style.color = '#fff'; }
    }

    function toggleIntegrationPanel() {
      if (window.DEMO_MODE) return;
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
      wizardPatientId = pid;
      wizardBearerToken = '';
      wizardCurrentStep = 2;
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
      var tokenBox = document.querySelector('.wizard-token-box');
      if (tokenBox) tokenBox.style.display = 'none';
      var tokenWarning = tokenBox ? tokenBox.nextElementSibling : null;
      if (tokenWarning && tokenWarning.style) tokenWarning.style.display = 'none';
      updateWizardDots();
      document.getElementById('wizard-overlay').classList.add('active');
    }

    function showOnboarding() {
      wizardPatientId = '';
      wizardBearerToken = '';
      wizardCurrentStep = 1;
      // Reset form fields
      document.getElementById('wiz-name').value = '';
      document.getElementById('wiz-id').value = '';
      document.getElementById('wiz-diagnosis').value = '';
      document.getElementById('wiz-email').value = '';
      document.getElementById('wiz-lang').value = 'sk';
      document.getElementById('wiz-step1-error').style.display = 'none';
      // Show step 1, hide others
      for (var i = 1; i <= 4; i++) {
        document.getElementById('wiz-step-' + i).style.display = i === 1 ? 'block' : 'none';
      }
      updateWizardDots();
      document.getElementById('wizard-overlay').classList.add('active');
    }

    function hideOnboarding() {
      document.getElementById('wizard-overlay').classList.remove('active');
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

    async function wizardStep1Submit() {
      var btn = document.getElementById('wiz-create-btn');
      var errEl = document.getElementById('wiz-step1-error');
      errEl.style.display = 'none';
      btn.disabled = true;
      btn.textContent = 'Creating...';

      var name = document.getElementById('wiz-name').value.trim();
      var pid = document.getElementById('wiz-id').value.trim().toLowerCase();
      if (!name || !pid) {
        errEl.textContent = 'Patient name and ID are required.';
        errEl.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Create Patient';
        return;
      }

      var body = JSON.stringify({
        patient_id: pid,
        display_name: name,
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
        // Auto-retry once on transient DB errors (circuit breaker, Turso stream expiry).
        if (resp.status === 503 || resp.status === 500) {
          btn.textContent = dashLang === 'sk' ? 'Pripájam databázu… skúšam znova' : 'Reconnecting database… retrying';
          await new Promise(function (r) { setTimeout(r, 3500); });
          resp = await doCreate();
        }
        var data = await resp.json();
        if (resp.status === 409) {
          errEl.textContent = dashLang === 'sk'
            ? 'Tento pacient už existuje. Zvoľte iné ID.'
            : 'This patient already exists — pick a different ID.';
          errEl.style.display = 'block';
          btn.disabled = false;
          btn.textContent = 'Create Patient';
          return;
        }
        if (!resp.ok) {
          errEl.textContent = dashLang === 'sk'
            ? 'Databáza je krátko nedostupná, prosím skúste znova o ~30s. (' + (data.error || resp.status) + ')'
            : 'Database briefly unavailable, please try again in ~30s. (' + (data.error || resp.status) + ')';
          errEl.style.display = 'block';
          btn.disabled = false;
          btn.textContent = 'Create Patient';
          return;
        }
        wizardPatientId = pid;
        wizardBearerToken = data.bearer_token || '';
        // Show token in step 2
        document.getElementById('wiz-token-display').textContent = wizardBearerToken;
        wizardGoToStep(2);
      } catch (e) {
        errEl.textContent = (dashLang === 'sk' ? 'Sieťová chyba: ' : 'Network error: ') + e.message;
        errEl.style.display = 'block';
      }
      btn.disabled = false;
      btn.textContent = 'Create Patient';
    }

    function wizardStep2Auth() {
      var url = '/oauth/authorize/drive?patient_id=' + encodeURIComponent(wizardPatientId);
      window.open(url, '_blank');
      var statusEl = document.getElementById('wiz-step2-status');
      statusEl.textContent = 'Complete the authorization in the new tab, then click below.';
      statusEl.style.display = 'block';
      document.getElementById('wiz-step2-auth-done').style.display = 'flex';
    }

    async function wizardStep2LoadFolders() {
      document.getElementById('wiz-step2-auth').style.display = 'none';
      document.getElementById('wiz-step2-folder').style.display = 'block';

      try {
        var resp = await fetch(
          '/api/gdrive-folders?patient_id=' + encodeURIComponent(wizardPatientId),
          { headers: { 'Authorization': authHeader() } }
        );
        if (!resp.ok) {
          var err = await resp.json();
          var statusEl = document.getElementById('wiz-step2-folder-status');
          statusEl.textContent = err.error || 'Failed to load folders';
          statusEl.style.display = 'block';
          return;
        }
        var data = await resp.json();
        var select = document.getElementById('wiz-folder-select');
        clearEl(select);

        // "Create new" option first
        var newOpt = document.createElement('option');
        newOpt.value = '__new__';
        newOpt.textContent = '+ Create new folder';
        select.appendChild(newOpt);

        // Existing folders
        (data.folders || []).forEach(function(f) {
          var opt = document.createElement('option');
          opt.value = f.id;
          opt.textContent = f.name;
          select.appendChild(opt);
        });

        // Default new folder name
        var patientName = document.getElementById('wiz-name').value.trim();
        document.getElementById('wiz-folder-name').value = 'Oncofiles - ' + patientName;
        wizardFolderChanged();
      } catch (e) {
        var statusEl = document.getElementById('wiz-step2-folder-status');
        statusEl.textContent = 'Error: ' + e.message;
        statusEl.style.display = 'block';
      }
    }

    function wizardFolderChanged() {
      var val = document.getElementById('wiz-folder-select').value;
      document.getElementById('wiz-folder-new').style.display = val === '__new__' ? 'block' : 'none';
    }

    async function wizardStep2SetFolder() {
      var btn = document.getElementById('wiz-folder-btn');
      var statusEl = document.getElementById('wiz-step2-folder-status');
      btn.disabled = true;
      statusEl.textContent = 'Setting up folder structure...';
      statusEl.style.display = 'block';

      var selectVal = document.getElementById('wiz-folder-select').value;
      var body = { patient_id: wizardPatientId };
      if (selectVal === '__new__') {
        body.folder_name = document.getElementById('wiz-folder-name').value.trim() || 'Oncofiles';
      } else {
        body.folder_id = selectVal;
      }

      try {
        var resp = await fetch('/api/gdrive-set-folder', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        var data = await resp.json();
        if (!resp.ok) {
          statusEl.textContent = data.error || 'Failed to set folder';
          btn.disabled = false;
          return;
        }
        statusEl.textContent = 'Folder ready! ' + data.subfolders_created + ' category folders created.';
        statusEl.style.color = 'var(--green)';
        setTimeout(function() { wizardGoToStep(3); }, 1000);
      } catch (e) {
        statusEl.textContent = 'Error: ' + e.message;
        btn.disabled = false;
      }
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
          statusEl.textContent = 'Sync issue: ' + (data.error || 'unknown error');
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

    async function triggerManualSync() {
      var btn = document.getElementById('ip-sync-btn');
      if (!btn || btn.disabled) return;
      btn.disabled = true;
      var origText = btn.textContent;
      btn.textContent = 'Syncing\u2026';
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
          btn.textContent = data.error || 'Error';
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
      var parts = ['Patient "' + esc(wizardPatientId) + '" created.'];
      if (stats && stats.new > 0) {
        parts.push(stats.new + ' documents imported.');
      }
      el.textContent = parts.join(' ');
      // Populate token for copy in step 4 (step-2 box uses same variable but different element)
      var tokenEl = document.getElementById('wiz-token-display-4');
      if (tokenEl && wizardBearerToken) {
        tokenEl.textContent = wizardBearerToken;
      } else if (tokenEl) {
        tokenEl.textContent = '(generate via Share button)';
      }
    }

    function wizCopy(elementId) {
      var el = document.getElementById(elementId);
      if (el && navigator.clipboard) {
        navigator.clipboard.writeText(el.textContent).then(function() {
          var btn = el.nextElementSibling;
          if (btn) {
            btn.textContent = 'Copied!';
            setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
          }
        });
      }
    }

    async function wizardFinish() {
      hideOnboarding();
      // Switch to new patient and reload
      currentPatientId = wizardPatientId;
      localStorage.setItem('oncofiles_patient_id', currentPatientId);
      await loadPatients();
      document.getElementById('patient-select').value = currentPatientId;
      refresh();
    }

    // ── Share modal ─────────────────────────────────────────────────────

    var MCP_URL = 'https://oncofiles.com/mcp';

    async function regenerateToken() {
      var btn = document.getElementById('mcp-token-gen');
      var orig = btn.textContent;
      btn.disabled = true;
      btn.textContent = '...';
      try {
        var resp = await fetch('/api/patient-tokens', {
          method: 'POST',
          headers: { 'Authorization': authHeader(), 'Content-Type': 'application/json' },
          body: JSON.stringify({ patient_id: currentPatientId, label: 'dashboard-regen' })
        });
        var data = await resp.json();
        if (!resp.ok) {
          btn.textContent = data.error || 'Error';
          btn.disabled = false;
          return;
        }
        document.getElementById('mcp-token-display').textContent = data.bearer_token;
        document.getElementById('mcp-token-copy').style.display = '';
        btn.style.display = 'none';
      } catch (e) {
        btn.textContent = orig;
        btn.disabled = false;
      }
    }

    function showShareModal() {
      document.getElementById('share-code-box').style.display = 'none';
      document.getElementById('share-gen-btn').disabled = false;
      document.getElementById('share-gen-btn').textContent = 'Generate One-Time Setup Code';
      document.getElementById('share-token').textContent = 'Generate a setup code or create a new token';
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
        if (btn) { btn.textContent = 'Copied!'; setTimeout(function() { btn.textContent = 'Copy'; }, 1500); }
      });
    }

    async function generateShareCode() {
      var btn = document.getElementById('share-gen-btn');
      btn.disabled = true;
      btn.textContent = 'Generating...';

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
          btn.textContent = data.error || 'Error';
          btn.disabled = false;
          return;
        }

        // Show the code
        document.getElementById('share-code-value').textContent = data.code;
        document.getElementById('share-code-box').style.display = 'block';
        btn.textContent = 'Code generated (10 min)';

        // Now redeem it immediately to get the token for display
        var redeemResp = await fetch('/api/share-link/' + data.code);
        if (redeemResp.ok) {
          var info = await redeemResp.json();
          document.getElementById('share-token').textContent = info.bearer_token;
          updateShareInstructions(info.bearer_token);
          // Also update the MCP connection bar
          document.getElementById('mcp-token-display').textContent = info.bearer_token;
          document.getElementById('mcp-token-copy').style.display = '';
          document.getElementById('mcp-token-gen').style.display = 'none';
        }
      } catch (e) {
        btn.textContent = 'Error: ' + e.message;
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

    function showSkeletons() {
      ['card-total','card-complete','card-gaps','card-synced','card-uptime'].forEach(function(id) {
        var el = document.getElementById(id);
        el.textContent = '';
        el.className = 'value skeleton';
      });
    }

    function hideSkeletons() {
      ['card-total','card-complete','card-gaps','card-synced','card-uptime'].forEach(function(id) {
        document.getElementById(id).className = 'value';
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
      // Sequential fetches — Turso single-connection can't handle parallel queries
      // Each section is independent: one failure doesn't block the others
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
        var docs = await apiFetch('/api/documents?filter=' + encodeURIComponent(currentFilter) + '&limit=200');
        renderDocs(docs);
        _swrSet('documents', docs);
        anyOk = true;
      } catch (e) {
        if (!trackBreaker(e)) errors.push('documents: ' + e.message);
      }
      try {
        var prompts = await apiFetch('/api/prompt-log?limit=50');
        renderPromptLog(prompts);
        _swrSet('prompt-log', prompts);
        anyOk = true;
      } catch (e) {
        if (!trackBreaker(e)) errors.push('prompt-log: ' + e.message);
      }
      try {
        var recon = await apiFetch('/api/reconciliation');
        if (recon) renderReconciliation(recon);
        anyOk = true;
      } catch (e) { if (!trackBreaker(e)) console.warn('Reconciliation fetch failed:', e); }
      try {
        var analytics = await apiFetch('/api/usage-analytics');
        if (analytics) renderAnalytics(analytics);
        anyOk = true;
      } catch (e) { if (!trackBreaker(e)) console.warn('Analytics fetch failed:', e); }
      // #411: per-patient cost leaderboard — admin-only. Skip the fetch
      // entirely for non-admin callers (saves a guaranteed 403). renderCostLeaderboard
      // also hides the section when isGlobalAdmin is false, in case the order
      // of operations ever changes.
      if (isGlobalAdmin) {
        try {
          var leaderboard = await apiFetch('/api/cost-leaderboard?days=30&limit=50');
          if (leaderboard) renderCostLeaderboard(leaderboard);
          anyOk = true;
        } catch (e) { if (!trackBreaker(e)) console.warn('Cost leaderboard fetch failed:', e); }
      } else {
        // Non-admin caller: explicitly hide the widget so a previous admin
        // session's render doesn't bleed through after a page refresh.
        var lb = document.getElementById('cost-leaderboard-section');
        if (lb) lb.style.display = 'none';
      }
      try {
        var sched = await apiFetch('/api/schedules');
        if (sched) renderSchedules(sched);
        anyOk = true;
      } catch (e) { if (!trackBreaker(e)) console.warn('Schedules fetch failed:', e); }
      // Admin-only breaker widget (#469 Phase 7) — fire-and-forget, no-op when non-admin.
      updateBreakerWidget();
      // Budget pill (#442) — fire-and-forget, hides itself when tier is admin/paid_*.
      updateBudgetPill();
      hideSkeletons();

      // Breaker open — show friendly SK/EN countdown + auto-retry instead of a
      // red cascade. Any data we DID render during the partial load stays visible.
      if (breakerError) {
        setConnectionState('loading');
        showBreakerBanner(breakerError.retryAfterMs);
        return;
      }

      setConnectionState(anyOk ? 'ok' : 'offline');
      if (anyOk) {
        updateLastRefreshed();
        setTimeout(function(){ document.getElementById('next-sync').textContent = 'Next sync: ~' + (5 - Math.floor(((Date.now()/1000) % 300) / 60)) + ' min'; }, 100);
      }
      if (errors.length) {
        showError('Partial load failure: ' + errors.join('; '));
      }
    }

    // ── Renderers ────────────────────────────────────────────────────────

    function renderStatus(s) {
      window._lastStatus = s;
      document.getElementById('version-label').textContent = 'v' + (s.version || '?');

      // Google service connection badges
      var gs = s.google_services || {};
      ['drive', 'gmail', 'calendar'].forEach(function(svc) {
        var id = svc === 'calendar' ? 'svc-cal' : 'svc-' + svc;
        var el = document.getElementById(id);
        if (el) el.className = 'svc-badge ' + (gs[svc] ? 'on' : 'off');
      });
      // Update integration popover data
      var ipEmail = document.getElementById('ip-email');
      if (ipEmail) ipEmail.textContent = gs.owner_email || '—';
      var statusLabels = {true: {sk:'pripojené', en:'connected'}, false: {sk:'nepripojené', en:'not connected'}};
      var lang = window._dashLang || 'sk';
      ['drive','gmail'].forEach(function(svc) {
        var dot = document.getElementById('ip-' + svc + '-dot');
        var txt = document.getElementById('ip-' + svc + '-text');
        if (dot) dot.className = 'ip-status ' + (gs[svc] ? 'on' : 'off');
        if (txt) txt.textContent = statusLabels[!!gs[svc]][lang];
      });
      var calDot = document.getElementById('ip-cal-dot');
      var calTxt = document.getElementById('ip-cal-text');
      if (calDot) calDot.className = 'ip-status ' + (gs.calendar ? 'on' : 'off');
      if (calTxt) calTxt.textContent = statusLabels[!!gs.calendar][lang];
      // Per-service last sync times (#276)
      var sst = s.service_sync_times || {};
      ['drive','gmail','cal'].forEach(function(key) {
        var svcKey = key === 'cal' ? 'calendar' : key;
        var el = document.getElementById('ip-' + key + '-sync');
        if (el) {
          var t = sst[svcKey];
          if (t) {
            var d = new Date(t);
            var hm = d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
            el.textContent = hm;
          } else {
            el.textContent = '';
          }
        }
      });
      var ipFolder = document.getElementById('ip-folder');
      if (ipFolder) {
        ipFolder.textContent = '';
        if (gs.folder_id) {
          var folderLink = document.createElement('a');
          folderLink.href = 'https://drive.google.com/drive/folders/' + encodeURIComponent(gs.folder_id);
          folderLink.target = '_blank';
          folderLink.rel = 'noopener';
          folderLink.title = gs.folder_name || gs.folder_id;
          folderLink.style.cssText = 'color:var(--accent);text-decoration:none;display:inline-flex;align-items:center;gap:0.25rem';
          folderLink.textContent = '\uD83D\uDCC2 ' + (gs.folder_name || (gs.folder_id.substring(0, 12) + '\u2026'));
          ipFolder.appendChild(folderLink);
        } else {
          ipFolder.textContent = '\u2014';
        }
      }
      // Show GDrive connect banner if no folder set for this patient
      var gdriveBar = document.getElementById('gdrive-connect-bar');
      if (gdriveBar) gdriveBar.style.display = (!gs.drive || !gs.folder_id) && !window.DEMO_MODE ? 'flex' : 'none';

      const h = s.document_health || {};
      document.getElementById('card-total').textContent = h.total || 0;
      document.getElementById('card-synced').textContent = h.synced || 0;
      // Format uptime as human-readable (e.g. "2d 5h" or "3h 15m")
      var uptimeSec = s.uptime_s || 0;
      var uptimeText = '-';
      if (uptimeSec > 0) {
        var days = Math.floor(uptimeSec / 86400);
        var hours = Math.floor((uptimeSec % 86400) / 3600);
        var mins = Math.floor((uptimeSec % 3600) / 60);
        uptimeText = days > 0 ? days + 'd ' + hours + 'h' : hours > 0 ? hours + 'h ' + mins + 'm' : mins + 'm';
      }
      document.getElementById('card-uptime').textContent = uptimeText;

      // Per-service sync time badges (#276)
      var svcTimesEl = document.getElementById('svc-sync-times');
      if (svcTimesEl) {
        svcTimesEl.textContent = '';
        var sst2 = s.service_sync_times || {};
        [['Drive','gdrive'],['Gmail','gmail'],['Cal','calendar']].forEach(function(pair) {
          var t = sst2[pair[1]];
          if (t) {
            var sp = document.createElement('span');
            var d = new Date(t);
            sp.textContent = pair[0] + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
            svcTimesEl.appendChild(sp);
          }
        });
      }

      // Sync history
      const syncEl = document.getElementById('sync-rows');
      clearEl(syncEl);
      const syncs = s.recent_syncs || [];
      if (syncs.length === 0) {
        const empty = document.createElement('div');
        empty.style.cssText = 'padding:0.5rem 0;color:var(--text-muted);font-size:0.8125rem';
        empty.textContent = 'No recent syncs';
        syncEl.appendChild(empty);
      } else {
        syncs.forEach(function(sc) {
          const row = document.createElement('div');
          row.className = 'sync-row';

          const time = document.createElement('span');
          time.className = 'sync-time';
          var ts = sc.started_at || '';
          if (ts && !ts.endsWith('Z') && !ts.includes('+')) ts += 'Z';
          time.textContent = ts ? new Date(ts).toLocaleString() : '-';
          row.appendChild(time);

          const status = document.createElement('span');
          status.className = 'sync-status ' + (sc.status === 'completed' ? 'ok' : 'fail');
          status.textContent = sc.status || '-';
          row.appendChild(status);

          const trigger = document.createElement('span');
          trigger.className = 'sync-trigger';
          trigger.textContent = sc.trigger || '-';
          row.appendChild(trigger);

          const dur = document.createElement('span');
          dur.className = 'sync-dur';
          dur.textContent = sc.duration_s ? sc.duration_s + 's' : '-';
          row.appendChild(dur);

          if (sc.new > 0) {
            const newSpan = document.createElement('span');
            newSpan.className = 'sync-new';
            newSpan.textContent = sc.new + ' new';
            row.appendChild(newSpan);
          }

          if (sc.errors) {
            const err = document.createElement('span');
            err.style.cssText = 'color:var(--red);font-size:0.75rem';
            err.textContent = sc.errors + ' err';
            row.appendChild(err);
          }

          syncEl.appendChild(row);
        });
      }

      // Manifest status (#182)
      var ms = document.getElementById('manifest-status');
      var ml = document.getElementById('manifest-live');
      if (ms && s.document_health) {
        var dh = s.document_health;
        ms.textContent = '(' + (dh.total || 0) + ' docs)';
      }
      if (ml && s.last_sync_at) {
        ml.textContent = 'Last export: ' + new Date(s.last_sync_at)
          .toLocaleString();
      }
    }

    function renderDocs(data) {
      const summary = data.summary || {};
      const total = summary.total || 0;
      const fc = summary.fully_complete || 0;
      const gaps = total - fc;

      document.getElementById('card-complete').textContent = fc;
      document.getElementById('card-gaps').textContent = gaps;

      // Pipeline funnel
      var stages = [
        { label: 'Total', count: total },
        { label: 'OCR', count: summary.with_ocr || 0 },
        { label: 'AI Summary', count: summary.with_ai || 0 },
        { label: 'Metadata', count: summary.with_metadata || 0 },
        { label: 'Date', count: summary.with_date || 0 },
        { label: 'Institution', count: summary.with_institution || 0 },
        { label: 'Synced', count: summary.synced || 0 },
        { label: 'Named', count: summary.standard_named || 0 },
        { label: 'Complete', count: fc, complete: true }
      ];
      var funnelEl = document.getElementById('funnel-rows');
      clearEl(funnelEl);
      stages.forEach(function(st) {
        var pct = total > 0 ? (st.count / total * 100) : 0;
        var row = document.createElement('div');
        row.className = 'funnel-row';

        var label = document.createElement('span');
        label.className = 'funnel-label';
        label.textContent = st.label;
        row.appendChild(label);

        var barBg = document.createElement('div');
        barBg.className = 'funnel-bar-bg';
        var bar = document.createElement('div');
        bar.className = st.complete ? 'funnel-bar complete' : 'funnel-bar';
        bar.style.width = pct + '%';
        barBg.appendChild(bar);
        row.appendChild(barBg);

        var count = document.createElement('span');
        count.className = 'funnel-count';
        count.textContent = st.count;
        row.appendChild(count);

        row.style.cursor = 'pointer';
        row.title = 'Click for details';
        row.onclick = (function(stage) { return function() {
          var info = {
            'Total': 'Total documents imported from Google Drive. Triggered by: scheduled GDrive sync (every 5 min) or manual upload.',
            'OCR': 'Text extraction from scanned PDFs and images. Triggered by: new document import. Uses: Claude Haiku vision model for scanned docs, PyMuPDF for text PDFs.',
            'AI Summary': 'AI-generated summary and tags. Triggered by: after OCR completion. Uses: Claude Haiku with system prompt asking for medical document summary, key findings, diagnoses.',
            'Metadata': 'Structured data extraction (document_type, findings, diagnoses, medications, providers). Triggered by: after AI summary. Uses: Claude Haiku with JSON schema prompt.',
            'Date': 'Document date extracted from content via AI or parsed from filename. Required for year-month folder organization.',
            'Institution': 'Medical institution/provider identified from document content. Mapped via PROVIDER_TO_INSTITUTION. Required for standard filename.',
            'Synced': 'Bidirectional Google Drive sync. Files exported to organized category/year-month folders with standard naming.',
            'Named': 'Standard filename format: YYYYMMDD_PatientName_Institution_Category_Description.ext. Auto-renamed during sync.',
            'Complete': 'All pipeline stages passed: OCR + AI Summary + Metadata + Date + Institution + Synced + Named. The document is fully processed.'
          };
          var existing = document.getElementById('funnel-tooltip');
          if (existing) { existing.remove(); if (existing.dataset.stage === stage.label) return; }
          var tip = document.createElement('div');
          tip.id = 'funnel-tooltip';
          tip.dataset.stage = stage.label;
          tip.style.cssText = 'background:var(--surface);border:1px solid var(--border);border-radius:0.5rem;padding:1rem;margin:0.75rem 0;font-size:0.8125rem;color:var(--text-muted);line-height:1.6';
          var title = document.createElement('strong');
          title.style.color = 'var(--accent)';
          title.textContent = stage.label;
          tip.appendChild(title);
          tip.appendChild(document.createElement('br'));
          tip.appendChild(document.createTextNode(info[stage.label] || 'Pipeline stage'));
          funnelEl.appendChild(tip);
        }; })(st);

        funnelEl.appendChild(row);
      });

      // Store for sorting
      docsData = data.documents || [];
      renderTable();
      renderRemedies(summary);
      updateFilterCounts(summary);
    }

    function renderTable() {
      var tbody = document.getElementById('doc-tbody');
      clearEl(tbody);

      // Text search filter
      var searchEl = document.getElementById('doc-search');
      var query = (searchEl ? searchEl.value : '').toLowerCase().trim();
      var filtered = docsData;
      if (query) {
        filtered = docsData.filter(function(d) {
          return (d.filename || '').toLowerCase().includes(query)
            || (d.category || '').toLowerCase().includes(query)
            || (d.institution || '').toLowerCase().includes(query)
            || (d.date || '').includes(query);
        });
      }

      if (filtered.length === 0) {
        var emptyRow = document.createElement('tr');
        var emptyTd = document.createElement('td');
        emptyTd.setAttribute('colspan', '11');
        emptyTd.className = 'table-empty';
        emptyTd.textContent = query ? 'No documents match "' + query + '"' : 'No documents match this filter';
        emptyRow.appendChild(emptyTd);
        tbody.appendChild(emptyRow);
        return;
      }

      // Sort (booleans: false < true so problems sort first in ascending)
      var sorted = filtered.slice().sort(function(a, b) {
        var va = a[sortCol];
        var vb = b[sortCol];
        if (va == null) va = '';
        if (vb == null) vb = '';
        if (typeof va === 'boolean') { va = va ? 1 : 0; vb = vb ? 1 : 0; }
        if (typeof va === 'string') va = va.toLowerCase();
        if (typeof vb === 'string') vb = vb.toLowerCase();
        if (va < vb) return sortAsc ? -1 : 1;
        if (va > vb) return sortAsc ? 1 : -1;
        return 0;
      });

      sorted.forEach(function(d) {
        var tr = document.createElement('tr');

        // Filename
        var tdFn = document.createElement('td');
        tdFn.className = 'filename';
        if (d.gdrive_id && d.gdrive_id !== 'demo') {
          var a = document.createElement('a');
          a.href = 'https://drive.google.com/file/d/' + encodeURIComponent(d.gdrive_id) + '/view';
          a.target = '_blank';
          a.rel = 'noopener';
          a.textContent = d.filename || '';
          tdFn.appendChild(a);
        } else {
          tdFn.textContent = d.filename || '';
        }
        tr.appendChild(tdFn);

        // Category
        var tdCat = document.createElement('td');
        var badge = document.createElement('span');
        badge.className = 'category-badge';
        badge.textContent = d.category || '';
        tdCat.appendChild(badge);
        tr.appendChild(tdCat);

        // Institution
        var tdInst = document.createElement('td');
        tdInst.textContent = d.institution || '-';
        tdInst.style.fontSize = '0.75rem';
        tr.appendChild(tdInst);

        // Date
        var tdDate = document.createElement('td');
        tdDate.textContent = d.date || '-';
        tr.appendChild(tdDate);

        // Status dots: OCR, AI, Meta, Date, Inst., Sync, Name
        var fields = ['has_ocr', 'has_ai', 'has_metadata', 'has_date', 'has_institution', 'is_synced', 'is_standard_name'];
        fields.forEach(function(f) {
          var td = document.createElement('td');
          td.appendChild(makeDot(d[f]));
          tr.appendChild(td);
        });

        tr.style.cursor = 'pointer';
        tr.onclick = (function(doc, clickedTr) { return function() {
          var existingDetail = clickedTr.nextElementSibling;
          if (existingDetail && existingDetail.className === 'doc-detail-row') {
            existingDetail.remove();
            return;
          }
          document.querySelectorAll('.doc-detail-row').forEach(function(el) { el.remove(); });

          var detailRow = document.createElement('tr');
          detailRow.className = 'doc-detail-row';
          var detailTd = document.createElement('td');
          detailTd.setAttribute('colspan', '11');
          detailTd.style.cssText = 'padding:0.5rem;background:rgba(20,184,166,0.04)';

          // Show loading state
          var loadingDiv = document.createElement('div');
          loadingDiv.className = 'doc-detail-loading';
          var loadingSpan = document.createElement('span');
          loadingSpan.setAttribute('data-sk', 'Na\u010d\u00edtavam detail\u2026');
          loadingSpan.setAttribute('data-en', 'Loading detail\u2026');
          loadingSpan.textContent = 'Loading detail\u2026';
          loadingDiv.appendChild(loadingSpan);
          detailTd.appendChild(loadingDiv);
          detailRow.appendChild(detailTd);
          clickedTr.parentNode.insertBefore(detailRow, clickedTr.nextSibling);
          applyDashLang();

          // Fetch detail from API (or use demo data)
          var detailPromise = window.DEMO_MODE
            ? Promise.resolve(getDemoDocDetail(doc))
            : apiFetch('/api/doc-detail/' + doc.id);
          detailPromise.then(function(data) {
            if (data.error) {
              loadingDiv.textContent = data.error;
              return;
            }
            // Clear loading
            while (detailTd.firstChild) detailTd.removeChild(detailTd.firstChild);

            var panel = document.createElement('div');
            panel.className = 'doc-detail-panel';

            // Close button
            var closeBtn = document.createElement('button');
            closeBtn.className = 'detail-close';
            closeBtn.textContent = '\u00d7';
            closeBtn.title = 'Close';
            closeBtn.onclick = function(e) { e.stopPropagation(); detailRow.remove(); };
            panel.appendChild(closeBtn);

            // ── Left column: preview (skip if no preview available) ──
            if (data.preview_url || data.gdrive_url) {
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

              if (data.gdrive_url && /^https:\/\//.test(data.gdrive_url)) {
                var gdriveLink = document.createElement('a');
                gdriveLink.href = data.gdrive_url;
                gdriveLink.target = '_blank';
                gdriveLink.rel = 'noopener';
                gdriveLink.style.cssText = 'display:inline-block;padding:0.375rem 0.75rem;background:var(--accent);color:#fff;border-radius:0.375rem;font-size:0.75rem;font-weight:600;text-decoration:none;text-align:center';
                var gdriveLinkSpan = document.createElement('span');
                gdriveLinkSpan.setAttribute('data-sk', 'Otvori\u0165 v GDrive');
                gdriveLinkSpan.setAttribute('data-en', 'Open in GDrive');
                gdriveLinkSpan.textContent = 'Open in GDrive';
                gdriveLink.appendChild(gdriveLinkSpan);
                left.appendChild(gdriveLink);
              }

              panel.appendChild(left);
            } else {
              // No preview — make right column full width
              panel.style.gridTemplateColumns = '1fr';
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
                textDiv.textContent = page.text || '';
                pageDiv.appendChild(textDiv);

                right.appendChild(pageDiv);
              });
            } else {
              var noOcr = document.createElement('div');
              noOcr.className = 'doc-ocr-page';
              var noOcrSpan = document.createElement('span');
              noOcrSpan.style.cssText = 'color:var(--text-muted);font-size:0.8125rem';
              noOcrSpan.setAttribute('data-sk', 'Nie s\u00fa dostupn\u00e9 OCR d\u00e1ta');
              noOcrSpan.setAttribute('data-en', 'No OCR data available');
              noOcrSpan.textContent = 'No OCR data available';
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
            catBadge.textContent = data.category || 'other';
            addMetaRow('Kateg\u00f3ria', 'Category', catBadge);

            addMetaRow('D\u00e1tum', 'Date', data.document_date || '-');
            addMetaRow('In\u0161tit\u00facia', 'Institution', data.institution || '-');
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
              addMetaRow('AI Zhrnutie', 'AI Summary', data.ai_summary);
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
            panel.appendChild(right);

            detailTd.appendChild(panel);
            applyDashLang();
          }).catch(function(err) {
            loadingDiv.textContent = 'Error: ' + (err.message || 'unknown');
          });
        }; })(d, tr);

        tbody.appendChild(tr);
      });
    }

    // ── Processing Schedules (#287) ──────────────────────────────────
    var schedFilter = 'all';
    var schedData = null;

    document.getElementById('sched-groups').addEventListener('click', function(e) {
      if (!e.target.classList.contains('pill')) return;
      e.target.parentElement.querySelectorAll('.pill').forEach(function(p) { p.classList.remove('active'); });
      e.target.classList.add('active');
      schedFilter = e.target.dataset.schedFilter;
      if (schedData) renderSchedules(schedData);
    });

    function renderSchedules(data) {
      schedData = data;
      var container = document.getElementById('sched-jobs');
      clearEl(container);

      var jobs = (data.jobs || []).filter(function(j) {
        return schedFilter === 'all' || j.group === schedFilter;
      });

      var groupColors = { sync: 'var(--accent)', pipeline: 'var(--purple)', housekeeping: '#f59e0b', infra: 'var(--text-muted)' };

      jobs.forEach(function(j) {
        var row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:0.75rem;padding:0.5rem 0;border-bottom:1px solid var(--border)';

        // Status indicator
        var dot = document.createElement('span');
        dot.style.cssText = 'width:8px;height:8px;border-radius:50%;flex-shrink:0;';
        if (j.running) { dot.style.background = 'var(--accent)'; dot.style.boxShadow = '0 0 6px var(--accent)'; }
        else if (j.last_error && !j.last_ok) { dot.style.background = '#ef4444'; }
        else if (j.last_ok) { dot.style.background = '#22c55e'; }
        else { dot.style.background = 'var(--text-muted)'; }
        row.appendChild(dot);

        // Name + schedule
        var info = document.createElement('div');
        info.style.cssText = 'flex:1;min-width:0';
        var nameEl = document.createElement('div');
        nameEl.style.cssText = 'font-weight:600;color:var(--text);font-size:0.8125rem';
        nameEl.textContent = j.name;
        info.appendChild(nameEl);
        var descEl = document.createElement('div');
        descEl.style.cssText = 'font-size:0.7rem;color:var(--text-muted);margin-top:0.125rem';
        descEl.textContent = j.desc;
        info.appendChild(descEl);
        row.appendChild(info);

        // Schedule badge
        var badge = document.createElement('span');
        badge.style.cssText = 'font-size:0.7rem;padding:0.125rem 0.5rem;border-radius:1rem;background:' + (groupColors[j.group] || 'var(--text-muted)') + ';color:white;white-space:nowrap;flex-shrink:0';
        badge.textContent = j.schedule;
        row.appendChild(badge);

        // Last run
        var status = document.createElement('div');
        status.style.cssText = 'text-align:right;font-size:0.7rem;color:var(--text-muted);min-width:100px;flex-shrink:0';
        if (j.running) { status.textContent = 'Running...'; status.style.color = 'var(--accent)'; }
        else if (j.last_ok) {
          var ago = Math.round((Date.now() - new Date(j.last_ok + 'Z').getTime()) / 60000);
          status.textContent = (ago < 1 ? '<1' : ago) + 'm ago';
          if (j.last_duration_s) status.textContent += ' (' + j.last_duration_s + 's)';
        }
        else { status.textContent = 'Not run yet'; }
        if (j.last_error) {
          var errLine = document.createElement('div');
          errLine.style.cssText = 'color:#ef4444;font-size:0.65rem;margin-top:0.125rem';
          errLine.textContent = 'Error: ' + (j.last_error_msg || 'unknown').substring(0, 60);
          status.appendChild(errLine);
        }
        row.appendChild(status);

        container.appendChild(row);
      });

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
        barBg.style.cssText = 'flex:1;height:14px;background:var(--bg);border-radius:7px;overflow:hidden';
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

    async function loadManifestPreview() {
      var pre = document.getElementById('manifest-preview');
      pre.style.display = 'block';
      pre.textContent = 'Loading manifest...';
      try {
        var data = await apiFetch('/api/manifest');
        var summary = {
          documents: (data.documents || []).length,
          treatment_events: (data.treatment_events || []).length,
          conversation_entries: (data.conversation_entries || []).length,
          research_entries: (data.research_entries || []).length,
          generated_at: data.generated_at
        };
        pre.textContent = JSON.stringify(summary, null, 2) + '\n\n// Full manifest: ' + JSON.stringify(data).length + ' bytes\n// Use "Download JSON" for the complete file';
      } catch (e) {
        pre.textContent = 'Error: ' + e.message;
      }
    }

    async function downloadManifest() {
      try {
        var data = await apiFetch('/api/manifest');
        var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = '_manifest.json';
        a.click();
        URL.revokeObjectURL(url);
      } catch (e) {
        alert('Failed to download manifest: ' + e.message);
      }
    }

    function renderRemedies(summary) {
      var panel = document.getElementById('remedies-panel');
      var rowsEl = document.getElementById('remedies-rows');
      clearEl(rowsEl);

      var total = summary.total || 0;
      var gaps = [];

      function check(label, count, tool) {
        var missing = total - (count || 0);
        if (missing > 0) gaps.push({ label: label, missing: missing, tool: tool });
      }

      check('Missing OCR', summary.with_ocr, 'enhance_documents');
      check('Missing AI Summary', summary.with_ai, 'enhance_documents');
      check('Missing Metadata', summary.with_metadata, 'extract_all_metadata');
      check('Not Synced', summary.synced, 'gdrive_sync');
      check('Not Standard Named', summary.standard_named, 'rename_documents_to_standard');
      check('Missing Date', summary.with_date, 'extract_document_metadata');
      check('Missing Institution', summary.with_institution, 'extract_document_metadata');

      if (gaps.length === 0) {
        panel.style.display = 'none';
        return;
      }

      panel.style.display = 'block';
      gaps.forEach(function(g) {
        var row = document.createElement('div');
        row.className = 'remedy-row';

        var lbl = document.createElement('span');
        lbl.className = 'remedy-label';
        lbl.textContent = g.label;
        row.appendChild(lbl);

        var cnt = document.createElement('span');
        cnt.className = 'remedy-count';
        cnt.textContent = g.missing;
        row.appendChild(cnt);

        var act = document.createElement('span');
        act.className = 'remedy-action';
        act.textContent = g.tool;
        row.appendChild(act);

        rowsEl.appendChild(row);
      });
    }

    function updateFilterCounts(summary) {
      var total = summary.total || 0;
      var counts = {
        'all': total,
        'incomplete': total - (summary.fully_complete || 0),
        'missing_ocr': total - (summary.with_ocr || 0),
        'missing_ai': total - (summary.with_ai || 0),
        'missing_metadata': total - (summary.with_metadata || 0),
        'missing_date': total - (summary.with_date || 0),
        'missing_institution': total - (summary.with_institution || 0),
        'not_synced': total - (summary.synced || 0),
        'not_renamed': total - (summary.standard_named || 0),
      };
      document.querySelectorAll('.pill').forEach(function(pill) {
        var filter = pill.dataset.filter;
        var count = counts[filter];
        // Remove existing count badge
        var existing = pill.querySelector('.pill-count');
        if (existing) pill.removeChild(existing);
        // Add count badge
        if (count !== undefined) {
          var badge = document.createElement('span');
          badge.className = 'pill-count';
          badge.textContent = count;
          pill.appendChild(badge);
        }
      });
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

    // ── Reconciliation ───────────────────────────────────────────────────

    function renderReconciliation(data) {
      var panel = document.getElementById('recon-panel');
      var summaryEl = document.getElementById('recon-summary');
      var issuesEl = document.getElementById('recon-issues');
      clearEl(summaryEl);
      clearEl(issuesEl);

      var hasIssues = !data.healthy ||
        (data.in_db_not_gdrive && data.in_db_not_gdrive.length > 0) ||
        (data.in_gdrive_not_db && data.in_gdrive_not_db.length > 0) ||
        (data.filename_mismatch && data.filename_mismatch.length > 0);

      if (!hasIssues) {
        panel.style.display = 'none';
        return;
      }

      panel.style.display = 'block';

      var items = [
        { label: 'DB docs', value: data.db_count },
        { label: 'GDrive files', value: data.gdrive_count },
      ];
      items.forEach(function(it) {
        var s = document.createElement('span');
        var v = document.createElement('strong');
        v.style.color = 'var(--accent)';
        v.textContent = it.value;
        s.appendChild(v);
        s.appendChild(document.createTextNode(' ' + it.label));
        summaryEl.appendChild(s);
      });

      if (data.healthy) {
        var ok = document.createElement('span');
        ok.style.color = 'var(--green)';
        ok.textContent = 'All synced';
        summaryEl.appendChild(ok);
      }

      function addIssueSection(title, items, cols) {
        if (!items || items.length === 0) return;
        var h = document.createElement('div');
        h.style.cssText = 'font-size:0.8125rem;font-weight:600;margin:0.75rem 0 0.25rem;color:var(--red)';
        h.textContent = title + ' (' + items.length + ')';
        issuesEl.appendChild(h);
        items.forEach(function(item) {
          var row = document.createElement('div');
          row.className = 'sync-row';
          cols.forEach(function(col) {
            var sp = document.createElement('span');
            sp.style.cssText = 'font-size:0.75rem;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
            sp.textContent = item[col] || '-';
            row.appendChild(sp);
          });
          issuesEl.appendChild(row);
        });
      }

      addIssueSection('In DB but missing from GDrive', data.in_db_not_gdrive, ['filename', 'gdrive_id']);
      addIssueSection('In GDrive but not in DB (orphans)', data.in_gdrive_not_db, ['name', 'gdrive_id']);
      addIssueSection('Filename mismatch', data.filename_mismatch, ['db_filename', 'gdrive_filename']);
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
      var lang = window._dashLang || 'sk';
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
        body.textContent = (window._dashLang === 'sk' ? 'Žiadne dáta' : 'No data');
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
          row.title = (window._dashLang === 'sk' ? 'Klikni pre detail' : 'Click to drill in');
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
      totalLabel.textContent = (window._dashLang === 'sk' ? 'Spolu' : 'Total') +
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

    document.getElementById('filters').addEventListener('click', function(e) {
      if (!e.target.classList.contains('pill')) return;
      document.querySelectorAll('.pill').forEach(function(p) { p.classList.remove('active'); });
      e.target.classList.add('active');
      currentFilter = e.target.dataset.filter;
      refresh();
    });

    document.querySelectorAll('th[data-sort]').forEach(function(th) {
      th.addEventListener('click', function() {
        var col = th.dataset.sort;
        if (sortCol === col) {
          sortAsc = !sortAsc;
        } else {
          sortCol = col;
          sortAsc = true;
        }
        document.querySelectorAll('th .sort-arrow').forEach(function(a) { a.textContent = ''; });
        th.querySelector('.sort-arrow').textContent = sortAsc ? '\u25B2' : '\u25BC';
        renderTable();
      });
    });

    /* ── Bug Report ──────────────────────────────────────────────────── */
    (function() {
      var fab = document.getElementById('bug-fab');
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
          sync_history_rows: document.querySelectorAll('.sync-history tr').length,
          document_rows: document.querySelectorAll('#doc-table-body tr').length,
          current_filter: typeof currentFilter !== 'undefined' ? currentFilter : '',
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
          screenshot.style.display = 'block';
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
          page_url: location.href,
          page_section: location.hash || 'dashboard',
          page_state: collectPageState(),
          console_errors: consoleErrors.join('\n'),
          screenshot: screenshotB64,
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

