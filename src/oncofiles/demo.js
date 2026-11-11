/* Oncofiles public demo (Nov 2026 redesign).
 *
 * One invented patient, rendered from /api/demo-data. This page loads no
 * dashboard.js and calls no other endpoint: the fetch() wrapper below refuses
 * every same-origin request except /api/demo-data (#605). All DOM is built
 * with createElement/textContent; there is no innerHTML. Interactions are
 * declared with data-action="fn|arg" and routed through one dispatcher, so
 * the markup carries no inline event handlers.
 */
(function () {
  'use strict';

  var DEMO_ALLOWED_PATHS = ['/api/demo-data'];
  var realFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var raw = typeof input === 'string' ? input : (input && input.url) || '';
    var u = null;
    try { u = new URL(raw, window.location.origin); } catch (e) { u = null; }
    if (!u || (u.origin === window.location.origin && DEMO_ALLOWED_PATHS.indexOf(u.pathname) === -1)) {
      return Promise.reject(new Error('Not available in the demo'));
    }
    return realFetch(input, init);
  };

  var lang = 'sk';
  try { lang = localStorage.getItem('oncofiles_lang') === 'en' ? 'en' : 'sk'; } catch (e) { /* private mode */ }
  try {
    var qp = new URLSearchParams(window.location.search).get('lang');
    if (qp === 'en' || qp === 'sk') lang = qp;
  } catch (e) { /* old browser */ }
  var data = null;
  var currentDoc = null;
  var currentLine = null;
  var SVG = 'http://www.w3.org/2000/svg';

  var STATE = {
    machine: ['overené strojom', 'machine-verified'],
    human: ['overené človekom', 'human-verified'],
    unverified: ['neoverené', 'unverified']
  };
  var STATE_NOTE = {
    machine: ['Druhé nezávislé čítanie dokumentu našlo rovnaké číslo aj rovnaký dátum odberu.',
      'A second independent reading of the document found the same number and the same sample date.'],
    human: ['Hodnotu skontroloval človek podľa tohto riadku.', 'A person checked the value against this line.'],
    date_mismatch: ['Pri tejto hodnote je uvedený iný odber ako pri ostatných a druhé čítanie dátum nepotvrdilo. Do trendov sa nepočíta, kým ju niekto neoverí.',
      'This value refers to a different sample than the rest, and the second reading did not confirm the date. It stays out of trends until someone checks it.']
  };
  var CATEGORY = { labs: ['Laboratórium', 'Lab report'], imaging: ['Zobrazovanie', 'Imaging'], report: ['Lekárska správa', 'Doctor’s report'] };
  var MONTHS_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function t(pair) { return lang === 'sk' ? pair[0] : pair[1]; }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function fmtDate(iso) {
    var p = iso.split('-');
    var d = parseInt(p[2], 10), m = parseInt(p[1], 10);
    return lang === 'sk' ? d + '. ' + m + '. ' + p[0] : d + ' ' + MONTHS_EN[m - 1] + ' ' + p[0];
  }
  function srcText(page, line) { return lang === 'sk' ? 'str. ' + page + ', r. ' + line : 'p. ' + page + ', l. ' + line; }
  function stateNode(state) { return el('span', 'st st-' + state, t(STATE[state])); }
  function docById(id) { for (var i = 0; i < data.documents.length; i++) if (data.documents[i].id === id) return data.documents[i]; return null; }
  function seriesName(s) { return lang === 'sk' ? s.name_sk : s.name_en; }
  // Payload fields with an English twin (`text_en`, `cells_en`, `filename_en`,
  // `institution_en`, `name_en`): EN mode shows the twin, SK the original.
  function loc(obj, key) {
    var en = obj[key + '_en'];
    return lang === 'en' && en !== undefined && en !== null ? en : obj[key];
  }

  /* ── Static text ───────────────────────────────────────────────── */
  function applyStatic() {
    document.documentElement.lang = lang;
    document.querySelectorAll('[data-sk][data-en]').forEach(function (n) { n.textContent = n.getAttribute('data-' + lang); });
    document.querySelectorAll('[data-sk-label][data-en-label]').forEach(function (n) { n.setAttribute('aria-label', n.getAttribute('data-' + lang + '-label')); });
    var b = document.getElementById('lang-toggle');
    if (b) b.textContent = lang === 'sk' ? 'EN' : 'SK';
  }

  /* ── Overview ──────────────────────────────────────────────────── */
  function renderOverview() {
    var docs = data.documents, labs = docs.filter(function (d) { return d.category === 'labs'; }).length;
    document.getElementById('patient-name').textContent = loc(data.patient, 'name');
    document.getElementById('patient-sub').textContent = lang === 'sk'
      ? 'Vymyslená pacientka. ' + docs.length + ' dokumentov, z toho ' + labs + ' laboratórne výsledky.'
      : 'An invented patient. ' + docs.length + ' documents, ' + labs + ' of them lab reports.';
    var counts = { machine: 0, human: 0, unverified: 0 }, total = 0;
    data.series.forEach(function (s) { s.points.forEach(function (p) { counts[p.state] += 1; total += 1; }); });
    var v = document.getElementById('verify-summary');
    clear(v);
    v.appendChild(el('b', null, String(total)));
    v.appendChild(document.createTextNode(lang === 'sk' ? ' prečítaných hodnôt: ' : ' values read: '));
    ['machine', 'human', 'unverified'].forEach(function (k, i) {
      var item = el('span', 'vs');
      item.appendChild(document.createTextNode(counts[k] + '\u00a0'));
      item.appendChild(stateNode(k));
      v.appendChild(item);
      v.appendChild(document.createTextNode(i < 2 ? ', ' : '. '));
    });
    v.appendChild(document.createTextNode(lang === 'sk'
      ? 'Neoverené hodnoty sa do trendov nepočítajú.'
      : 'Unverified values are left out of trends.'));
  }

  function sparkline(s) {
    var w = 120, h = 32, pad = 4;
    var vals = s.points.map(function (p) { return p.value; });
    var lo = Math.min.apply(null, vals.concat(s.low !== null ? [s.low] : [])),
        hi = Math.max.apply(null, vals.concat(s.high !== null ? [s.high] : []));
    if (hi === lo) hi = lo + 1;
    function x(i) { return pad + (s.points.length < 2 ? 0 : i * (w - 2 * pad) / (s.points.length - 1)); }
    function y(v) { return h - pad - (v - lo) * (h - 2 * pad) / (hi - lo); }
    var svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('class', 'spark');
    svg.setAttribute('aria-hidden', 'true');
    var band = document.createElementNS(SVG, 'rect');
    var bTop = y(s.high !== null ? s.high : hi), bBot = y(s.low !== null ? s.low : lo);
    band.setAttribute('x', '0'); band.setAttribute('width', String(w));
    band.setAttribute('y', String(Math.min(bTop, bBot))); band.setAttribute('height', String(Math.abs(bBot - bTop)));
    band.setAttribute('class', 'band');
    svg.appendChild(band);
    var d = '';
    s.points.forEach(function (p, i) {
      if (p.state === 'unverified') return;
      d += (d ? ' L' : 'M') + x(i).toFixed(1) + ' ' + y(p.value).toFixed(1);
    });
    var path = document.createElementNS(SVG, 'path');
    path.setAttribute('d', d); path.setAttribute('class', 'ln');
    path.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.appendChild(path);
    s.points.forEach(function (p, i) {
      var c = document.createElementNS(SVG, 'circle');
      c.setAttribute('cx', x(i).toFixed(1)); c.setAttribute('cy', y(p.value).toFixed(1));
      c.setAttribute('r', '2.6'); c.setAttribute('class', p.state === 'unverified' ? 'pt-u' : 'pt');
      svg.appendChild(c);
    });
    return svg;
  }

  function renderTrends() {
    var tb = document.getElementById('trend-rows');
    clear(tb);
    data.series.forEach(function (s) {
      var verified = s.points.filter(function (p) { return p.state !== 'unverified'; });
      var last = verified[verified.length - 1];
      if (!last) return;
      var tr = document.createElement('tr');
      var c1 = el('td');
      c1.appendChild(el('span', 'name', seriesName(s)));
      c1.appendChild(el('span', 'ref', (lang === 'sk' ? 'norma ' : 'range ') + numFmt(s.ref) + ' ' + s.unit));
      var c2 = el('td', 'c-spark'); c2.appendChild(sparkline(s));
      var c3 = el('td', 'c-num');
      c3.appendChild(el('b', null, numFmt(last.value_text))); c3.appendChild(el('span', 'unit', s.unit));
      var c4 = el('td', 'c-date');
      c4.appendChild(document.createTextNode(fmtDate(last.date) + ' '));
      c4.appendChild(document.createElement('br'));
      c4.appendChild(stateNode(last.state));
      var c5 = el('td');
      var b = el('button', 'src', srcText(last.page, last.line));
      b.type = 'button';
      b.setAttribute('data-action', 'openSource|' + last.doc + '|' + last.line);
      b.setAttribute('aria-label', (lang === 'sk' ? 'Otvoriť zdroj: ' : 'Open source: ') + seriesName(s) + ', ' + srcText(last.page, last.line));
      c5.appendChild(b);
      [c1, c2, c3, c4, c5].forEach(function (c) { tr.appendChild(c); });
      tb.appendChild(tr);
    });
  }

  function renderDocList() {
    var ol = document.getElementById('doc-list');
    clear(ol);
    data.documents.forEach(function (d) {
      var li = document.createElement('li');
      var b = el('button');
      b.type = 'button';
      b.setAttribute('data-action', 'openDoc|' + d.id);
      if (currentDoc === d.id) b.setAttribute('aria-current', 'true');
      b.appendChild(el('span', 'd', fmtDate(d.date)));
      b.appendChild(el('span', 't', lang === 'sk' ? d.title_sk : d.title_en));
      var n = valuesOf(d.id).length;
      b.appendChild(el('span', 'c', t(CATEGORY[d.category]) + (n ? (lang === 'sk' ? ', ' + n + ' hodnoty' : ', ' + n + ' values') : '')));
      li.appendChild(b);
      ol.appendChild(li);
    });
  }

  /* ── Document view ─────────────────────────────────────────────── */
  function valuesOf(docId) {
    var out = [];
    data.series.forEach(function (s) {
      s.points.forEach(function (p) { if (p.doc === docId) out.push({ s: s, p: p }); });
    });
    out.sort(function (a, b) { return a.p.line - b.p.line; });
    return out;
  }

  function stamp() {
    var svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('viewBox', '0 0 100 100'); svg.setAttribute('class', 'stamp'); svg.setAttribute('aria-hidden', 'true');
    var ring = document.createElementNS(SVG, 'circle');
    ring.setAttribute('cx', '50'); ring.setAttribute('cy', '50'); ring.setAttribute('r', '44');
    ring.setAttribute('fill', 'none'); ring.setAttribute('stroke', '#2c4f9e'); ring.setAttribute('stroke-width', '2.5');
    svg.appendChild(ring);
    var inner = document.createElementNS(SVG, 'circle');
    inner.setAttribute('cx', '50'); inner.setAttribute('cy', '50'); inner.setAttribute('r', '33');
    inner.setAttribute('fill', 'none'); inner.setAttribute('stroke', '#2c4f9e'); inner.setAttribute('stroke-width', '1');
    svg.appendChild(inner);
    var txt = document.createElementNS(SVG, 'text');
    txt.setAttribute('x', '50'); txt.setAttribute('y', '47'); txt.setAttribute('text-anchor', 'middle');
    txt.setAttribute('font-size', '9'); txt.setAttribute('fill', '#2c4f9e'); txt.setAttribute('font-family', 'Georgia, serif');
    txt.textContent = lang === 'sk' ? 'NEMOCNICA' : 'EXAMPLE';
    svg.appendChild(txt);
    var txt2 = txt.cloneNode(); txt2.setAttribute('y', '59'); txt2.textContent = lang === 'sk' ? 'PRÍKLADOV' : 'HOSPITAL';
    svg.appendChild(txt2);
    var sig = document.createElementNS(SVG, 'path');
    sig.setAttribute('d', 'M14 74 C 26 60, 34 86, 46 70 S 66 62, 74 76 S 88 70, 94 64');
    sig.setAttribute('fill', 'none'); sig.setAttribute('stroke', '#1d2b55'); sig.setAttribute('stroke-width', '1.6');
    svg.appendChild(sig);
    return svg;
  }

  function renderDoc() {
    var d = docById(currentDoc);
    if (!d) return;
    document.getElementById('doc-title').textContent = lang === 'sk' ? d.title_sk : d.title_en;
    document.getElementById('doc-date').textContent = (lang === 'sk' ? 'Vydané ' : 'Issued ') + fmtDate(d.date) + ',';
    document.getElementById('doc-inst').textContent = loc(d, 'institution');
    document.getElementById('doc-file').textContent = loc(d, 'filename');
    var sheet = document.getElementById('sheet');
    clear(sheet);
    d.pages[0].lines.forEach(function (ln) {
      var row = el('div', 'ln' + (ln.kind ? ' k-' + ln.kind : ''));
      row.setAttribute('data-line', String(ln.n));
      row.appendChild(el('span', 'no', String(ln.n)));
      if (ln.cells) {
        var cells = el('span', 'cells');
        loc(ln, 'cells').forEach(function (c) { cells.appendChild(el('span', null, c)); });
        row.appendChild(cells);
      } else {
        row.appendChild(el('span', null, loc(ln, 'text') || ' '));
      }
      sheet.appendChild(row);
    });
    if (d.category === 'labs') sheet.appendChild(stamp());
    document.getElementById('page-cap').textContent = lang === 'sk'
      ? 'Strana 1 z 1. Žltou je zvýraznený riadok, z ktorého bola hodnota prečítaná.'
      : 'Page 1 of 1. The line a value was read from is highlighted in yellow.';

    var vals = valuesOf(d.id);
    var tb = document.getElementById('value-rows');
    clear(tb);
    var table = tb.parentNode;
    var hint = document.getElementById('extract-hint');
    if (!vals.length) {
      table.hidden = true;
      hint.textContent = lang === 'sk'
        ? 'Táto správa neobsahuje laboratórne hodnoty. Jej text je uložený ako prepis a dá sa v ňom hľadať v Claude alebo ChatGPT.'
        : 'This report has no lab values. Its text is stored as a transcript and is searchable in Claude or ChatGPT.';
    } else {
      table.hidden = false;
      hint.textContent = lang === 'sk' ? 'Kliknite na hodnotu a v dokumente sa zvýrazní jej riadok.' : 'Click a value to highlight its line in the document.';
      vals.forEach(function (v) {
        var tr = document.createElement('tr');
        tr.setAttribute('data-action', 'pickLine|' + v.p.line);
        tr.setAttribute('tabindex', '0');
        tr.setAttribute('aria-selected', currentLine === v.p.line ? 'true' : 'false');
        tr.appendChild(el('td', null, seriesName(v.s)));
        var num = el('td', 'c-num');
        num.appendChild(el('b', null, numFmt(v.p.value_text))); num.appendChild(el('span', 'unit', v.s.unit));
        tr.appendChild(num);
        tr.appendChild(el('td', 'c-date', fmtDate(v.p.date)));
        tr.appendChild(el('td', 'num', String(v.p.line)));
        var st = el('td'); st.appendChild(stateNode(v.p.state)); tr.appendChild(st);
        tb.appendChild(tr);
      });
    }
    highlight();
  }

  function highlight() {
    document.querySelectorAll('#sheet .ln').forEach(function (r) {
      r.classList.toggle('on', currentLine !== null && r.getAttribute('data-line') === String(currentLine));
    });
    document.querySelectorAll('#value-rows tr').forEach(function (r) {
      r.setAttribute('aria-selected', r.getAttribute('data-action') === 'pickLine|' + currentLine ? 'true' : 'false');
    });
    var note = document.getElementById('state-note');
    var hit = valuesOf(currentDoc).filter(function (v) { return v.p.line === currentLine; })[0];
    if (!hit) { note.textContent = ''; return; }
    var key = hit.p.state === 'unverified' ? (hit.p.note || 'date_mismatch') : hit.p.state;
    note.textContent = t(STATE_NOTE[key] || STATE_NOTE.machine);
  }


  /* ── Chat examples built from the demo data ────────────────────── */
  function chip(docId, line, state, cls) {
    var d = docById(docId);
    var b = el('button', cls || 'chip');
    b.type = 'button';
    b.setAttribute('data-action', 'openSource|' + docId + '|' + line);
    if (cls === 'tchip') {
      b.textContent = '[' + srcText(1, line) + ']';
      return b;
    }
    var dot = el('i', 'cd cd-' + (state === 'machine' ? 'm' : state === 'human' ? 'h' : 'u'));
    b.appendChild(dot);
    b.appendChild(el('span', null, (lang === 'sk' ? 'Správa ' : 'Report ') + fmtDate(d.date) + ' · ' + srcText(1, line).replace(', ', ' · ') + ' · ' + t(STATE[state])));
    return b;
  }
  function numFmt(v) { return lang === 'sk' ? v : v.replace(/,/g, '.'); }
  function arrow(a, b) { return b > a ? ' ↑' : b < a ? ' ↓' : ' →'; }
  function labComparison() {
    var labs = data.documents.filter(function (d) { return d.category === 'labs'; });
    var latest = labs[0], prev = labs[1];
    var now = valuesOf(latest.id), before = prev ? valuesOf(prev.id) : [];
    var rows = now.map(function (v) {
      var old = before.filter(function (x) { return x.s.code === v.s.code && x.p.state !== 'unverified'; })[0];
      return { v: v, old: old };
    });
    var sampleNow = (now.filter(function (v) { return v.p.state !== 'unverified'; })[0] || now[0]).p.date;
    var samplePrev = before.length ? before[0].p.date : null;
    return { latest: latest, prev: prev, rows: rows, sampleNow: sampleNow, samplePrev: samplePrev };
  }
  function toolLine(name, tail) {
    var p = el('p', 'm-tool');
    p.appendChild(el('span', 'tdot'));
    p.appendChild(document.createTextNode('Oncofiles · ' + name + ' '));
    if (tail) p.appendChild(el('span', 'tool-ok', tail));
    return p;
  }
  function userLine(text, avatar) {
    var p = el('p', 'm-user');
    if (avatar) { p.appendChild(el('span', 'av', 'J')); }
    p.appendChild(el('span', null, text));
    return p;
  }

  function renderChatGpt(c) {
    var body = document.getElementById('chat-gpt-body');
    clear(body);
    body.appendChild(userLine(lang === 'sk'
      ? 'Aké boli moje posledné odbery a zmenilo sa niečo oproti minule?'
      : 'What did my latest blood tests show, and has anything changed since last time?'));
    body.appendChild(toolLine('fact_series', lang === 'sk' ? 'hotovo' : 'done'));
    var ai = el('div', 'm-ai');
    ai.appendChild(el('p', null, lang === 'sk'
      ? 'Posledný odber bol ' + fmtDate(c.sampleNow) + '. Oproti odberu z ' + fmtDate(c.samplePrev) + ':'
      : 'Your latest sample was taken on ' + fmtDate(c.sampleNow) + '. Compared with ' + fmtDate(c.samplePrev) + ':'));
    var ul = el('ul', 'ans');
    c.rows.forEach(function (r) {
      var li = el('li', r.v.p.state === 'unverified' ? 'unv' : null);
      var txt = el('span', 'ans-v');
      txt.appendChild(document.createTextNode(seriesName(r.v.s) + ' '));
      if (r.v.p.state === 'unverified') {
        txt.appendChild(el('b', null, numFmt(r.v.p.value_text) + ' ' + r.v.s.unit));
        txt.appendChild(document.createTextNode(lang === 'sk'
          ? ' je neoverené: v správe je pri ňom iný dátum odberu (' + fmtDate(r.v.p.date) + '), preto ho neporovnávam.'
          : ' is unverified: the report gives a different sample date for it (' + fmtDate(r.v.p.date) + '), so I am not comparing it.'));
      } else {
        txt.appendChild(el('b', null, numFmt(r.v.p.value_text) + ' ' + r.v.s.unit + (r.old ? arrow(r.old.p.value, r.v.p.value) : '')));
        if (r.old) txt.appendChild(document.createTextNode((lang === 'sk' ? ' (predtým ' : ' (previously ') + numFmt(r.old.p.value_text) + ')'));
      }
      li.appendChild(txt);
      li.appendChild(chip(c.latest.id, r.v.p.line, r.v.p.state));
      ul.appendChild(li);
    });
    ai.appendChild(ul);
    ai.appendChild(el('p', null, lang === 'sk'
      ? 'Porovnávam len overené hodnoty. Ak chcete, pripravím k nim otázky pre lekára.'
      : 'I only compare verified values. If you like, I can prepare questions for your doctor.'));
    body.appendChild(ai);
  }

  function renderClaude(c) {
    var body = document.getElementById('chat-claude-body');
    clear(body);
    body.appendChild(userLine(lang === 'sk' ? 'Priprav mi otázky na stredajšiu kontrolu u onkológa.' : 'Prepare questions for my oncology visit on Wednesday.', true));
    body.appendChild(toolLine('fact_series', '›'));
    body.appendChild(toolLine('document_detail', '›'));
    var ai = el('div', 'm-ai');
    ai.appendChild(el('p', null, lang === 'sk'
      ? 'Tu sú otázky, ktoré vychádzajú z vašich posledných výsledkov. Odpovede patria lekárovi.'
      : 'Here are questions based on your latest results. The answers belong to your doctor.'));
    var ol = el('ol', 'ans');
    var ordered = c.rows.filter(function (r) { return r.v.p.state === 'unverified'; })
      .concat(c.rows.filter(function (r) { return r.v.p.state !== 'unverified'; }));
    ordered.slice(0, 4).forEach(function (r) {
      var name = seriesName(r.v.s), val = numFmt(r.v.p.value_text) + ' ' + r.v.s.unit, q;
      if (r.v.p.state === 'unverified') {
        q = lang === 'sk'
          ? name + ' ' + val + ' je v správe z iného odberu (' + fmtDate(r.v.p.date) + '). Potrebujeme pred cyklom aktuálnu hodnotu?'
          : 'The ' + name + ' of ' + val + ' in the report is from a different sample (' + fmtDate(r.v.p.date) + '). Do we need a current value before the cycle?';
      } else if (r.old) {
        q = lang === 'sk'
          ? name + ' sa zmenil z ' + numFmt(r.old.p.value_text) + ' na ' + val + '. Je to pre ďalší postup podstatné?'
          : name + ' changed from ' + numFmt(r.old.p.value_text) + ' to ' + val + '. Does that matter for the next steps?';
      } else {
        q = lang === 'sk' ? name + ' je ' + val + '. Treba ho pred ďalším cyklom skontrolovať?' : name + ' is ' + val + '. Should it be checked before the next cycle?';
      }
      var li = el('li');
      li.appendChild(el('span', 'ans-v', q));
      li.appendChild(chip(c.latest.id, r.v.p.line, r.v.p.state));
      ol.appendChild(li);
    });
    ai.appendChild(ol);
    body.appendChild(ai);
  }

  function renderGemini(c) {
    var body = document.getElementById('chat-gemini-body');
    clear(body);
    var pr = el('p', 't-prompt');
    pr.appendChild(el('span', 't-gt', '>'));
    pr.appendChild(document.createTextNode(' ' + (lang === 'sk'
      ? 'Skontroluj, či pribudol nový laboratórny výsledok, a porovnaj ho s minulým.'
      : 'Check whether a new lab result has arrived and compare it with the previous one.')));
    body.appendChild(pr);
    function tool(name, lines) {
      var box = el('div', 't-tool');
      var h = el('p');
      h.appendChild(el('span', 't-ok', '✓'));
      h.appendChild(document.createTextNode(' ' + name + ' '));
      h.appendChild(el('span', 't-dim', '(oncofiles)'));
      box.appendChild(h);
      lines.forEach(function (l) { box.appendChild(el('p', 't-dim', l)); });
      body.appendChild(box);
    }
    tool('list_documents', ['limit: 5', (lang === 'sk' ? 'nový: ' : 'new: ') + loc(c.latest, 'filename')]);
    tool('document_detail', ['document_id: ' + c.latest.id]);
    tool('fact_series', ['codes: ' + c.rows.map(function (r) { return r.v.s.code; }).join(', ')]);
    body.appendChild(el('p', 't-out', lang === 'sk'
      ? 'Pribudol 1 nový výsledok: odber ' + fmtDate(c.sampleNow) + ' (správa z ' + fmtDate(c.latest.date) + '). Oproti odberu ' + fmtDate(c.samplePrev) + ':'
      : '1 new result: sample of ' + fmtDate(c.sampleNow) + ' (report of ' + fmtDate(c.latest.date) + '). Compared with the sample of ' + fmtDate(c.samplePrev) + ':'));
    var ul = el('ul', 't-diff');
    c.rows.forEach(function (r) {
      var li = el('li', r.v.p.state === 'unverified' ? 'unv' : null);
      li.appendChild(el('span', 't-k', r.v.s.code));
      var val = numFmt(r.v.p.value_text) + ' ' + r.v.s.unit;
      li.appendChild(el('span', 't-n', r.v.p.state === 'unverified'
        ? val + (lang === 'sk' ? ' neoverené, iný dátum odberu; neporovnávam' : ' unverified, different sample date; not compared')
        : (r.old ? numFmt(r.old.p.value_text) + ' → ' + val + arrow(r.old.p.value, r.v.p.value) : val)));
      li.appendChild(chip(c.latest.id, r.v.p.line, r.v.p.state, 'tchip'));
      ul.appendChild(li);
    });
    body.appendChild(ul);
    var idle = el('p', 't-prompt');
    idle.appendChild(el('span', 't-gt', '>'));
    idle.appendChild(document.createTextNode(' '));
    idle.appendChild(el('span', 't-cur'));
    body.appendChild(idle);
  }

  function renderChats() {
    var c = labComparison();
    renderChatGpt(c);
    renderClaude(c);
    renderGemini(c);
  }
  function showChat(name) {
    document.querySelectorAll('.chat-tabs [role="tab"]').forEach(function (tb) {
      var on = tb.getAttribute('aria-controls') === 'chat-' + name;
      tb.setAttribute('aria-selected', on ? 'true' : 'false');
      tb.setAttribute('tabindex', on ? '0' : '-1');
    });
    document.querySelectorAll('.chats [role="tabpanel"]').forEach(function (p) { p.hidden = p.id !== 'chat-' + name; });
  }
  function applyClientFlags() {
    document.querySelectorAll('.chats [data-client]').forEach(function (frame) {
      var tab = document.querySelector('.chat-tabs [aria-controls="' + frame.id + '"]');
      if (tab) tab.setAttribute('data-verified', frame.getAttribute('data-verified') === 'true' ? 'true' : 'false');
    });
  }

  /* ── Actions ───────────────────────────────────────────────────── */
  function openDoc(id, line) {
    currentDoc = parseInt(id, 10);
    var vals = valuesOf(currentDoc);
    currentLine = line ? parseInt(line, 10) : (vals.length ? vals[0].p.line : null);
    renderDocList();
    renderDoc();
  }
  function openSource(id, line) {
    openDoc(id, line);
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    document.getElementById('docview').scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  }
  function pickLine(n) { currentLine = parseInt(n, 10); highlight(); }
  function toggleLang() {
    lang = lang === 'sk' ? 'en' : 'sk';
    try { localStorage.setItem('oncofiles_lang', lang); } catch (e) { /* private mode */ }
    applyStatic();
    if (data) renderAll();
  }
  function renderAll() { renderOverview(); renderTrends(); renderDocList(); renderDoc(); renderChats(); }

  var ACTIONS = { openDoc: openDoc, openSource: openSource, pickLine: pickLine, toggleLang: toggleLang, showChat: showChat };
  function dispatch(spec) {
    if (!spec) return;
    var parts = String(spec).split('|');
    var fn = ACTIONS[parts[0]];
    if (typeof fn === 'function') fn.apply(null, parts.slice(1));
  }
  document.addEventListener('click', function (e) {
    var t2 = e.target.closest('[data-action]');
    if (t2) dispatch(t2.getAttribute('data-action'));
  });
  document.addEventListener('keydown', function (e) {
    var tab = e.target.closest('.chat-tabs [role="tab"]');
    if (tab && ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].indexOf(e.key) !== -1) {
      var tabs = [].slice.call(document.querySelectorAll('.chat-tabs [role="tab"]'));
      var i = tabs.indexOf(tab) + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1);
      var next = tabs[(i + tabs.length) % tabs.length];
      e.preventDefault();
      next.focus();
      dispatch(next.getAttribute('data-action'));
      return;
    }
    if (e.key !== 'Enter' && e.key !== ' ') return;
    var row = e.target.closest('tr[data-action]');
    if (row) { e.preventDefault(); dispatch(row.getAttribute('data-action')); }
  });

  applyStatic();
  applyClientFlags();
  fetch('/api/demo-data')
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(function (d) {
      data = d;
      var latestLab = d.documents.filter(function (x) { return x.category === 'labs'; })[0];
      currentDoc = latestLab ? latestLab.id : d.documents[0].id;
      var hgb = valuesOf(currentDoc).filter(function (v) { return v.s.code === 'HGB'; })[0];
      currentLine = hgb ? hgb.p.line : null;
      renderAll();
      document.getElementById('demo').setAttribute('aria-busy', 'false');
    })
    .catch(function () {
      var sub = document.getElementById('patient-sub');
      sub.textContent = lang === 'sk' ? 'Ukážkové údaje sa nepodarilo načítať. Obnovte stránku.' : 'The sample data did not load. Reload the page.';
    });
})();
