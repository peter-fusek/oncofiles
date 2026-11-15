(function () {
'use strict';
const T = THREE;
const rad = d => d * Math.PI / 180;
const V = (x, y, z) => new T.Vector3(x, y, z);
const lerp = (a, b, t) => a + (b - a) * t;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const smooth = t => t * t * (3 - 2 * t);
const $ = s => document.querySelector(s);

/* ───────────── renderer, svetlá ───────────── */
const stageEl = $('#stage'), canvas = $('#gl');
const renderer = new T.WebGLRenderer({ canvas, antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputEncoding = T.sRGBEncoding;
renderer.toneMapping = T.ACESFilmicToneMapping;
renderer.toneMappingExposure = .8;
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = T.PCFSoftShadowMap;
const scene = new T.Scene();
const camera = new T.PerspectiveCamera(35, 1, 0.05, 800);

(function env() {
  const c = document.createElement('canvas'); c.width = 512; c.height = 256;
  const g = c.getContext('2d');
  const gr = g.createLinearGradient(0, 0, 0, 256);
  gr.addColorStop(0, '#e6eff3'); gr.addColorStop(.45, '#8198a3'); gr.addColorStop(.56, '#3b4b53'); gr.addColorStop(1, '#131b1f');
  g.fillStyle = gr; g.fillRect(0, 0, 512, 256);
  g.fillStyle = '#ffffff'; g.fillRect(50, 26, 100, 54); g.fillRect(330, 18, 130, 44);
  g.fillStyle = '#ffe3c6'; g.fillRect(215, 58, 46, 30);
  const tex = new T.CanvasTexture(c); tex.mapping = T.EquirectangularReflectionMapping; tex.encoding = T.sRGBEncoding;
  const pm = new T.PMREMGenerator(renderer);
  scene.environment = pm.fromEquirectangular(tex).texture; pm.dispose();
})();
const hemi = new T.HemisphereLight(0xe8f2f6, 0x26333a, .16); scene.add(hemi);
const key = new T.DirectionalLight(0xfff1e0, 1.15); key.castShadow = true;
key.shadow.mapSize.set(2048, 2048); key.shadow.bias = -0.0004; key.shadow.radius = 5;
scene.add(key, key.target);
const rim = new T.DirectionalLight(0x8fd4ff, .7); scene.add(rim, rim.target);
const ground = new T.Mesh(new T.CircleGeometry(1, 64), new T.ShadowMaterial({ opacity: .38 }));
ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; scene.add(ground);

/* ───────────── textúry a materiály ───────────── */
function canvasTex(w, h, draw, rx, ry) {
  const c = document.createElement('canvas'); c.width = w; c.height = h; draw(c.getContext('2d'), w, h);
  const t = new T.CanvasTexture(c); t.wrapS = t.wrapT = T.RepeatWrapping; t.repeat.set(rx || 1, ry || 1); return t;
}
const rnd = (() => { let s = 7; return () => (s = (s * 16807) % 2147483647) / 2147483647; })();
const boneBump = canvasTex(256, 256, (g, w, h) => {
  g.fillStyle = '#808080'; g.fillRect(0, 0, w, h);
  for (let i = 0; i < 2600; i++) { const v = 90 + rnd() * 80 | 0; g.fillStyle = `rgb(${v},${v},${v})`; g.fillRect(rnd() * w, rnd() * h, 1 + rnd() * 2, 1 + rnd() * 2); }
}, 3, 3);
const fiberMap = canvasTex(256, 256, (g, w, h) => {
  g.fillStyle = '#a23c38'; g.fillRect(0, 0, w, h);
  for (let i = 0; i < 420; i++) {
    const x = rnd() * w, l = rnd();
    g.strokeStyle = l > .5 ? `rgba(226,128,112,${.18 + rnd() * .3})` : `rgba(84,22,24,${.2 + rnd() * .35})`;
    g.lineWidth = .6 + rnd() * 1.8; g.beginPath(); g.moveTo(x, 0); g.bezierCurveTo(x + 6, h * .3, x - 6, h * .7, x + (rnd() - .5) * 8, h); g.stroke();
  }
}, 2, 1);
fiberMap.encoding = T.sRGBEncoding;
const P = o => new T.MeshPhysicalMaterial(Object.assign({ envMapIntensity: .55 }, o));
const M = {
  skin: P({ color: 0xaebbc3, roughness: .5, clearcoat: .25, clearcoatRoughness: .5 }),
  skinHL: P({ color: 0x18a57c, roughness: .45, clearcoat: .3, emissive: 0x0b4a38, emissiveIntensity: .5 }),
  bone: P({ color: 0xdccfb2, roughness: .62, bumpMap: boneBump, bumpScale: .035, clearcoat: .12, clearcoatRoughness: .6 }),
  cart: P({ color: 0xcfe6f2, roughness: .22, clearcoat: .7, clearcoatRoughness: .2 }),
  menisc: P({ color: 0xb9d2c8, roughness: .35, clearcoat: .5 }),
  disc: P({ color: 0x9cc6d9, roughness: .4, clearcoat: .35 }),
  tendon: P({ color: 0xf1ecde, roughness: .3, clearcoat: .6, clearcoatRoughness: .25 }),
  lig: P({ color: 0xd9d2bd, roughness: .4, clearcoat: .4 }),
  muscle: P({ color: 0xffffff, map: fiberMap, roughness: .48, clearcoat: .35, clearcoatRoughness: .4 }),
  fluid: P({ color: 0x58b4ff, roughness: .08, transparent: true, opacity: .5, emissive: 0x0b4f8a, emissiveIntensity: .5, depthWrite: false }),
  ghost: new T.MeshBasicMaterial({ color: 0x9fd0d8, transparent: true, opacity: .07, depthWrite: false }),
  mat: P({ color: 0x0f4347, roughness: .85 }),
};
const C = { cart: new T.Color(0xcfe6f2), worn: new T.Color(0xe7a55a), lab: new T.Color(0xb9d2c8), lesion: new T.Color(0xe5352b), disc: new T.Color(0x9cc6d9), discBad: new T.Color(0xc9793f), bone: new T.Color(0xece3cf), spur: new T.Color(0xe39a4c), red: new T.Color(0xff3b24), black: new T.Color(0x000000), act: new T.Color(0xff4a1c) };

const SPH = new T.SphereGeometry(1, 40, 28);
function mesh(geo, mat, parent, x, y, z, sx, sy, sz) {
  const m = new T.Mesh(geo, mat); m.position.set(x || 0, y || 0, z || 0);
  if (sx !== undefined) m.scale.set(sx, sy, sz);
  m.castShadow = true; m.receiveShadow = true; if (parent) parent.add(m); return m;
}
const cyl = (rt, rb, h, seg) => new T.CylinderGeometry(rt, rb, h, seg || 28);

/* spojnica medzi dvoma bodmi (šľacha, väz, sval) */
const _up = V(0, 1, 0), _d = V(0, 0, 0);
const CYL1 = new T.CylinderGeometry(1, 1, 1, 14, 1), BELLY = new T.SphereGeometry(1, 20, 14);
function linkMesh(mat, r, parent, opt) {
  opt = opt || {};
  const m = new T.Mesh(opt.belly ? BELLY : CYL1, mat); m.castShadow = true;
  m.userData = { r, flat: opt.flat || 1, belly: !!opt.belly }; parent.add(m); return m;
}
function setLink(m, a, b) {
  _d.subVectors(b, a); const len = Math.max(_d.length(), 1e-4);
  m.position.addVectors(a, b).multiplyScalar(.5);
  m.quaternion.setFromUnitVectors(_up, _d.multiplyScalar(1 / len));
  const u = m.userData; m.scale.set(u.r, u.belly ? len * .56 : len, u.r * u.flat);
}
function staticLink(mat, r, parent, a, b, opt) { const m = linkMesh(mat, r, parent, opt); setLink(m, a, b); return m; }
/* dynamická spojnica: konce sú v lokálnych súradniciach pohyblivých skupín */
function dyn(list, mat, r, layer, A, B, opt) {
  opt = opt || {};
  const o = { a: A, b: B, wrap: opt.wrap, m1: linkMesh(mat, r, layer, opt) };
  if (opt.wrap) o.m2 = linkMesh(mat, r, layer, opt);
  list.push(o); return o;
}
const _a = V(0, 0, 0), _b = V(0, 0, 0), _c = V(0, 0, 0), _m = V(0, 0, 0);
function updateDyn(list) {
  for (const o of list) {
    _a.copy(o.a[1]); o.a[0].localToWorld(_a); _b.copy(o.b[1]); o.b[0].localToWorld(_b);
    if (o.wrap) {
      _c.copy(o.wrap[1]); o.wrap[0].localToWorld(_c);
      _m.addVectors(_a, _b).multiplyScalar(.5).sub(_c);
      const R = o.wrap[2]; if (_m.length() < R) { if (_m.lengthSq() < 1e-6) _m.set(0, 1, 0); _m.setLength(R); }
      _m.add(_c); setLink(o.m1, _a, _m); setLink(o.m2, _m, _b);
    } else setLink(o.m1, _a, _b);
  }
}

/* ───────────── FIGURÍNA ───────────── */
const JN = ['root', 'spine', 'chest', 'neck', 'lSh', 'lEl', 'rSh', 'rEl', 'lHip', 'lKn', 'lAn', 'rHip', 'rKn', 'rAn'];
function buildFigure() {
  const G = new T.Group(), root = new T.Group(); G.add(root);
  const parts = {}, J = { root };
  const part = (name, parent, geo, x, y, z, sx, sy, sz) => { const m = mesh(geo, M.skin, parent, x, y, z, sx, sy, sz); parts[name] = m; return m; };
  const joint = (name, parent, x, y, z) => { const g = new T.Group(); g.position.set(x, y, z); parent.add(g); J[name] = g; return g; };
  part('pelvis', root, SPH, 0, 0, 0, .175, .125, .115);
  const spine = joint('spine', root, 0, .06, 0);
  part('abdomen', spine, SPH, 0, .13, 0, .15, .17, .105);
  const chest = joint('chest', spine, 0, .26, 0);
  part('chest', chest, SPH, 0, .16, 0, .19, .2, .118);
  const neck = joint('neck', chest, 0, .36, 0);
  part('neckc', neck, cyl(.045, .05, .1, 16), 0, .04, 0);
  part('head', neck, SPH, 0, .17, .01, .098, .118, .108);
  part('nose', neck, SPH, 0, .16, .115, .014, .02, .016);
  for (const [s, sx] of [['l', 1], ['r', -1]]) {
    const sh = joint(s + 'Sh', chest, sx * .215, .3, 0);
    part(s + 'ShCap', sh, SPH, 0, 0, 0, .062, .062, .062);
    part(s + 'UpperArm', sh, new T.CapsuleGeometry(.046, .22, 8, 18), 0, -.15, 0);
    const el = joint(s + 'El', sh, 0, -.3, 0);
    part(s + 'Forearm', el, new T.CapsuleGeometry(.038, .2, 8, 18), 0, -.13, 0);
    part(s + 'Hand', el, SPH, 0, -.3, 0, .04, .055, .03);
    const hip = joint(s + 'Hip', root, sx * .095, -.03, 0);
    part(s + 'Thigh', hip, new T.CapsuleGeometry(.07, .3, 8, 18), 0, -.21, 0);
    const kn = joint(s + 'Kn', hip, 0, -.43, 0);
    part(s + 'KneeCap', kn, SPH, 0, 0, .005, .06, .06, .06);
    part(s + 'Shin', kn, new T.CapsuleGeometry(.05, .3, 8, 18), 0, -.21, 0);
    const an = joint(s + 'An', kn, 0, -.42, 0);
    const fg = new T.CapsuleGeometry(.04, .13, 8, 14); fg.rotateX(Math.PI / 2);
    part(s + 'Foot', an, fg, 0, -.045, .06);
  }
  const matMesh = mesh(new T.BoxGeometry(.8, .016, 2.15), M.mat, G, 0, .008, 0); matMesh.castShadow = false;
  // značky problémových oblastí
  const hot = {};
  const hotDefs = { spine: [J.root, V(0, .07, -.085)], knee: [J.rKn, V(0, 0, .02)], shoulder: [J.lSh, V(0, 0, 0)] };
  for (const k in hotDefs) {
    const dot = new T.Mesh(new T.SphereGeometry(.028, 20, 14), new T.MeshBasicMaterial({ color: 0xff7d60, depthTest: false, transparent: true }));
    const ring = new T.Mesh(new T.RingGeometry(.05, .058, 48), new T.MeshBasicMaterial({ color: 0xff7d60, depthTest: false, transparent: true, side: T.DoubleSide }));
    dot.renderOrder = ring.renderOrder = 20; G.add(dot, ring); hot[k] = { dot, ring, src: hotDefs[k] };
  }
  return { G, root, parts, J, matMesh, hot };
}
const fig = buildFigure();
const STAND = { lSh: [0, 0, 8], rSh: [0, 0, -8], lEl: -8, rEl: -8 };
const pv = (p, k) => { const v = p[k]; return v === undefined ? [0, 0, 0] : (Array.isArray(v) ? v : [v, 0, 0]); };
function applyPose(A, B, t) {
  for (const k of JN) { const a = pv(A, k), b = pv(B, k); fig.J[k].rotation.set(rad(lerp(a[0], b[0], t)), rad(lerp(a[1], b[1], t)), rad(lerp(a[2], b[2], t))); }
}
const _box = new T.Box3(), _w = V(0, 0, 0);
function groundFigure(anchor, target) {
  fig.root.position.set(0, 0, 0); fig.G.updateMatrixWorld(true);
  _box.makeEmpty(); for (const k in fig.parts) _box.expandByObject(fig.parts[k], true);
  fig.root.position.y = -_box.min.y;
  if (anchor) { fig.J[anchor].getWorldPosition(_w); fig.root.position.x = target.x - _w.x; fig.root.position.z = target.z - _w.z; }
  fig.G.updateMatrixWorld(true);
}

/* ───────────── KOLENO (pravé; +X = vnútorná strana, +Z = dopredu) ───────────── */
function buildKnee() {
  const S = new T.Group(), L3 = new T.Group(); S.add(L3);
  const fem = new T.Group(), tib = new T.Group(), pat = new T.Group(); S.add(fem, tib, pat);
  const dl = [];
  // stehnová kosť
  mesh(cyl(1.45, 1.75, 13), M.bone, fem, 0, 8, 0);
  mesh(SPH, M.bone, fem, 0, 2.0, -.1, 3.0, 2.3, 2.2);
  for (const sx of [1, -1]) mesh(SPH, M.cart, fem, sx * 1.75, 0, -.2, 2.15, 2.15, 2.6);
  mesh(new T.CapsuleGeometry(5.6, 12, 8, 24), M.ghost, fem, 0, 9, -.3).castShadow = false;
  // píšťala + ihlica
  mesh(cyl(3.3, 2.5, 2.3, 40), M.bone, tib, 0, -3.6, -.1, 1, 1, .8);
  mesh(cyl(1.95, 1.25, 12), M.bone, tib, 0, -10.6, 0);
  mesh(SPH, M.bone, tib, 0, -5.4, 1.75, .95, 1.4, .75);
  for (const sx of [1, -1]) {
    mesh(cyl(1.5, 1.5, .16, 28), M.cart, tib, sx * 1.7, -2.4, -.1, 1, 1, 1.15);
    const g = new T.TorusGeometry(1.42, .34, 12, 36, Math.PI * 1.5); g.rotateZ(rad(sx > 0 ? -135 : 45)); g.rotateX(Math.PI / 2);
    mesh(g, M.menisc, tib, sx * 1.7, -2.2, -.1, 1, .62, 1.15);
  }
  mesh(SPH, M.bone, tib, -3.05, -4.7, -1.4, .85, .95, .85);
  mesh(cyl(.5, .45, 11, 14), M.bone, tib, -3.05, -10.4, -1.4);
  mesh(new T.CapsuleGeometry(4.3, 11, 8, 24), M.ghost, tib, -.4, -10, -.6).castShadow = false;
  // jabĺčko
  const patBone = mesh(SPH, M.bone, pat, 0, .9, 3.05, 1.5, 1.65, .7);
  const patCartMat = M.cart.clone();
  mesh(SPH, patCartMat, pat, 0, .9, 2.62, 1.3, 1.45, .42);
  // nález: výpotok, tekutina v hiatus popliteus, miesto preskakovania
  const eff = [mesh(SPH, M.fluid, fem, 0, 3.6, 2.15, 2.5, 2.2, .95), mesh(SPH, M.fluid, fem, 2.75, 1.3, 1.2, .8, 1.5, 1.0), mesh(SPH, M.fluid, fem, -2.75, 1.3, 1.2, .8, 1.5, 1.0)];
  const hiat = mesh(SPH, M.fluid, tib, -2.35, -2.5, -2.25, .95, 1.15, .75);
  eff.concat([hiat]).forEach(m => { m.castShadow = false; m.userData.s = m.scale.clone(); m.renderOrder = 5; });
  const clickMat = new T.MeshBasicMaterial({ color: 0xffd23c, transparent: true, opacity: 0, depthTest: false });
  const click = new T.Mesh(new T.SphereGeometry(.55, 20, 14), clickMat); click.position.set(1.95, .5, 2.75); click.renderOrder = 15; pat.add(click);
  // úroveň 3: svaly, šľachy, väzy
  const act = M.muscle.clone(); act.emissive = C.act.clone(); act.emissiveIntensity = 0;
  mesh(SPH, act, L3, 0, 11.2, 2.3, 2.5, 5.2, 1.35);
  mesh(SPH, act, L3, 2.0, 5.4, 2.0, 1.25, 2.4, .95).rotation.z = rad(-22);
  mesh(SPH, M.muscle, L3, -2.2, 6.6, 1.9, 1.2, 3.0, .95).rotation.z = rad(14);
  dyn(dl, M.tendon, .8, L3, [fem, V(0, 6.6, 2.5)], [pat, V(0, 2.2, 3.1)], { flat: .4 });
  dyn(dl, M.tendon, .42, L3, [fem, V(1.7, 3.6, 2.5)], [pat, V(1.2, 1.3, 3.1)], { flat: .5 });
  dyn(dl, M.tendon, .78, L3, [pat, V(0, -.5, 3.15)], [tib, V(0, -5.0, 2.45)], { flat: .4 });
  dyn(dl, M.lig, .5, L3, [fem, V(2.95, .7, -.2)], [tib, V(2.75, -6.2, .2)], { flat: .3 });
  dyn(dl, M.lig, .3, L3, [fem, V(-2.95, .6, -.5)], [tib, V(-3.05, -4.2, -1.4)]);
  dyn(dl, M.lig, .3, L3, [fem, V(-.55, .3, -1.1)], [tib, V(.3, -2.45, 1.0)]);
  dyn(dl, M.lig, .32, L3, [fem, V(.6, -.1, .4)], [tib, V(0, -2.5, -1.7)]);
  dyn(dl, M.tendon, .3, L3, [fem, V(-2.7, -.2, -.7)], [tib, V(-1.3, -3.3, -2.15)]);
  dyn(dl, act, .75, L3, [tib, V(-1.3, -3.3, -2.15)], [tib, V(1.4, -7.6, -1.6)], { belly: true, flat: .6 });
  dyn(dl, act, 1.25, L3, [fem, V(-2.3, 13.5, -2.6)], [tib, V(-3.0, -4.3, -1.5)], { belly: true, flat: .7 });
  dyn(dl, act, 1.25, L3, [fem, V(2.2, 13.5, -2.6)], [tib, V(2.5, -4.6, -1.3)], { belly: true, flat: .7 });
  const st = { flex: 0 };
  function update(tm, m, s) {
    const u = .5 - .5 * Math.cos(tm * Math.PI * 2); st.flex = 4 + 88 * u;
    tib.rotation.x = rad(st.flex); pat.rotation.x = rad(st.flex * .62);
    eff.forEach(e => e.scale.copy(e.userData.s).multiplyScalar(Math.max(m, .001)));
    hiat.scale.copy(hiat.userData.s).multiplyScalar(Math.max(m, .001));
    patCartMat.color.copy(C.cart).lerp(C.worn, m * .75); patCartMat.roughness = lerp(.22, .7, m);
    const inZone = st.flex > 34 && st.flex < 58; const k = inZone ? Math.sin((st.flex - 34) / 24 * Math.PI) : 0;
    clickMat.opacity = m * (.25 + .75 * k) * .9; click.scale.setScalar(.7 + k * .9);
    pat.position.x = m * k * .16 * Math.sin(s.time * 38);
    act.emissiveIntensity = (1 - m) * (.16 + .14 * Math.sin(tm * Math.PI * 2 - 1));
    S.updateMatrixWorld(true); updateDyn(dl);
  }
  const labels = [
    { t: 'Výpotok v kĺbe (stredne veľký)', k: 'bad', o: fem, p: V(0, 4.6, 2.9), show: s => s.m > .5 },
    { t: 'Chrupka jabĺčka mierne nerovná (I.–II. stupeň)', k: 'bad', o: pat, p: V(-1.2, .9, 2.9), show: s => s.m > .5 && s.level === 2 },
    { t: 'Miesto preskakovania', k: 'bad', o: pat, p: V(1.95, .5, 2.75), show: s => s.m > .5 },
    { t: 'Tekutina pri šľache m. popliteus', k: 'bad', o: tib, p: V(-2.5, -2.5, -2.6), show: s => s.m > .5 },
    { t: 'Menisky bez trhliny', k: 'ok', o: tib, p: V(2.9, -2.2, .6), show: s => s.level === 2 },
    { t: 'Hladká chrupka, kĺb bez výpotku', k: 'ok', o: fem, p: V(0, 3.2, 2.4), show: s => s.m < .5 },
    { t: 'Štvorhlavý sval stehna', k: 'info', o: fem, p: V(-1.6, 11.5, 3.2), show: s => s.level === 3 },
    { t: 'Vnútorná hlava (VMO) vedie jabĺčko', k: 'info', o: fem, p: V(2.6, 5.4, 2.6), show: s => s.level === 3 },
    { t: 'Väzy pevné, bez poškodenia', k: 'ok', o: tib, p: V(2.9, -4.5, .3), show: s => s.level === 3 },
  ];
  return { G: S, L3, update, labels, st, ground: -17.2, size: 20, center: V(0, -1, 0), cam: { az: .95, el: .1, dist: 52, t: V(0, -.6, 0) } };
}

/* ───────────── CHRBTICA L3–S1 (+Z = dopredu) ───────────── */
function buildSpine() {
  const S = new T.Group(), L3 = new T.Group(); S.add(L3);
  const base = new T.Group(); base.rotation.x = rad(34); S.add(base);
  mesh(cyl(2.55, 1.0, 6.4, 28), M.bone, base, 0, -3.2, -.2, 1, 1, .72);
  mesh(SPH, M.bone, base, 0, -1.6, -.9, 4.4, 1.9, 1.5);
  for (const sx of [1, -1]) { mesh(SPH, M.ghost, base, sx * 6.4, -1.5, -1.2, 4.2, 5.4, 1.4).castShadow = false; mesh(SPH, M.cart, base, sx * 1.75, .9, -3.1, .6, .6, .6); }
  const spurMat = M.bone.clone(); spurMat.color.copy(C.spur);
  const facetMat = M.cart.clone(); facetMat.emissive = C.red.clone(); facetMat.emissiveIntensity = 0;
  const facetHot = M.cart.clone(); facetHot.emissive = C.red.clone(); facetHot.emissiveIntensity = 0;
  const spurs = [];
  function vertebra(hotFacet) {
    const g = new T.Group();
    mesh(cyl(2.3, 2.3, 2.7, 32), M.bone, g, 0, 0, 0, 1, 1, .8);
    mesh(new T.TorusGeometry(2.28, .14, 8, 36), M.bone, g, 0, 1.3, 0, 1, .8, 1).rotation.x = Math.PI / 2;
    mesh(new T.TorusGeometry(2.28, .14, 8, 36), M.bone, g, 0, -1.3, 0, 1, .8, 1).rotation.x = Math.PI / 2;
    for (const sx of [1, -1]) {
      mesh(new T.BoxGeometry(.75, .95, 1.7), M.bone, g, sx * 1.5, .45, -2.5);
      const tp = mesh(cyl(.38, .3, 3.1, 12), M.bone, g, sx * 3.3, .4, -2.9); tp.rotation.z = rad(sx * 82);
      mesh(SPH, M.cart, g, sx * 1.75, 1.55, -3.2, .58, .62, .58);
      mesh(SPH, hotFacet ? facetHot : facetMat, g, sx * 1.5, -1.75, -3.45, .56, .62, .56);
      const sp = mesh(SPH, spurMat, g, sx * 2.05, -1.55, -3.75, .34, .34, .34); spurs.push(sp);
      const sp2 = mesh(SPH, spurMat, g, sx * 2.2, 1.7, -3.4, .3, .3, .3); spurs.push(sp2);
    }
    mesh(new T.BoxGeometry(3.7, 1.5, .6), M.bone, g, 0, .1, -3.5);
    const spn = mesh(new T.BoxGeometry(.55, 1.7, 2.5), M.bone, g, 0, -.35, -4.9); spn.rotation.x = rad(-12);
    return g;
  }
  const angles = [-15, -12, -9], joints = [], discs = [], verts = [];
  const discMats = [M.disc.clone(), M.disc, M.disc];
  let parent = base, y0 = 0;
  for (let i = 0; i < 3; i++) {
    const j = new T.Group(); j.position.y = y0; parent.add(j);
    const d = mesh(cyl(2.36, 2.36, 1, 32), discMats[i], j, 0, .55, 0, 1, 1.1, .8);
    const v = vertebra(i === 0); j.add(v);
    joints.push(j); discs.push(d); verts.push(v); parent = j;
  }
  // predné výrastky na okraji L5/S1
  const lip = [mesh(SPH, spurMat, verts[0], 0, -1.35, 1.95, .7, .25, .35), mesh(SPH, spurMat, base, 0, -.05, 2.0, .7, .25, .35)];
  lip.forEach(l => spurs.push(l));
  spurs.forEach(s => s.userData.s = s.scale.clone());
  // úroveň 3: svaly
  const act = M.muscle.clone(); act.emissive = C.act.clone(); act.emissiveIntensity = 0;
  const dl = [];
  for (const sx of [1, -1]) {
    dyn(dl, M.muscle, 1.15, L3, [verts[2], V(sx * 2.9, 2.2, -4.4)], [base, V(sx * 2.7, -3.5, -2.6)], { belly: true, flat: .75 });
    dyn(dl, act, .5, L3, [verts[2], V(sx * .35, -1.0, -5.6)], [verts[0], V(sx * 1.9, .9, -3.7)], { belly: true });
    dyn(dl, act, .5, L3, [verts[1], V(sx * .35, -1.0, -5.6)], [base, V(sx * 1.9, .2, -3.3)], { belly: true });
    dyn(dl, act, .45, L3, [verts[0], V(sx * .35, -1.0, -5.6)], [base, V(sx * 1.6, -2.4, -2.5)], { belly: true });
    dyn(dl, M.muscle, .95, L3, [verts[2], V(sx * 2.7, .3, .2)], [base, V(sx * 5.2, -7.5, 3.6)], { belly: true, flat: .8 });
  }
  const st = { s: 0 };
  function update(tm, m) {
    const s = Math.sin(tm * Math.PI * 2); st.s = s;
    const h = [lerp(1.1, .16, m), 1.1, 1.1];
    const amp = [lerp(5, .8, m), lerp(5, 6.6, m), lerp(5, 6.6, m)];
    let y = 0;
    for (let i = 0; i < 3; i++) {
      joints[i].position.y = i === 0 ? 0 : y;
      joints[i].rotation.x = rad(angles[i] + amp[i] * s + (i === 0 ? m * 5 : 0));
      discs[i].scale.y = h[i]; discs[i].position.y = h[i] / 2; verts[i].position.y = h[i] + 1.35; y = h[i] + 2.7;
    }
    discMats[0].color.copy(C.disc).lerp(C.discBad, m);
    spurs.forEach(sp => sp.scale.copy(sp.userData.s).multiplyScalar(Math.max(m, .001)));
    const ext = Math.max(0, -s);
    facetHot.emissiveIntensity = m * (.25 + ext * 1.1); facetMat.emissiveIntensity = m * ext * .35;
    act.emissiveIntensity = (1 - m) * (.12 + .2 * Math.abs(s));
    S.updateMatrixWorld(true); updateDyn(dl);
  }
  const labels = [
    { t: 'L5/S1: zaniknutý medzistavcový priestor', k: 'bad', o: joints[0], p: V(2.5, .1, .3), show: s => s.m > .5 },
    { t: 'Opotrebované malé kĺby (spondylartróza)', k: 'bad', o: verts[1], p: V(2.1, -1.6, -3.7), show: s => s.m > .5 },
    { t: 'Platnička normálnej výšky', k: 'ok', o: joints[0], p: V(2.5, .55, .3), show: s => s.m < .5 },
    { t: 'Bez posunu stavca (bez listézy)', k: 'ok', o: verts[0], p: V(0, 0, 2.0), show: () => true },
    { t: 'Stavec L5', k: 'info', o: verts[0], p: V(-2.4, 0, .3), show: s => s.level === 2 },
    { t: 'Krížová kosť', k: 'info', o: base, p: V(-2.3, -3, .5), show: s => s.level === 2 },
    { t: 'Hlboké svaly chrbta (multifidus)', k: 'info', o: verts[1], p: V(.5, -.4, -5.9), show: s => s.level === 3 },
    { t: 'Vzpriamovače chrbtice', k: 'info', o: verts[2], p: V(-3.4, 1, -4.6), show: s => s.level === 3 },
  ];
  return { G: S, L3, update, labels, st, ground: -9, size: 15, center: V(0, 5, -1), cam: { az: 2.02, el: .1, dist: 42, t: V(0, 4.6, -1.2) } };
}

/* ───────────── RAMENO (ľavé; +X = von, +Z = dopredu) ───────────── */
function buildShoulder() {
  const S = new T.Group(), L3 = new T.Group(); S.add(L3);
  const scap = new T.Group(), hum = new T.Group(); S.add(scap, hum);
  const blade = new T.Group(); blade.rotation.y = rad(-25); blade.position.z = -1.3; scap.add(blade);
  const sh = new T.Shape(); sh.moveTo(-1.7, 3.3); sh.lineTo(-10.6, 3.2); sh.lineTo(-9.3, -10.6); sh.lineTo(-2.1, -3.7); sh.closePath();
  mesh(new T.ExtrudeGeometry(sh, { depth: .4, bevelEnabled: true, bevelSize: .35, bevelThickness: .2, bevelSegments: 4 }), M.bone, blade, 0, 0, -.2);
  staticLink(M.bone, .6, blade, V(-10.2, 2.4, -.6), V(-.6, 3.6, -.9));
  const gg = cyl(1.8, 1.9, .7, 36); gg.rotateZ(Math.PI / 2); mesh(gg, M.bone, scap, -2.45, 0, 0, 1, 1, .78);
  mesh(SPH, M.bone, scap, -3.1, 0, -.4, 1.2, 1.5, 1.3);
  const gc = cyl(1.6, 1.6, .12, 36); gc.rotateZ(Math.PI / 2); mesh(gc, M.cart, scap, -2.05, 0, 0, 1, 1, .78);
  mesh(SPH, M.bone, scap, .5, 3.95, -.5, 2.5, .42, 1.7);
  staticLink(M.bone, .48, scap, V(-2.6, 2.1, .2), V(-.9, 2.0, 2.7));
  staticLink(M.bone, .55, scap, V(-.2, 4.15, .9), V(-8.5, 3.6, 3.4));
  // labrum: zdravá časť + poškodený úsek (zadný až horný)
  const lg = new T.Group(); lg.position.x = -1.8; lg.rotation.y = Math.PI / 2; lg.scale.set(.78, 1, 1); scap.add(lg);
  const lesMat = M.menisc.clone(); lesMat.emissive = C.red.clone(); lesMat.emissiveIntensity = 0;
  const lesion = mesh(new T.TorusGeometry(1.82, .38, 12, 28, rad(105)), lesMat, lg); lesion.rotation.z = rad(-5);
  const okArc = mesh(new T.TorusGeometry(1.82, .3, 12, 48, rad(255)), M.menisc, lg); okArc.rotation.z = rad(100);
  const cyst = mesh(SPH, M.fluid, scap, -3.0, .75, -2.05, .95, .85, .8); cyst.castShadow = false; cyst.renderOrder = 5; cyst.userData.s = cyst.scale.clone();
  const painMat = new T.MeshBasicMaterial({ color: 0xff3b24, transparent: true, opacity: 0, depthWrite: false });
  const pain = mesh(SPH, painMat, scap, .3, 2.9, -.3, 2.3, .8, 1.7); pain.castShadow = false; pain.renderOrder = 6;
  // ramenná kosť
  const headMat = M.cart.clone(); headMat.transparent = true; headMat.opacity = .42; headMat.depthWrite = false;
  mesh(SPH, headMat, hum, .5, 0, 0, 2.25, 2.25, 2.25).renderOrder = 7;
  mesh(SPH, M.bone, hum, 1.75, .35, .1, 1.45, 1.6, 1.5);
  mesh(SPH, M.bone, hum, 1.0, -.3, 1.55, .85, .95, .8);
  mesh(SPH, M.bone, hum, 1.35, -2.1, 0, 1.45, 1.8, 1.35);
  mesh(cyl(1.1, .95, 13.5), M.bone, hum, 1.4, -9.2, 0);
  mesh(new T.CapsuleGeometry(3.5, 10, 8, 24), M.ghost, hum, 1.4, -9, 0).castShadow = false;
  // úroveň 3: rotátorová manžeta, biceps, deltový sval
  const act = M.muscle.clone(); act.emissive = C.act.clone(); act.emissiveIntensity = 0;
  const dl = [], HC = [scap, V(.5, 0, 0), 2.5];
  const bellies = [
    [V(-9.4, 2.9, -4.6), V(-2.4, 3.05, -1.3), 1.0, V(2.5, 1.5, .1)],   // supraspinatus
    [V(-9.0, -3.4, -5.4), V(-2.6, -.3, -2.6), 1.25, V(2.4, .6, -1.5)],  // infraspinatus
    [V(-8.2, -2.4, -2.0), V(-2.5, -.3, 1.7), 1.15, V(1.1, -.2, 2.3)],   // subscapularis
  ];
  const scapL3 = new T.Group(), humL3 = new T.Group(); scap.add(scapL3); hum.add(humL3);
  for (const b of bellies) {
    staticLink(act, b[2], scapL3, b[0], b[1], { belly: true, flat: .55 });
    dyn(dl, M.tendon, .42, L3, [scap, b[1]], [hum, b[3]], { wrap: HC, flat: .5 });
  }
  dyn(dl, M.tendon, .24, L3, [scap, V(-1.8, 1.85, .1)], [hum, V(1.7, -3.0, 1.75)], { wrap: [scap, V(.5, 0, 0), 2.6] });
  staticLink(M.muscle, .95, humL3, V(1.7, -3.0, 1.75), V(1.6, -13.5, 1.9), { belly: true, flat: .8 });
  const delt = M.muscle.clone(); delt.transparent = true; delt.opacity = .2; delt.depthWrite = false;
  const dm = mesh(SPH, delt, humL3, 1.6, -2.6, 0, 3.4, 4.9, 3.3); dm.castShadow = false; dm.renderOrder = 4;
  const st = { abd: 0 };
  function update(tm, m, s) {
    const u = .5 - .5 * Math.cos(tm * Math.PI * 2); st.abd = 5 + 140 * u;
    hum.rotation.z = rad(st.abd); scap.rotation.z = rad(Math.max(0, st.abd - 30) / 3);
    lesMat.color.copy(C.lab).lerp(C.lesion, m); lesMat.emissiveIntensity = m * (.35 + .25 * Math.sin(s.time * 5)); lesMat.roughness = lerp(.35, .85, m);
    lesion.scale.setScalar(lerp(1, 1.07, m));
    cyst.scale.copy(cyst.userData.s).multiplyScalar(Math.max(m, .001));
    const over = clamp((st.abd - 85) / 30, 0, 1);
    painMat.opacity = m * over * (.3 + .15 * Math.sin(s.time * 6));
    act.emissiveIntensity = (1 - m) * (.12 + .22 * u);
    const show3 = L3.visible; scapL3.visible = show3; humL3.visible = show3;
    S.updateMatrixWorld(true); updateDyn(dl);
  }
  const labels = [
    { t: 'Poškodený horný až zadný lem jamky (labrum)', k: 'bad', o: scap, p: V(-1.8, 1.6, -1.1), show: s => s.m > .5 },
    { t: 'Cysta za zadným okrajom jamky', k: 'bad', o: scap, p: V(-3.2, .7, -2.6), show: s => s.m > .5 },
    { t: 'Zóna, kde Peter udával bolesť', k: 'bad', o: scap, p: V(.6, 3.1, .6), show: s => s.m > .5 && st.abd > 90 },
    { t: 'Neporušený lem jamky', k: 'ok', o: scap, p: V(-1.8, 1.6, -1.1), show: s => s.m < .5 },
    { t: 'Hlavica ramennej kosti (spriehľadnená)', k: 'info', o: hum, p: V(2.2, -1.2, -1.2), show: s => s.level === 2 },
    { t: 'Nadpleciok (akromion)', k: 'info', o: scap, p: V(1.4, 4.3, 0), show: s => s.level === 2 && st.abd < 80 },
    { t: 'Šľachy rotátorovej manžety bez poškodenia', k: 'ok', o: scap, p: V(-1.2, 3.2, -1.0), show: s => s.level === 3 },
    { t: 'Šľacha dlhej hlavy bicepsu', k: 'info', o: hum, p: V(1.7, -4.5, 1.9), show: s => s.level === 3 },
  ];
  return { G: S, L3, update, labels, st, ground: -17.5, size: 20, center: V(-2, -2, 0), cam: { az: 2.05, el: .3, dist: 46, t: V(-1.6, -1.6, -.6) } };
}

const stages = { figure: { G: fig.G, ground: 0, size: 1.5, center: V(0, .9, 0) }, knee: buildKnee(), spine: buildSpine(), shoulder: buildShoulder() };
for (const k in stages) { scene.add(stages[k].G); stages[k].G.visible = false; }

/* ───────────── OBSAH ───────────── */
const SUP = { root: [-90, 0, 0], neck: [8, 0, 0] };
const QUAD = { root: [90, 0, 0], lHip: -90, rHip: -90, lKn: 90, rKn: 90, lAn: 55, rAn: 55, lSh: -90, rSh: -90, neck: -22 };
const mix = (...o) => Object.assign({}, ...o);
const EX = {
  knee: [
    { name: 'Dvíhanie vystretej nohy v ľahu', goal: 'Štvorhlavý sval stehna bez zaťaženia kĺbu', hl: ['rThigh'], mat: 'z', cam: { az: 1.25, el: .28, dist: 4.3, y: .28 },
      A: mix(SUP, { lHip: -45, lKn: 115, lAn: 20, lSh: [0, 0, 10], rSh: [0, 0, -10] }), B: mix(SUP, { lHip: -45, lKn: 115, lAn: 20, rHip: -42, lSh: [0, 0, 10], rSh: [0, 0, -10] }),
      caps: ['Pravá noha vystretá na podložke', 'Výdrž s vystretým kolenom'],
      steps: ['Ľahnite si na chrbát. Ľavú nohu pokrčte a chodidlo oprite o podložku.', 'Pravú nohu vystrite, špičku pritiahnite k sebe a napnite stehno, akoby ste koleno tlačili do podložky.', 'S napnutým stehnom pomaly zdvihnite pravú nohu do výšky ľavého kolena.', 'Podržte 3 sekundy, koleno ostáva celý čas vystreté.', 'Pomaly položte a stehno uvoľnite až na podložke.'],
      dose: '2–3 série po 10 opakovaní', care: 'Ak sa pri zdvihu prehýbate v drieku, zdvíhajte nižšie.' },
    { name: 'Mostík', goal: 'Sedacie svaly a zadná strana stehna', hl: ['pelvis', 'lThigh', 'rThigh'], mat: 'z', anchor: 'lAn', cam: { az: 1.25, el: .25, dist: 4.3, y: .3 },
      A: mix(SUP, { lHip: -45, rHip: -45, lKn: 115, rKn: 115, lAn: 20, rAn: 20, lSh: [0, 0, 12], rSh: [0, 0, -12] }), B: { root: [-112, 0, 0], neck: [26, 0, 0], lKn: 112, rKn: 112, lSh: [22, 0, 12], rSh: [22, 0, -12] },
      caps: ['Ľah na chrbte, chodidlá na šírku panvy', 'Panva hore, kolená a plecia v jednej línii'],
      steps: ['Ľahnite si na chrbát, kolená pokrčte, chodidlá položte na šírku panvy.', 'Stiahnite sedacie svaly a jemne pritiahnite brucho.', 'Zdvihnite panvu, až kým kolená, bedrá a plecia netvoria jednu rovnú líniu.', 'Podržte 3–5 sekúnd. Kolená smerujú rovno, nepadajú dnu ani von.', 'Pomaly sa vráťte, stavec po stavci.'],
      dose: '2–3 série po 10–12 opakovaní', care: 'Neprehýbajte sa v drieku. Ťah máte cítiť v zadku, nie v krížoch.' },
    { name: 'Mini-drep s kontrolou osi kolena', goal: 'Stehná a sedacie svaly, vedenie kolena nad chodidlom', hl: ['lThigh', 'rThigh', 'pelvis'], anchor: 'lAn', cam: { az: .75, el: .1, dist: 4.8, y: .9 },
      A: STAND, B: { root: [22, 0, 0], lHip: -62, rHip: -62, lKn: 58, rKn: 58, lAn: -18, rAn: -18, lSh: [-95, 0, 4], rSh: [-95, 0, -4], neck: -12 },
      caps: ['Stoj, chodidlá na šírku panvy', 'Podrep, kolená nad špičkami chodidiel'],
      steps: ['Postavte sa, chodidlá na šírku panvy, špičky mierne von.', 'Zadok posúvajte dozadu, akoby ste si sadali na vysokú stoličku.', 'Choďte len do polovičného podrepu. Kolená smerujú nad druhý prst na nohe.', 'Váha ostáva na celom chodidle, päty neopúšťajú zem.', 'Pomaly sa vystrite a hore stiahnite sedacie svaly.'],
      dose: '2–3 série po 10 opakovaní', care: 'Hĺbku zvyšujte až po dohode s fyzioterapeutom, v kolene je výpotok.' },
    { name: 'Unožovanie v ľahu na boku', goal: 'Bočné sedacie svaly, ktoré držia os nohy', hl: ['pelvis', 'rThigh'], mat: 'x', cam: { az: 0, el: .3, dist: 4.3, y: .3 },
      A: { root: [0, 0, -90], lHip: -35, lKn: 55, lSh: [-80, 0, 0], lEl: -30, neck: [0, 0, 12] }, B: { root: [0, 0, -90], lHip: -35, lKn: 55, rHip: [0, 0, -36], lSh: [-80, 0, 0], lEl: -30, neck: [0, 0, 12] },
      caps: ['Ľah na ľavom boku, pravá noha vystretá', 'Pravá noha zdvihnutá, špička smeruje dopredu'],
      steps: ['Ľahnite si na ľavý bok. Spodnú nohu pokrčte, aby ste boli stabilný.', 'Vrchnú, pravú nohu vystrite v predĺžení trupu.', 'Zdvihnite ju asi 30–40 cm. Špička smeruje dopredu, nie k stropu.', 'Panva sa nepreklápa dozadu. Podržte 2 sekundy.', 'Pomaly spustite.'],
      dose: '2–3 série po 12 opakovaní', care: 'Nohu neveďte pred telo, pracovať má bok zadku.' },
    { name: 'Stoj na jednej nohe', goal: 'Rovnováha a súhra svalov okolo kolena', hl: ['rThigh', 'rShin', 'pelvis'], anchor: 'rAn', cam: { az: .5, el: .08, dist: 4.8, y: .95 },
      A: mix(STAND, { lSh: [0, 0, 18], rSh: [0, 0, -18] }), B: { lHip: -55, lKn: 72, lSh: [0, 0, 26], rSh: [0, 0, -26], lEl: -8, rEl: -8, rKn: 8, rHip: -4, rAn: -4 },
      caps: ['Stoj na oboch nohách', 'Váha na pravej nohe, koleno mierne povolené'],
      steps: ['Postavte sa k stene alebo stoličke, aby ste sa mohli pridržať.', 'Preneste váhu na pravú nohu, koleno nechajte jemne povolené.', 'Ľavú nohu zdvihnite a držte rovnováhu 20–30 sekúnd.', 'Pravé koleno smeruje rovno nad chodidlo a panva ostáva vodorovne.', 'Vystriedajte nohy. Neskôr skúste bez pridŕžania.'],
      dose: '3–4 opakovania po 20–30 sekúnd na každú nohu', care: 'Pri neistote sa pridržte, cieľom je pokojné koleno.' },
  ],
  spine: [
    { name: 'Mačka a krava', goal: 'Jemné rozhýbanie driekovej chrbtice', hl: ['abdomen', 'chest', 'pelvis'], mat: 'z', cam: { az: 1.3, el: .2, dist: 4.8, y: .38 },
      A: mix(QUAD, { spine: -10, chest: -10, lSh: -70, rSh: -70, neck: -35 }), B: mix(QUAD, { spine: 16, chest: 18, lSh: -124, rSh: -124, neck: 30, lHip: -84, rHip: -84 }),
      caps: ['Krava: hrudník klesá, pohľad dopredu', 'Mačka: chrbát do oblúka, brada k hrudi'],
      steps: ['Kľaknite si na štyri. Dlane pod plecia, kolená pod bedrá.', 'S nádychom nechajte hrudník a brucho klesnúť a pozrite sa dopredu.', 'S výdychom vytlačte chrbát do oblúka a podsaďte panvu.', 'Pohyb veďte pomaly, stavec po stavci, bez bolesti.', 'Striedajte obe polohy plynulo.'],
      dose: '10–12 pomalých opakovaní', care: 'V záklone nechoďte do krajnej polohy, drieková oblasť má opotrebované malé kĺby.' },
    { name: 'Vták a pes (bird-dog)', goal: 'Hlboké svaly chrbta a trupu, stabilita L5/S1', hl: ['abdomen', 'pelvis', 'lThigh', 'rUpperArm'], mat: 'z', cam: { az: 1.3, el: .2, dist: 4.3, y: .42 },
      A: QUAD, B: mix(QUAD, { rSh: -178, lHip: 0, lKn: 0, lAn: 30, neck: -14 }),
      caps: ['Kľak na štyroch, chrbát rovný', 'Pravá ruka a ľavá noha v jednej línii s trupom'],
      steps: ['Kľaknite si na štyri, chrbát držte rovný ako stôl.', 'Jemne pritiahnite brucho, drieky sa neprehýbajú.', 'Vystrite naraz pravú ruku dopredu a ľavú nohu dozadu.', 'Panva sa nenakláňa ani neotáča. Podržte 5 sekúnd.', 'Vráťte sa a vymeňte strany.'],
      dose: '2–3 série po 6–8 opakovaní na každú stranu', care: 'Nohu dvíhajte len do výšky panvy, nie vyššie.' },
    { name: 'Mŕtvy chrobák (dead bug)', goal: 'Brušný korzet pri pohybe končatín', hl: ['abdomen', 'pelvis'], mat: 'z', cam: { az: 1.25, el: .28, dist: 4.3, y: .35 },
      A: mix(SUP, { lSh: -90, rSh: -90, lHip: -90, rHip: -90, lKn: 90, rKn: 90 }), B: mix(SUP, { lSh: -90, rSh: -172, lHip: -14, rHip: -90, lKn: 8, rKn: 90 }),
      caps: ['Ruky nad plecami, kolená nad bedrami', 'Pravá ruka a ľavá noha klesajú, drieky ostávajú na podložke'],
      steps: ['Ľahnite si na chrbát. Ruky vystrite nad plecia, kolená dajte nad bedrá.', 'Drieky jemne pritlačte k podložke a pritiahnite brucho.', 'Pomaly spúšťajte pravú ruku za hlavu a ľavú nohu vystierajte nad podložku.', 'Choďte len tak ďaleko, kým drieky ostávajú na podložke.', 'Vráťte sa a vymeňte strany.'],
      dose: '2–3 série po 6–8 opakovaní na každú stranu', care: 'Ak sa drieky odlepia, skráťte rozsah.' },
    { name: 'Podpor na predlaktiach (plank)', goal: 'Výdrž celého trupu', hl: ['abdomen', 'chest', 'pelvis'], mat: 'z', cam: { az: 1.3, el: .18, dist: 4.3, y: .3 },
      A: { root: [82, 0, 0], lSh: -82, rSh: -82, lEl: -90, rEl: -90, neck: -8 }, B: { root: [81, 0, 0], spine: 2, lSh: -81, rSh: -81, lEl: -90, rEl: -90, neck: -8 },
      caps: ['Telo v jednej línii od hlavy po päty', 'Výdrž, pokojné dýchanie'],
      steps: ['Oprite sa o predlaktia, lakte pod plecami.', 'Vystrite nohy a oprite sa o špičky.', 'Stiahnite brucho a zadok. Telo tvorí rovnú líniu.', 'Dýchajte pokojne a držte 15–30 sekúnd.', 'Položte kolená a oddýchnite si.'],
      dose: '3 výdrže po 15–30 sekúnd', care: 'Keď začne klesať panva, výdrž ukončite. Ľahšia verzia je na kolenách.' },
  ],
  shoulder: [
    { name: 'Sťahovanie lopatiek s gumou', goal: 'Svaly medzi lopatkami, držanie pleca', hl: ['chest', 'lUpperArm', 'rUpperArm'], cam: { az: 2.5, el: .12, dist: 5.2, y: 1.05 },
      A: { lSh: [-80, 0, 4], rSh: [-80, 0, -4], lEl: -12, rEl: -12 }, B: { chest: -5, lSh: [28, 0, 14], rSh: [28, 0, -14], lEl: -108, rEl: -108 },
      caps: ['Ruky vystreté pred telom, guma napnutá', 'Lakte pri tele, lopatky stiahnuté k sebe'],
      steps: ['Gumu upevnite vo výške hrudníka a chyťte ju oboma rukami.', 'Postavte sa vzpriamene, plecia držte dole, nie pri ušiach.', 'Ťahajte lakte popri tele dozadu a stiahnite lopatky k sebe.', 'Podržte 2 sekundy.', 'Pomaly povoľte späť.'],
      dose: '2–3 série po 12 opakovaní', care: 'Nezakláňajte sa a nedvíhajte plecia.' },
    { name: 'Vonkajšia rotácia s gumou', goal: 'Rotátorová manžeta, centrovanie hlavice v jamke', hl: ['lUpperArm', 'lForearm', 'lShCap'], cam: { az: .25, el: .25, dist: 4.4, y: 1.0 },
      A: mix(STAND, { lSh: [0, -42, 5], lEl: -90 }), B: mix(STAND, { lSh: [0, 42, 5], lEl: -90 }),
      caps: ['Lakeť pri tele, predlaktie pred bruchom', 'Predlaktie vytočené von, lakeť ostáva pri tele'],
      steps: ['Gumu upevnite vo výške lakťa po pravej strane tela.', 'Ľavý lakeť pokrčte do pravého uhla a pritlačte k boku. Pomôže zrolovaný uterák pod lakťom.', 'Vytáčajte predlaktie von, lakeť sa od tela neodlepí.', 'Choďte len do bezbolestného rozsahu a podržte 2 sekundy.', 'Pomaly sa vráťte.'],
      dose: '2–3 série po 12 opakovaní s ľahkou gumou', care: 'Pri bolesti vzadu v ramene rozsah skráťte, lem jamky je poškodený vzadu a hore.' },
    { name: 'Upažovanie v rovine lopatky', goal: 'Súhra lopatky a ramena po výšku pliec', hl: ['lUpperArm', 'rUpperArm', 'lShCap', 'rShCap'], cam: { az: .45, el: .12, dist: 4.8, y: 1.0 },
      A: STAND, B: { lSh: [0, -30, 84], rSh: [0, 30, -84] },
      caps: ['Ruky pri tele, palce dopredu', 'Ruky vo výške pliec, mierne pred telom'],
      steps: ['Postavte sa vzpriamene, ruky pri tele, palce smerujú dopredu.', 'Dvíhajte vystreté ruky šikmo do strán, asi 30° pred telom.', 'Zastavte vo výške pliec. Nad horizontálu zatiaľ nechoďte.', 'Plecia držte ďaleko od uší. Podržte 2 sekundy.', 'Pomaly spustite za 3 sekundy.'],
      dose: '2–3 série po 10 opakovaní, bez závažia alebo do 1 kg', care: 'Bolesť nad horizontálou je signál zastaviť nižšie.' },
  ],
};
const REG = {
  spine: { name: 'Chrbát', sub: 'Drieková chrbtica, prechod L5/S1',
    find: ['RTG neukázal posun stavca (bez listézy).', 'Priestor medzi posledným driekovým stavcom a krížovou kosťou (L5/S1) je zaniknutý. Platnička tam stratila výšku.', 'Malé kĺby driekovej chrbtice sú difúzne opotrebované (spondylartróza).', 'Peter udáva pocit vychýlenia stavca v spodnej časti chrbta.'],
    peter: ['L5 sedí takmer priamo na krížovej kosti.', 'Segment L5/S1 sa takmer nehýbe, pohyb preberajú vyššie segmenty.', 'V záklone sa viac zaťažujú malé kĺby (červená).'],
    normal: ['Platnička má plnú výšku a pruží.', 'Pohyb sa delí rovnomerne medzi segmenty.', 'Hlboké svaly chrbta držia segment stabilne (oranžová žiara).'],
    legend: [['#ece3cf', 'kosť'], ['#9cc6d9', 'platnička'], ['#c9793f', 'znížená platnička'], ['#e39a4c', 'kostné výrastky'], ['#a23c38', 'svaly']],
    motion: 'Model ukazuje predklon a záklon.' },
  knee: { name: 'Pravé koleno', sub: 'Preskakovanie na vnútornej strane vedľa jabĺčka',
    warn: 'Správa z 11. 11. 2026 cituje MR ako léziu menisku a chondropatiu IV. stupňa. Popis rádiológa uvádza menisky bez trhliny a chondropatiu I.–II. stupňa. Model vychádza z popisu rádiológa. Pred záťažou to treba overiť u ortopéda.',
    find: ['Preskakovanie vedľa jabĺčka na vnútornej strane, bez bolesti, napríklad pri výpadoch.', 'MR: stredne veľký výpotok v kĺbe.', 'MR: mierne nerovná chrupka jabĺčka a žliabku stehnovej kosti (I.–II. stupeň).', 'MR: tekutina okolo neporušenej šľachy podkolenného svalu.', 'Menisky a väzy sú bez trhliny, chondromatóza sa nepotvrdila.', 'Hybnosť je plná a nebolestivá, citlivá je vonkajšia kĺbová štrbina.'],
    peter: ['V kĺbe je viac tekutiny (modrá).', 'Chrupka jabĺčka je mierne zdrsnená (oranžová).', 'Okolo 35–55° ohybu jabĺčko krátko preskočí (žltý bod).'],
    normal: ['Kĺb je bez výpotku.', 'Jabĺčko kĺže v žliabku hladko a stredom.', 'Svaly okolo kolena vedú pohyb (oranžová žiara).'],
    legend: [['#ece3cf', 'kosť'], ['#cfe6f2', 'chrupka'], ['#b9d2c8', 'menisky'], ['#58b4ff', 'tekutina'], ['#f1ecde', 'šľachy a väzy'], ['#a23c38', 'svaly']],
    motion: 'Model ukazuje ohyb kolena od vystretia po 90°.' },
  shoulder: { name: 'Ľavé rameno', sub: 'Bolesť pri zdvihnutí ruky nad horizontálu (2025)',
    warn: 'Pre rameno ortopéd rehabilitáciu nepredpísal, odporučil tejpovať pri záťaži. Posledný záznam je z apríla 2025. Cviky sú návrh na posúdenie fyzioterapeutom.',
    find: ['Február 2025: tri mesiace bolesť bez úrazu, hlavne nad horizontálou. Test na impingement bol pozitívny.', 'MR: poškodený horný až zadný lem kĺbovej jamky (labrum).', 'MR: cysta za zadným okrajom jamky, najskôr vychádza z poškodeného lemu.', 'Šľachy rotátorovej manžety sú bez poškodenia, kĺb bez zápalu.', 'Po obstreku do kĺbu sa stav zlepšil.'],
    peter: ['Lem jamky je vzadu a hore poškodený (červená).', 'Za jamkou je cysta (modrá).', 'Nad horizontálou sa ozýva priestor pod nadpleckom (červená zóna).'],
    normal: ['Lem jamky je celistvý a prehlbuje jamku.', 'Lopatka sa otáča spolu s rukou v pomere asi 1 : 2.', 'Rotátorová manžeta drží hlavicu v strede jamky (oranžová žiara).'],
    legend: [['#ece3cf', 'kosť'], ['#cfe6f2', 'chrupka'], ['#b9d2c8', 'lem jamky'], ['#e5352b', 'poškodený lem'], ['#58b4ff', 'cysta'], ['#a23c38', 'svaly']],
    motion: 'Model ukazuje upaženie od tela až nad hlavu.' },
};

/* ───────────── STAV, KAMERA ───────────── */
const reduce = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
const S = { region: 'knee', level: 1, mode: 'peter', ex: null, playing: !reduce, speed: 1, tm: 0, m: 1, mT: 1, time: 0, autoRot: false, cmpT: 0 };
const cam = { az: .45, el: .08, dist: 5.6, t: V(0, .95, 0) }, goal = { az: .45, el: .08, dist: 5.6, t: V(0, .95, 0) };
const FIGCAM = { spine: { az: 2.55, el: .1 }, knee: { az: .5, el: .06 }, shoulder: { az: .55, el: .1 } };
let active = null, home = null;
function setGoal(c, snap) {
  goal.az = c.az; goal.el = c.el; goal.dist = c.dist; goal.t.copy(c.t);
  home = { az: c.az, el: c.el, dist: c.dist, t: c.t.clone() };
  if (snap) { cam.az = c.az; cam.el = c.el; cam.dist = c.dist * 1.5; cam.t.copy(c.t); }
}
function lightFor(st) {
  const s = st.size, c = st.center;
  key.position.set(c.x + s * 1.1, c.y + s * 2.4, c.z + s * 1.7); key.target.position.copy(c);
  const sc = key.shadow.camera; sc.left = -s * 1.4; sc.right = s * 1.4; sc.top = s * 1.6; sc.bottom = -s * 1.6; sc.near = s * .2; sc.far = s * 8; sc.updateProjectionMatrix();
  rim.position.set(c.x - s * 2, c.y + s * 1.2, c.z - s * 2.2); rim.target.position.copy(c);
  ground.position.set(c.x, st.ground, c.z); ground.scale.setScalar(s * 3);
}
function stageKey() { return (S.ex || S.level === 1) ? 'figure' : S.region; }
function activate(snap) {
  const k = stageKey(); active = stages[k];
  for (const n in stages) stages[n].G.visible = n === k;
  lightFor(active);
  if (k === 'figure') {
    for (const n in fig.parts) fig.parts[n].material = M.skin;
    for (const n in fig.hot) { fig.hot[n].dot.visible = fig.hot[n].ring.visible = !S.ex; }
    if (S.ex) {
      S.ex.hl.forEach(n => fig.parts[n] && (fig.parts[n].material = M.skinHL));
      fig.matMesh.visible = !!S.ex.mat; fig.matMesh.rotation.y = S.ex.mat === 'x' ? Math.PI / 2 : 0;
      applyPose(S.ex.A, S.ex.A, 0); groundFigure();
      if (S.ex.anchor) { fig.J[S.ex.anchor].getWorldPosition(_w); S.ex._at = _w.clone(); }
      const c = S.ex.cam; setGoal({ az: c.az, el: c.el, dist: c.dist, t: V(0, c.y, 0) }, snap);
    } else {
      fig.matMesh.visible = false; applyPose(STAND, STAND, 0); groundFigure();
      const f = FIGCAM[S.region]; setGoal({ az: f.az, el: f.el, dist: 4.9, t: V(0, .98, 0) }, snap);
    }
  } else {
    active.L3.visible = S.level === 3;
    const c = active.cam; setGoal({ az: c.az, el: c.el, dist: c.dist * (S.level === 3 ? .92 : 1), t: c.t }, snap);
  }
  rebuildLabels();
}
let fadeTimer = 0;
function transition(fn) {
  clearTimeout(fadeTimer);
  const prev = stageKey();
  const zoomIn = prev === 'figure' && !S.ex;
  if (zoomIn) { const h = fig.hot[S.region]; _w.copy(h.src[1]); h.src[0].localToWorld(_w); goal.t.copy(_w); goal.dist = 1.6; }
  stageEl.classList.add('fading');
  fadeTimer = setTimeout(() => { fn(); activate(true); syncUI(); stageEl.classList.remove('fading'); }, zoomIn ? 330 : 220);
}

/* ───────────── štítky ───────────── */
const labelsEl = $('#labels'), leaders = $('#leaders');
let labs = [];
function rebuildLabels() {
  labelsEl.innerHTML = ''; leaders.innerHTML = ''; labs = [];
  let defs;
  if (stageKey() === 'figure') {
    if (S.ex) defs = [];
    else defs = Object.keys(fig.hot).map(k => ({ t: REG[k].name, k: 'hot', o: fig.hot[k].src[0], p: fig.hot[k].src[1], show: () => true, region: k }));
  } else defs = active.labels;
  for (const d of defs) {
    const el = document.createElement(d.region ? 'button' : 'div');
    el.className = 'lab ' + d.k + (d.region && d.region === S.region ? ' sel' : ''); el.textContent = d.t;
    if (d.region) el.addEventListener('click', () => { transition(() => { S.region = d.region; S.level = 2; }); });
    labelsEl.appendChild(el);
    const ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    ln.setAttribute('stroke', d.k === 'bad' || d.k === 'hot' ? '#ff8b70' : d.k === 'ok' ? '#62d9a4' : 'rgba(255,255,255,.55)'); ln.setAttribute('stroke-width', '1.2');
    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle'); dot.setAttribute('r', '3'); dot.setAttribute('fill', ln.getAttribute('stroke'));
    leaders.appendChild(ln); leaders.appendChild(dot);
    labs.push({ d, el, ln, dot, w: 0 });
  }
}
const _p = V(0, 0, 0);
function layoutLabels(W, H) {
  const vis = [];
  for (const L of labs) {
    let on = L.d.show(S) && !(W < 560 && L.d.k === 'info');
    if (on) { _p.copy(L.d.p); L.d.o.localToWorld(_p); _p.project(camera); on = _p.z < 1 && Math.abs(_p.x) < 1.05 && Math.abs(_p.y) < 1.05; L.x = (_p.x * .5 + .5) * W; L.y = (-_p.y * .5 + .5) * H; }
    L.el.style.display = on ? '' : 'none'; L.ln.style.display = L.dot.style.display = on ? '' : 'none';
    if (on) { if (!L.w) L.w = L.el.offsetWidth; vis.push(L); }
  }
  const narrow = W < 560;
  const left = narrow ? vis.slice().sort((a, b) => a.y - b.y) : vis.filter(L => L.x < W * .5).sort((a, b) => a.y - b.y), right = narrow ? [] : vis.filter(L => L.x >= W * .5).sort((a, b) => a.y - b.y);
  const top = W < 560 ? 104 : 64, bot = H - 128;
  for (const [arr, side] of [[left, -1], [right, 1]]) {
    let prev = top - 30;
    for (const L of arr) {
      let lx = narrow ? 8 : side > 0 ? L.x + 34 : L.x - 34 - L.w;
      lx = clamp(lx, 8, W - L.w - 8);
      let ly = Math.max(L.y - 12, prev + 30); ly = Math.min(ly, bot); prev = ly;
      L.el.style.transform = `translate(${lx | 0}px,${ly | 0}px)`;
      const ex = side > 0 ? lx : Math.min(lx + L.w, L.x), ey = ly + 12;
      L.ln.setAttribute('x1', L.x); L.ln.setAttribute('y1', L.y); L.ln.setAttribute('x2', ex); L.ln.setAttribute('y2', ey);
      L.dot.setAttribute('cx', L.x); L.dot.setAttribute('cy', L.y);
    }
  }
}

/* ───────────── slučka ───────────── */
const badge = $('#badge'), caption = $('#caption'), scrub = $('#scrub');
let last = performance.now(), W = 0, H = 0, scrubbing = false;
function resize() {
  const r = stageEl.getBoundingClientRect(); W = r.width; H = r.height;
  renderer.setSize(W, H, false); camera.aspect = W / H; camera.fov = W / H < .8 ? 46 : 35; camera.updateProjectionMatrix();
  leaders.setAttribute('viewBox', `0 0 ${W} ${H}`); labs.forEach(L => L.w = 0);
}
new ResizeObserver(resize).observe(stageEl);
function exPhase(tm) { // zdvih 35 %, výdrž 15 %, návrat 35 %, pauza 15 %
  if (tm < .35) return smooth(tm / .35); if (tm < .5) return 1; if (tm < .85) return 1 - smooth((tm - .5) / .35); return 0;
}
function frame(now) {
  const dt = Math.min(.05, (now - last) / 1000); last = now; S.time += dt;
  if (S.playing && !scrubbing) { S.tm = (S.tm + dt * S.speed / (S.ex ? 5.5 : 6)) % 1; scrub.value = Math.round(S.tm * 1000); }
  if (S.mode === 'compare') { S.cmpT += dt; S.mT = (S.cmpT % 6) < 3 ? 1 : 0; } else S.mT = S.mode === 'peter' ? 1 : 0;
  S.m += (S.mT - S.m) * (1 - Math.exp(-dt * 5));
  const k = stageKey();
  if (k === 'figure') {
    if (S.ex) {
      const u = exPhase(S.tm); applyPose(S.ex.A, S.ex.B, u); groundFigure(S.ex.anchor, S.ex._at);
      caption.textContent = u < .5 ? S.ex.caps[0] : S.ex.caps[1];
    } else {
      for (const n in fig.hot) {
        const h = fig.hot[n]; _w.copy(h.src[1]); h.src[0].localToWorld(_w);
        h.dot.position.copy(_w); h.ring.position.copy(_w); h.ring.quaternion.copy(camera.quaternion);
        const ph = (S.time * .9 + (n === 'knee' ? .3 : n === 'spine' ? .6 : 0)) % 1, sel = n === S.region;
        h.ring.scale.setScalar((sel ? 1.2 : .9) + ph * 1.6); h.ring.material.opacity = (1 - ph) * (sel ? 1 : .6); h.dot.scale.setScalar(sel ? 1.35 : 1);
      }
      caption.textContent = 'Tri oblasti z ortopedických správ. Kliknite na štítok alebo zvoľte úroveň 2.';
    }
  } else {
    active.update(S.tm, S.m, S);
    const pm = S.m > .5;
    if (k === 'knee') { const f = active.st.flex; caption.textContent = `Ohyb kolena ${Math.round(f)}°` + (pm && f > 34 && f < 58 ? ' · tu Peter cíti preskočenie' : ''); }
    else if (k === 'spine') { const s = active.st.s; caption.textContent = (s > .15 ? 'Predklon' : s < -.15 ? 'Záklon' : 'Stredná poloha') + (pm ? ' · L5/S1 sa takmer nehýbe' : ' · pohyb rozložený rovnomerne'); }
    else { const a = active.st.abd; caption.textContent = `Upaženie ${Math.round(a)}°` + (pm && a > 90 ? ' · nad horizontálou sa ozývala bolesť' : ''); }
  }
  const showBadge = k !== 'figure';
  badge.style.display = showBadge ? '' : 'none';
  if (showBadge) { const pm = S.mT > .5; badge.className = pm ? 'peter' : 'normal'; badge.textContent = pm ? 'Petrov nález' : 'Zdravý stav'; }
  if (S.autoRot) goal.az += dt * .35;
  const f = 1 - Math.exp(-dt * 6);
  cam.az += (goal.az - cam.az) * f; cam.el += (goal.el - cam.el) * f; cam.dist += (goal.dist - cam.dist) * f; cam.t.lerp(goal.t, f);
  const ce = Math.cos(cam.el);
  camera.position.set(cam.t.x + cam.dist * ce * Math.sin(cam.az), cam.t.y + cam.dist * Math.sin(cam.el), cam.t.z + cam.dist * ce * Math.cos(cam.az));
  camera.lookAt(cam.t); camera.updateMatrixWorld();
  renderer.render(scene, camera);
  layoutLabels(W, H);
  requestAnimationFrame(frame);
}

/* ───────────── ovládanie ───────────── */
const ptrs = new Map(); let pinch = 0;
canvas.addEventListener('pointerdown', e => { canvas.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, [e.clientX, e.clientY]); S.autoRot = false; syncUI(); });
canvas.addEventListener('pointermove', e => {
  const p = ptrs.get(e.pointerId); if (!p) return;
  if (ptrs.size === 1) { goal.az -= (e.clientX - p[0]) * .008; goal.el = clamp(goal.el + (e.clientY - p[1]) * .006, -1.2, 1.35); }
  p[0] = e.clientX; p[1] = e.clientY;
  if (ptrs.size === 2) { const a = [...ptrs.values()], d = Math.hypot(a[0][0] - a[1][0], a[0][1] - a[1][1]); if (pinch) zoom(pinch / d); pinch = d; }
});
const up = e => { ptrs.delete(e.pointerId); pinch = 0; };
canvas.addEventListener('pointerup', up); canvas.addEventListener('pointercancel', up);
function zoom(f) { if (!home) return; goal.dist = clamp(goal.dist * f, home.dist * .22, home.dist * 2.2); }
canvas.addEventListener('wheel', e => { e.preventDefault(); zoom(Math.exp(e.deltaY * .0012)); }, { passive: false });
$('#zin').onclick = () => zoom(.78); $('#zout').onclick = () => zoom(1.28);
$('#rotl').onclick = () => goal.az -= .5; $('#rotr').onclick = () => goal.az += .5;
$('#reset').onclick = () => { if (home) { goal.az = home.az; goal.el = home.el; goal.dist = home.dist; goal.t.copy(home.t); cam.az = ((cam.az - home.az + Math.PI) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI) - Math.PI + home.az; } S.autoRot = false; syncUI(); };
$('#autorot').onclick = () => { S.autoRot = !S.autoRot; syncUI(); };
$('#play').onclick = () => { S.playing = !S.playing; syncUI(); };
$('#speed').onchange = e => S.speed = +e.target.value;
scrub.addEventListener('input', () => { scrubbing = true; S.tm = scrub.value / 1000; });
scrub.addEventListener('change', () => scrubbing = false);
scrub.addEventListener('pointerup', () => scrubbing = false);
document.querySelectorAll('.tabs button').forEach(b => b.onclick = () => { const r = b.dataset.region; if (r === S.region && !S.ex) return; transition(() => { S.region = r; S.ex = null; }); renderPanel(r); });
document.querySelectorAll('.seg.level button').forEach(b => b.onclick = () => { const l = +b.dataset.level; if (l === S.level) return; const sameStage = l > 1 && S.level > 1; if (sameStage) { S.level = l; activate(false); syncUI(); } else transition(() => S.level = l); });
document.querySelectorAll('.seg.mode button').forEach(b => b.onclick = () => { S.mode = b.dataset.mode; S.cmpT = 0; if (S.level === 1 && !S.ex) transition(() => S.level = 2); else syncUI(); });
$('#exback').onclick = () => { transition(() => { S.ex = null; if (S.level === 1) S.level = 2; }); document.querySelectorAll('.ex.on').forEach(e => e.classList.remove('on')); };
const ICON = { play: '<svg viewBox="0 0 24 24"><path d="M7 4.5v15l13-7.5z"/></svg>', pause: '<svg viewBox="0 0 24 24"><rect x="6" y="4.5" width="4.2" height="15" rx="1"/><rect x="13.8" y="4.5" width="4.2" height="15" rx="1"/></svg>' };
function syncUI() {
  document.querySelectorAll('.tabs button').forEach(b => b.setAttribute('aria-selected', b.dataset.region === S.region));
  document.querySelectorAll('.seg.level button').forEach(b => b.setAttribute('aria-pressed', +b.dataset.level === S.level && !S.ex));
  document.querySelectorAll('.seg.mode button').forEach(b => b.setAttribute('aria-pressed', b.dataset.mode === S.mode));
  $('#play').innerHTML = S.playing ? ICON.pause : ICON.play;
  $('#autorot').setAttribute('aria-pressed', S.autoRot);
  stageEl.classList.toggle('exmode', !!S.ex);
  if (S.ex) $('#exname').textContent = 'Cvičenie: ' + S.ex.name;
}

/* ───────────── panel ───────────── */
const panel = $('#panel');
const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
function renderPanel(r) {
  const R = REG[r], li = a => a.map(x => `<li>${esc(x)}</li>`).join('');
  panel.innerHTML = `
    <h2>${R.name}</h2><p class="sub">${R.sub}</p>
    ${R.warn ? `<p class="callout">${esc(R.warn)}</p>` : ''}
    <h3>Čo ukázali vyšetrenia</h3><ul>${li(R.find)}</ul>
    <h3>Čo ukazuje model</h3>
    <p>${R.motion} Prepínač hore vpravo mení Petrov nález za zdravý stav, voľba Porovnať ich strieda každé 3 sekundy.</p>
    <div class="cmp"><div><h4>Petrov nález</h4><ul>${li(R.peter)}</ul></div><div><h4>Zdravý, želaný stav</h4><ul>${li(R.normal)}</ul></div></div>
    <div class="legend">${R.legend.map(l => `<span><i style="background:${l[0]}"></i>${l[1]}</span>`).join('')}</div>
    <h3>Cvičenia na posúdenie fyzioterapeutom</h3>
    <div class="exlist">${EX[r].map((e, i) => `
      <div class="ex" data-i="${i}"><button aria-expanded="false"><span class="n">${i + 1}</span><span class="t">${esc(e.name)}<span class="g">${esc(e.goal)}</span></span><span class="go">Prehrať</span></button>
      <div class="body"><ol>${li(e.steps)}</ol><p class="dose"><b>Orientačne:</b> ${esc(e.dose)}. Dávku upraví fyzioterapeut.</p><p class="care">Pozor: ${esc(e.care)}</p></div></div>`).join('')}
    </div>
    <div class="foot">
      <p><b>Odporúčanie ortopéda z 11. 11. 2026:</b> rehabilitácia na aktiváciu dynamických stabilizátorov, po jej skončení cvičiť 3× týždenne dlhodobo a bicyklovať 15 minút denne.</p>
      <p>Model je zjednodušená ilustrácia zostavená z textu lekárskych správ (ortopéd 8. 10. a 11. 11. 2026, MR kolena 8. 10. 2026, ortopéd 19. 2. a 22. 4. 2025). Nezobrazuje skutočné snímky. Pohyb v Petrovom stave je znázornený schematicky.</p>
      <p>Cviky sú bežné príklady, nie predpis. Výber, rozsah a záťaž určuje fyzioterapeut po vyšetrení. Cvičí sa bez bolesti.</p>
    </div>`;
  panel.scrollTop = 0;
  panel.querySelectorAll('.ex').forEach(el => el.querySelector('button').onclick = () => {
    const e = EX[r][+el.dataset.i], was = el.classList.contains('on');
    panel.querySelectorAll('.ex.on').forEach(x => { x.classList.remove('on'); x.querySelector('button').setAttribute('aria-expanded', 'false'); });
    if (was) { transition(() => { S.ex = null; if (S.level === 1) S.level = 2; }); return; }
    el.classList.add('on'); el.querySelector('button').setAttribute('aria-expanded', 'true');
    transition(() => { S.ex = e; S.tm = 0; S.playing = S.playing || !reduce; });
    if (window.innerWidth <= 900) stageEl.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
  });
}

/* ───────────── štart ───────────── */
window.__atlas = { S, set(o) { Object.assign(S, o); if (o.exRef) S.ex = EX[o.exRef[0]][o.exRef[1]]; activate(true); cam.dist = goal.dist; syncUI(); renderPanel(S.region); }, EX };
resize(); renderPanel(S.region); activate(true); cam.dist = goal.dist; syncUI();
requestAnimationFrame(frame);
})();
