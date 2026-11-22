// Injected into /movement-atlas?record=1 by render.py (never shipped with the page).
// Builds the video chrome in the landing look (#849: Instarea navy, coral, turquoise,
// peach; Outfit + DM Sans, self-hosted fonts of the site): title block, footer,
// intro/outro cards, and the English text of the scene for the EN cut.
(() => {
  const CSS = `
  .stage{background:radial-gradient(120% 95% at 50% 28%,#14147c 0%,#00004a 62%,#000036 100%)!important;color:#fff}
  .lab{font-family:'DM Sans',sans-serif;font-size:19px;font-weight:600;padding:5px 13px;background:rgba(0,0,64,.82);border-color:rgba(255,255,255,.28);color:#fff;backdrop-filter:none}
  .lab.bad{border-color:#ff8a7a;color:#ffe1dc}
  .lab.ok{border-color:#33d6d6;color:#d9fbfb}
  #badge{bottom:152px;font-family:'Outfit',sans-serif;font-weight:700;font-size:24px;padding:6px 18px;background:rgba(0,0,64,.85)}
  #badge.peter{color:#ffb4a8;border-color:#ff8a7a}
  #badge.normal{color:#9df0f0;border-color:#33d6d6}
  #caption{bottom:110px;font-family:'DM Sans',sans-serif;font-size:23px;color:#e3e7f7}
  #rec-head{position:absolute;left:52px;top:46px;right:52px;z-index:5;pointer-events:none}
  #rec-head .k{font:600 15px/1 'DM Sans',sans-serif;letter-spacing:.14em;text-transform:uppercase;color:#33d6d6;margin:0 0 12px}
  #rec-head h2{font:700 50px/1.05 'Outfit',sans-serif;letter-spacing:-.02em;margin:0;color:#fff;max-width:900px}
  #rec-head p{font:400 22px/1.3 'DM Sans',sans-serif;color:#c9d1ee;margin:10px 0 0;max-width:860px}
  #rec-foot{position:absolute;left:52px;right:52px;bottom:40px;z-index:5;font:400 17px/1.3 'DM Sans',sans-serif;color:#9aa5cf;text-align:center}
  #rec-card{position:absolute;inset:0;z-index:20;background:#000059;overflow:hidden;opacity:0}
  #rec-card .c{position:absolute;border-radius:50%}
  #rec-card .body{position:absolute;left:96px;right:96px;top:50%;transform:translateY(-50%)}
  #rec-card .rule{width:72px;height:6px;background:#ff4646;margin:0 0 30px}
  #rec-card .k{font:600 19px/1 'DM Sans',sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#33d6d6;margin:0 0 22px}
  #rec-card h1{font:700 82px/1.02 'Outfit',sans-serif;letter-spacing:-.03em;margin:0;color:#fff}
  #rec-card h1 span{color:#ff4646}
  #rec-card p{font:300 32px/1.35 'Outfit',sans-serif;color:#c9d1ee;margin:26px 0 0}
  #rec-card p b{font-weight:700;color:#ffd0a8}
  #rec-card small{position:absolute;left:150px;right:96px;bottom:56px;max-width:760px;font:400 18px/1.4 'DM Sans',sans-serif;color:#9aa5cf}
  `;
  const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
  const stage = document.getElementById('stage');
  const head = document.createElement('div'); head.id = 'rec-head';
  head.innerHTML = '<p class="k"></p><h2></h2><p class="s"></p>';
  const foot = document.createElement('div'); foot.id = 'rec-foot';
  const card = document.createElement('div'); card.id = 'rec-card';
  stage.append(head, foot, card);

  const EN = {
    'Petrov nález': "Peter's finding", 'Zdravý stav': 'Healthy state',
    'Chrbát': 'Lower back', 'Pravé koleno': 'Right knee', 'Ľavé rameno': 'Left shoulder',
    'L5/S1: zaniknutý medzistavcový priestor': 'L5/S1: disc space collapsed',
    'Opotrebované malé kĺby (spondylartróza)': 'Worn facet joints (spondylarthrosis)',
    'Platnička normálnej výšky': 'Disc of normal height',
    'Bez posunu stavca (bez listézy)': 'No vertebral slip (no listhesis)',
    'Stavec L5': 'L5 vertebra', 'Krížová kosť': 'Sacrum',
    'Hlboké svaly chrbta (multifidus)': 'Deep back muscles (multifidus)',
    'Vzpriamovače chrbtice': 'Erector spinae',
    'Výpotok v kĺbe (stredne veľký)': 'Joint effusion (moderate)',
    'Chrupka jabĺčka mierne nerovná (I.–II. stupeň)': 'Kneecap cartilage slightly uneven (grade I–II)',
    'Miesto preskakovania': 'Where it clicks',
    'Tekutina pri šľache m. popliteus': 'Fluid around the popliteus tendon',
    'Menisky bez trhliny': 'Menisci intact',
    'Hladká chrupka, kĺb bez výpotku': 'Smooth cartilage, no effusion',
    'Štvorhlavý sval stehna': 'Quadriceps',
    'Vnútorná hlava (VMO) vedie jabĺčko': 'Inner head (VMO) guides the kneecap',
    'Väzy pevné, bez poškodenia': 'Ligaments stable and intact',
    'Poškodený horný až zadný lem jamky (labrum)': 'Lesion of the upper-to-rear socket rim (labrum)',
    'Cysta za zadným okrajom jamky': 'Cyst behind the rear edge of the socket',
    'Zóna, kde Peter udával bolesť': 'Zone where Peter reported pain',
    'Neporušený lem jamky': 'Intact socket rim',
    'Hlavica ramennej kosti (spriehľadnená)': 'Humeral head (shown transparent)',
    'Nadpleciok (akromion)': 'Acromion',
    'Šľachy rotátorovej manžety bez poškodenia': 'Rotator cuff tendons intact',
    'Šľacha dlhej hlavy bicepsu': 'Long head of biceps tendon',
    'Kľak na štyroch, chrbát rovný': 'On all fours, back flat',
    'Pravá ruka a ľavá noha v jednej línii s trupom': 'Right arm and left leg in line with the trunk',
    'Ruky nad plecami, kolená nad bedrami': 'Arms above the shoulders, knees above the hips',
    'Pravá ruka a ľavá noha klesajú, drieky ostávajú na podložke': 'Right arm and left leg lower, the lower back stays on the mat',
    'Stoj, chodidlá na šírku panvy': 'Standing, feet hip-width apart',
    'Podrep, kolená nad špičkami chodidiel': 'Half squat, knees over the toes',
    'Pravá noha vystretá na podložke': 'Right leg straight on the mat',
    'Výdrž s vystretým kolenom': 'Hold with the knee straight',
    'Lakeť pri tele, predlaktie pred bruchom': 'Elbow at the side, forearm across the belly',
    'Predlaktie vytočené von, lakeť ostáva pri tele': 'Forearm rotated outward, elbow stays at the side',
    'Ruky pri tele, palce dopredu': 'Arms by the sides, thumbs forward',
    'Ruky vo výške pliec, mierne pred telom': 'Arms at shoulder height, slightly in front of the body',
  };
  const PARTS = [
    [/^Ohyb kolena (\d+)°/, 'Knee flexion $1°'], [/ · tu Peter cíti preskočenie$/, ' · where Peter feels the click'],
    [/^Predklon/, 'Bending forward'], [/^Záklon/, 'Bending back'], [/^Stredná poloha/, 'Neutral'],
    [/ · L5\/S1 sa takmer nehýbe$/, ' · L5/S1 barely moves'], [/ · pohyb rozložený rovnomerne$/, ' · movement spread evenly'],
    [/^Upaženie (\d+)°/, 'Arm raised $1°'], [/ · nad horizontálou sa ozývala bolesť$/, ' · pain appeared above shoulder height'],
  ];
  const missing = new Set();
  function en(s) {
    if (!s) return s;
    if (EN[s]) return EN[s];
    let out = s; for (const [re, to] of PARTS) out = out.replace(re, to);
    if (out === s) missing.add(s);
    return out;
  }
  const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  window.__rec = {
    missing: () => [...missing],
    lang(l) { window.__atlas.setTr(l === 'en' ? en : null); },
    head(k, t, s) { head.querySelector('.k').textContent = k || ''; head.querySelector('h2').textContent = t || ''; head.querySelector('.s').textContent = s || ''; },
    foot(t) { foot.textContent = t || ''; },
    card(html, opacity) { if (html !== null) card.innerHTML = html; card.style.opacity = String(opacity); },
    headOpacity(o) { head.style.opacity = foot.style.opacity = String(o); },
    esc,
  };
})();
