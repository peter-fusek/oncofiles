"""Build the movement-atlas meshes (#841) from BodyParts3D release 4.0.

Source: BodyParts3D, (c) The Database Center for Life Science (see
``src/oncofiles/atlas/ATTRIBUTION.md`` for the licence). The input is the
extracted ``isa_BP3D_4.0_obj_99.zip`` from
https://dbarchive.biosciencedbc.jp/data/bodyparts3d/LATEST/ . It is treated as
data only: the OBJ files are parsed by the small reader below, nothing in the
download directory is imported or executed (run Python with ``-I``).

    uv run --no-project --with numpy --with scipy --with trimesh \
        --with fast-simplification --with scikit-image \
        python -I scripts/atlas_meshes/build_meshes.py \
        <dir with isa_BP3D_4.0_obj_99/> src/oncofiles/atlas [knee spine shoulder figure]

These tools are needed only here, never by the app (no runtime dependency).

Output: one GLB per atlas stage (``mesh-knee.glb``, ``mesh-spine.glb``,
``mesh-shoulder.glb``, ``mesh-figure.glb``): float32 positions + indices, no
compression (so no WASM decoder is needed under the atlas CSP), normals are
computed in the browser. Landmarks the page needs (joint centres, disc frames,
facet joints, ligament attachment points) are measured from the meshes here and
stored in the scene ``extras``; the page never hardcodes them.

Coordinates. BodyParts3D: mm, x = left, y = posterior, z = up. Atlas: y = up,
z = anterior, x = left, i.e. (x, z, -y); areas are in cm around the joint
centre, the figure is in metres around the pelvis.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import numpy as np

COPYRIGHT = (
    "BodyParts3D, (c) The Database Center for Life Science, CC BY 4.0 "
    "(release 4.0 files: CC BY-SA 2.1 Japan); modified, shared under CC BY-SA 4.0"
)

# element file -> (FMA concept, English name) for every source file used
SOURCES = {
    "FJ3365": ("FMA24474", "right femur"),
    "FJ3387": ("FMA24477", "right tibia"),
    "FJ3366": ("FMA24480", "right fibula"),
    "FJ3381": ("FMA24486", "right patella"),
    "FJ1433": ("FMA38928", "right rectus femoris"),
    "FJ1443": ("FMA38932", "right vastus medialis"),
    "FJ1442": ("FMA38930", "right vastus lateralis"),
    "FJ1395": ("FMA45888", "long head of right biceps femoris"),
    "FJ1435": ("FMA22448", "right semimembranosus"),
    "FJ1430": ("FMA22591", "right popliteus"),
    "FJ3162": ("FMA13074", "third lumbar vertebra"),
    "FJ3165": ("FMA13075", "fourth lumbar vertebra"),
    "FJ3168": ("FMA13076", "fifth lumbar vertebra"),
    "FJ3393": ("FMA16202", "sacrum"),
    "FJ3215": ("FMA16035", "intervertebral disk of third lumbar vertebra"),
    "FJ3216": ("FMA16036", "intervertebral disk of fourth lumbar vertebra"),
    "FJ3217": ("FMA16037", "intervertebral disk of fifth lumbar vertebra"),
    "FJ3152": ("FMA16586", "right hip bone"),
    "FJ3288": ("FMA16587", "left hip bone"),
    "FJ1527": ("FMA22740", "right iliocostalis lumborum"),
    "FJ1527M": ("FMA22741", "left iliocostalis lumborum"),
    "FJ1535": ("FMA22751", "right longissimus thoracis"),
    "FJ1535M": ("FMA22753", "left longissimus thoracis"),
    "FJ1431": ("FMA22342", "right psoas major"),
    "FJ1431M": ("FMA22343", "left psoas major"),
    "FJ3279": ("FMA13396", "left scapula"),
    "FJ3237": ("FMA13323", "left clavicle"),
    "FJ3262": ("FMA23131", "left humerus"),
    "FJ1506M": ("FMA32545", "left supraspinatus"),
    "FJ1500M": ("FMA32548", "left infraspinatus muscle"),
    "FJ1504M": ("FMA13415", "left subscapularis"),
    "FJ1468M": ("FMA34681", "clavicular part of left deltoid"),
    "FJ1467M": ("FMA34683", "acromial part of left deltoid"),
    "FJ1513M": ("FMA34685", "spinal part of left deltoid"),
    "FJ1478M": ("FMA37687", "long head of left biceps brachii"),
    "FJ2810": ("FMA7163", "skin"),
    # read for landmarks only (joint centres of the figure), not shipped
    "FJ3259": ("FMA24475", "left femur"),
    "FJ3282": ("FMA24478", "left tibia"),
    "FJ3368": ("FMA23130", "right humerus"),
    "FJ3349": ("FMA23464", "right radius"),
    "FJ3277": ("FMA23465", "left radius"),
    "FJ3172": ("FMA12525", "seventh cervical vertebra"),
    "FJ3156": ("FMA10081", "twelfth thoracic vertebra"),
}

SRC: Path


# ── mesh helpers ──────────────────────────────────────────────────────────


def load(fid: str) -> tuple[np.ndarray, np.ndarray]:
    """One OBJ in atlas orientation (mm), welded."""
    assert fid in SOURCES, fid
    vs, fs = [], []
    with open(SRC / "isa_BP3D_4.0_obj_99" / f"{fid}.obj", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("v "):
                x, y, z = (float(t) for t in line.split()[1:4])
                vs.append((x, z, -y))
            elif line.startswith("f "):
                fs.append([int(t.split("/")[0]) - 1 for t in line.split()[1:4]])
    return weld(np.array(vs), np.array(fs))


def compact(v, f):
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    used = np.unique(f)
    remap = -np.ones(len(v), dtype=np.int64)
    remap[used] = np.arange(len(used))
    return v[used], remap[f]


def weld(v, f):
    _, idx, inv = np.unique(np.round(v, 3), axis=0, return_index=True, return_inverse=True)
    return compact(v[idx], inv.reshape(-1)[f])


def decimate(v, f, target):
    if len(f) <= target:
        return v, f
    import fast_simplification

    v2, f2 = fast_simplification.simplify(
        v.astype(np.float32), f.astype(np.int32), target_count=int(target)
    )
    return weld(v2.astype(np.float64), f2.astype(np.int64))


def subdivide(v, f):
    from trimesh.remesh import subdivide_loop

    v2, f2 = subdivide_loop(v, f, iterations=1)
    return weld(np.asarray(v2, dtype=np.float64), np.asarray(f2, dtype=np.int64))


def face_normals(v, f):
    n = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def vertex_normals(v, f):
    n = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    out = np.zeros_like(v)
    for k in range(3):
        np.add.at(out, f[:, k], n)
    return out / np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-12)


def clip(v, f, normal, d, cap=True):
    """Keep the part with normal·p <= d; close the cut with a flat cap."""
    normal = np.asarray(normal, dtype=np.float64)
    s = v @ normal - d
    s[np.abs(s) < 1e-7] = -1e-7
    inside = s <= 0
    if inside.all():
        return v, f
    verts = [*v]
    cache: dict[tuple[int, int], int] = {}

    def cut(a, b):
        key = (min(a, b), max(a, b))
        if key not in cache:
            t = s[a] / (s[a] - s[b])
            verts.append(v[a] + t * (v[b] - v[a]))
            cache[key] = len(verts) - 1
        return cache[key]

    out, rim = [], []
    for tri in f:
        ins = inside[tri]
        k = int(ins.sum())
        if k == 3:
            out.append(tuple(tri))
        elif k == 1:
            i = int(np.argmax(ins))
            a, b, c = tri[i], tri[(i + 1) % 3], tri[(i + 2) % 3]
            p, q = cut(a, b), cut(a, c)
            out.append((a, p, q))
            rim.append((p, q))
        elif k == 2:
            i = int(np.argmin(ins))
            a, b, c = tri[i], tri[(i + 1) % 3], tri[(i + 2) % 3]
            p, q = cut(c, a), cut(a, b)
            out.append((b, c, p))
            out.append((b, p, q))
            rim.append((p, q))
    if cap and rim:
        parent: dict[int, int] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for p, q in rim:
            parent[find(p)] = find(q)
        groups: dict[int, list[tuple[int, int]]] = {}
        for p, q in rim:
            groups.setdefault(find(p), []).append((p, q))
        for edges in groups.values():
            if len(edges) < 3:
                continue
            ids = sorted({i for e in edges for i in e})
            dup = {}
            for i in ids:  # own vertices, so the cap keeps a hard edge
                verts.append(verts[i])
                dup[i] = len(verts) - 1
            verts.append(np.mean([verts[i] for i in ids], axis=0))
            centre = len(verts) - 1
            for p, q in edges:
                a, b = np.asarray(verts[p]), np.asarray(verts[q])
                flip = np.dot(np.cross(a - verts[centre], b - verts[centre]), normal) < 0
                out.append((centre, dup[q], dup[p]) if flip else (centre, dup[p], dup[q]))
    return compact(np.array(verts), np.array(out, dtype=np.int64))


def shell(v, f, mask, offset):
    """A thin layer over the selected faces (articular cartilage on a bone)."""
    vn = vertex_normals(v, f)
    return compact(v + vn * offset, f[mask])


def fit_sphere(p):
    a = np.column_stack([2 * p, np.ones(len(p))])
    sol, *_ = np.linalg.lstsq(a, (p**2).sum(1), rcond=None)
    c = sol[:3]
    return c, float(np.sqrt(sol[3] + c @ c))


def quat_from_basis(x, y, z):
    m = np.column_stack([x, y, z])
    t = np.trace(m)
    if t > 0:
        s = 0.5 / np.sqrt(t + 1)
        q = [(m[2, 1] - m[1, 2]) * s, (m[0, 2] - m[2, 0]) * s, (m[1, 0] - m[0, 1]) * s, 0.25 / s]
    else:
        i = int(np.argmax(np.diag(m)))
        j, k = (i + 1) % 3, (i + 2) % 3
        s = 2 * np.sqrt(1 + m[i, i] - m[j, j] - m[k, k])
        q = [0.0, 0.0, 0.0, (m[k, j] - m[j, k]) / s]
        q[i] = 0.25 * s
        q[j] = (m[j, i] + m[i, j]) / s
        q[k] = (m[k, i] + m[i, k]) / s
    return q


def quat_between(a, b):
    a, b = a / np.linalg.norm(a), b / np.linalg.norm(b)
    w = 1 + a @ b
    q = np.array([*np.cross(a, b), w])
    return (q / np.linalg.norm(q)).tolist()


def unit(a):
    a = np.asarray(a, dtype=np.float64)
    return a / np.linalg.norm(a)


def rnd(x):
    if isinstance(x, dict):
        return {k: rnd(val) for k, val in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [rnd(val) for val in x]
    if isinstance(x, str):
        return x
    return round(float(x), 4)


def write_glb(path: Path, meshes: list[dict], extras: dict) -> int:
    """Minimal glTF 2.0 binary: one node per mesh, POSITION + indices (+ custom
    ``_JOINTS`` / ``_WEIGHTS`` for the figure)."""
    blob = bytearray()
    views, accessors, gl_meshes, nodes = [], [], [], []

    def add(data: bytes, target: int | None) -> int:
        while len(blob) % 4:
            blob.append(0)
        view = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            view["target"] = target
        blob.extend(data)
        views.append(view)
        return len(views) - 1

    for m in meshes:
        v = np.ascontiguousarray(m["v"], dtype=np.float32)
        f = np.asarray(m["f"])
        wide = len(v) > 65535
        idx = np.ascontiguousarray(f.reshape(-1), dtype=np.uint32 if wide else np.uint16)
        accessors.append(
            {
                "bufferView": add(v.tobytes(), 34962),
                "componentType": 5126,
                "count": len(v),
                "type": "VEC3",
                "min": v.min(0).tolist(),
                "max": v.max(0).tolist(),
            }
        )
        attrs = {"POSITION": len(accessors) - 1}
        for name, arr, normalized in m.get("attrs", []):
            arr = np.ascontiguousarray(arr, dtype=np.uint8)
            acc = {
                "bufferView": add(arr.tobytes(), 34962),
                "componentType": 5121,
                "count": len(arr),
                "type": "VEC4",
            }
            if normalized:
                acc["normalized"] = True
            accessors.append(acc)
            attrs[name] = len(accessors) - 1
        accessors.append(
            {
                "bufferView": add(idx.tobytes(), 34963),
                "componentType": 5125 if wide else 5123,
                "count": len(idx),
                "type": "SCALAR",
            }
        )
        gl_meshes.append({"primitives": [{"attributes": attrs, "indices": len(accessors) - 1}]})
        nodes.append({"name": m["name"], "mesh": len(gl_meshes) - 1})
    while len(blob) % 4:
        blob.append(0)
    doc = {
        "asset": {"version": "2.0", "generator": "oncofiles atlas_meshes", "copyright": COPYRIGHT},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes))), "extras": extras}],
        "nodes": nodes,
        "meshes": gl_meshes,
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=True).encode()
    text += b" " * (-len(text) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(text), 0x4E4F534A))
        fh.write(text)
        fh.write(struct.pack("<II", len(blob), 0x004E4942))
        fh.write(blob)
    tris = sum(len(m["f"]) for m in meshes)
    print(f"{path.name}: {total} bytes, {len(meshes)} meshes, {tris} triangles")
    for m in meshes:
        print(f"   {m['name']:<22} {len(m['v']):>6} v {len(m['f']):>6} f")
    return total


class Area:
    """Parts in local cm around an origin (given in atlas mm)."""

    def __init__(self, origin):
        self.o = np.asarray(origin, dtype=np.float64)
        self.meshes: list[dict] = []

    def loc(self, v):
        return (v - self.o) / 10.0

    def part(self, fid, target=None, min_faces=0):
        v, f = load(fid)
        if target:
            v, f = decimate(v, f, target)
        if len(f) < min_faces:
            v, f = subdivide(v, f)
        return self.loc(v), f

    def add(self, name, v, f):
        self.meshes.append({"name": name, "v": v, "f": f})


def band(v, f, lo=None, hi=None, axis=1):
    n = np.zeros(3)
    n[axis] = 1
    if hi is not None:
        v, f = clip(v, f, n, hi)
    if lo is not None:
        v, f = clip(v, f, -n, -lo)
    return v, f


# ── knee (right) ──────────────────────────────────────────────────────────


def knee_origin(femur_v):
    """Flexion axis: centre of the condyles, ~24 mm above the distal surface."""
    ymin = femur_v[:, 1].min()
    d = femur_v[femur_v[:, 1] < ymin + 30]
    return np.array([(d[:, 0].min() + d[:, 0].max()) / 2, ymin + 24, d[:, 2].min() + 26])


def build_knee(out: Path) -> int:
    a = Area(knee_origin(load("FJ3365")[0]))
    top, bottom = 15.0, -17.0
    fem = band(*a.part("FJ3365", min_faces=3200), hi=top)
    tib = band(*a.part("FJ3387", min_faces=3200), lo=bottom)
    fib = band(*a.part("FJ3366", min_faces=2000), lo=bottom)
    pat = a.part("FJ3381", min_faces=1000)
    a.add("femur", *fem)
    a.add("tibia", *tib)
    a.add("fibula", *fib)
    a.add("patella", *pat)

    fv, ff = fem
    fn, fc = face_normals(fv, ff), fv[ff].mean(1)
    width = float(np.ptp(fv[np.abs(fv[:, 1]) < 2.5][:, 0]))
    cond = (fc[:, 1] < 0.9) & (np.abs(fn[:, 0]) < 0.8)
    troch = (fc[:, 1] < 3.2) & (fn[:, 2] > 0.45) & (np.abs(fc[:, 0]) < width * 0.3)
    a.add("cart_femur", *shell(fv, ff, cond | troch, 0.06))
    tv, tf = tib
    tn, tc = face_normals(tv, tf), tv[tf].mean(1)
    ttop = float(tv[:, 1].max())
    a.add("cart_tibia", *shell(tv, tf, (tn[:, 1] > 0.7) & (tc[:, 1] > ttop - 1.1), 0.06))
    pv, pf = pat
    a.add("cart_patella", *shell(pv, pf, face_normals(pv, pf)[:, 2] < -0.3, 0.07))

    for name, fid in [
        ("rectus_femoris", "FJ1433"),
        ("vastus_medialis", "FJ1443"),
        ("vastus_lateralis", "FJ1442"),
        ("biceps_femoris", "FJ1395"),
        ("semimembranosus", "FJ1435"),
    ]:
        a.add(name, *band(*a.part(fid, target=2600), lo=bottom, hi=top))
    a.add("popliteus", *a.part("FJ1430", min_faces=1200))

    near = fv[np.abs(fv[:, 1]) < 2.5]
    plateau = {}
    for side, sel in (("med", tv[:, 0] > 0.8), ("lat", tv[:, 0] < -0.8)):
        p = tv[sel & (tv[:, 1] > ttop - 1.3)]
        plateau[side] = [p[:, 0].mean(), float(np.percentile(p[:, 1], 80)), p[:, 2].mean()]
    tub = tv[(tv[:, 1] < ttop - 2.5) & (tv[:, 1] > ttop - 5.5)]
    mcl = tv[(tv[:, 1] < ttop - 5.0) & (tv[:, 1] > ttop - 7.0)]
    fibv = fib[0]
    extras = {
        "width": width,
        "femur": {
            "med": near[np.argmax(near[:, 0])],
            "lat": near[np.argmin(near[:, 0])],
            "front": float(fv[(fv[:, 1] > 0) & (fv[:, 1] < 3)][:, 2].max()),
            "back": float(near[:, 2].min()),
            "bottom": float(fv[:, 1].min()),
        },
        "patella": {
            "c": pv.mean(0),
            "top": float(pv[:, 1].max()),
            "apex": pv[np.argmin(pv[:, 1])],
            "med": pv[np.argmax(pv[:, 0])],
            "lat": pv[np.argmin(pv[:, 0])],
            "front": float(pv[:, 2].max()),
            "back": float(pv[:, 2].min()),
        },
        "tibia": {
            "top": ttop,
            "plateau": plateau,
            "tuberosity": tub[np.argmax(tub[:, 2])],
            "med": mcl[np.argmax(mcl[:, 0])],
            "back": float(tv[tv[:, 1] > ttop - 2][:, 2].min()),
        },
        "fibula": {"head": fibv[np.argmax(fibv[:, 1])]},
        "cut": {"top": top, "bottom": bottom},
    }
    return write_glb(out / "mesh-knee.glb", a.meshes, rnd(extras))


# ── lumbar spine L3–S1 ────────────────────────────────────────────────────


def disc_frame(v):
    c = v.mean(0)
    w, vec = np.linalg.eigh(np.cov((v - c).T))
    axis = vec[:, 0] * (1 if vec[1, 0] > 0 else -1)  # thinnest direction, pointing up
    x = unit(np.array([1.0, 0, 0]) - axis[0] * axis)
    z = np.cross(x, axis)
    rel = v - c
    core = rel[np.linalg.norm(rel - np.outer(rel @ axis, axis), axis=1) < 0.9]
    return {
        "c": c,
        "q": quat_from_basis(x, axis, z),
        "axis": axis,
        "h": float(np.ptp(core @ axis)),
        "rx": float(np.ptp(rel @ x) / 2),
        "rz": float(np.ptp(rel @ z) / 2),
        "front": c + z * float((rel @ z).max()),
    }


def facet_pair(upper, lower, body_z):
    """Left / right facet joint between two bones: where their posterior parts
    come closest."""
    res = {}
    for side, sign in (("l", 1), ("r", -1)):
        u = upper[(upper[:, 2] < body_z - 1.5) & (upper[:, 0] * sign > 0.5)]
        lo = lower[(lower[:, 2] < body_z - 1.0) & (lower[:, 0] * sign > 0.5)]
        d = np.linalg.norm(u[:, None, :] - lo[None, :, :], axis=2)
        i, j = np.nonzero(d < d.min() + 0.12)
        res[side] = (u[i].mean(0) + lo[j].mean(0)) / 2
    return res


def build_spine(out: Path) -> int:
    d0 = load("FJ3217")[0]
    o = d0.mean(0)
    o[0] = 0
    a = Area(o)
    sac = a.part("FJ3393", target=6500)
    verts = {n: a.part(fid) for n, fid in (("L5", "FJ3168"), ("L4", "FJ3165"), ("L3", "FJ3162"))}
    discs = [a.part(fid) for fid in ("FJ3217", "FJ3216", "FJ3215")]  # L5/S1, L4/5, L3/4
    a.add("sacrum", *sac)
    for n, m in verts.items():
        a.add(n, *m)
    for n, m in zip(("disc_L5S1", "disc_L4L5", "disc_L3L4"), discs, strict=True):
        a.add(n, *m)
    floor = float(sac[0][:, 1].min()) - 0.6
    for n, fid in (("hip_r", "FJ3152"), ("hip_l", "FJ3288")):
        a.add(n, *band(*a.part(fid), lo=floor))
    top = float(verts["L3"][0][:, 1].max()) + 2.2
    for n, fid in (
        ("iliocostalis_r", "FJ1527"),
        ("iliocostalis_l", "FJ1527M"),
        ("longissimus_r", "FJ1535"),
        ("longissimus_l", "FJ1535M"),
    ):
        v, f = a.part(fid)
        a.add(n, *decimate(*band(v, f, hi=top), 2600))
    for n, fid in (("psoas_r", "FJ1431"), ("psoas_l", "FJ1431M")):
        v, f = a.part(fid)
        a.add(n, *decimate(*band(v, f, lo=floor + 1.0, hi=top), 2400))

    frames = [disc_frame(m[0]) for m in discs]
    order = ["L5", "L4", "L3"]
    lower = [sac[0], verts["L5"][0], verts["L4"][0]]
    levels = []
    for i, name in enumerate(order):
        v = verts[name][0]
        fr = frames[i]
        body = v[v[:, 2] > fr["c"][2] - fr["rz"] * 1.05]
        mid = v[np.abs(v[:, 0]) < 0.7]
        levels.append(
            {
                "name": name,
                "disc": fr,
                "body": body.mean(0),
                "side": float(np.ptp(body[:, 0]) / 2),
                "front": float(body[:, 2].max()),
                "spinous": mid[np.argmin(mid[:, 2])],
                "facets": facet_pair(v, lower[i], fr["c"][2]),
            }
        )
    sv = sac[0]
    stop = float(sv[:, 1].max())
    dorsal = {}
    for side, sign in (("l", 1), ("r", -1)):
        p = sv[(np.abs(sv[:, 0] - sign * 1.7) < 0.6) & (np.abs(sv[:, 1] - (stop - 3.2)) < 0.8)]
        dorsal[side] = p[np.argmin(p[:, 2])]
    extras = {
        "levels": levels,
        "sacrum": {
            "c": sv.mean(0),
            "top": stop,
            "bottom": float(sv[:, 1].min()),
            "dorsal": dorsal,
            "front": sv[np.argmax(sv[:, 2] - np.abs(sv[:, 1] - (stop - 3.0)))],
        },
        "top": top,
        "floor": floor,
    }
    return write_glb(out / "mesh-spine.glb", a.meshes, rnd(extras))


# ── shoulder (left) ───────────────────────────────────────────────────────


def humeral_head(v, medial_sign):
    """Centre and radius of the humeral head (mm, atlas orientation)."""
    topv = v[v[:, 1] > v[:, 1].max() - 45]
    med = topv[(topv[:, 0] - np.median(topv[:, 0])) * medial_sign > -2]
    c, r = fit_sphere(med)
    close = topv[np.abs(np.linalg.norm(topv - c, axis=1) - r) < 3]
    close = close[(close[:, 0] - c[0]) * medial_sign > -r * 0.3]
    if len(close) > 30:
        c, r = fit_sphere(close)
    return c, r


def build_shoulder(out: Path) -> int:
    hv_mm = load("FJ3262")[0]
    centre, radius_mm = humeral_head(hv_mm, -1)
    a = Area(centre)
    r = radius_mm / 10
    bottom = -16.5
    scap = a.part("FJ3279", target=9000)
    a.add("scapula", *scap)
    a.add("clavicle", *a.part("FJ3237", min_faces=2000))
    hv, hf = band(*a.part("FJ3262", min_faces=5000), lo=bottom)
    near = np.linalg.norm(hv[hf].mean(1), axis=1) < r * 1.25
    a.add("humerus", *compact(hv, hf[~near]))
    a.add("humerus_head", *compact(hv, hf[near]))
    for n, fid in (
        ("supraspinatus", "FJ1506M"),
        ("infraspinatus", "FJ1500M"),
        ("subscapularis", "FJ1504M"),
    ):
        a.add(n, *decimate(*a.part(fid, min_faces=2200), 4000))
    dv, df, base = [], [], 0
    for fid in ("FJ1468M", "FJ1467M", "FJ1513M"):
        v, f = a.part(fid)
        dv.append(v)
        df.append(f + base)
        base += len(v)
    a.add("deltoid", *decimate(*band(np.vstack(dv), np.vstack(df), lo=bottom), 6000))
    a.add("biceps_long", *band(*a.part("FJ1478M", min_faces=1500), lo=bottom))

    sv = scap[0]
    g = sv[np.linalg.norm(sv, axis=1) < r + 0.9]
    gc = g.mean(0)
    n = unit(-gc)  # from the socket to the head centre
    rel = g - gc
    flat = rel - np.outer(rel @ n, n)
    w, vec = np.linalg.eigh(np.cov(flat.T))
    up = vec[:, 2] * (1 if vec[1, 2] > 0 else -1)
    up = unit(up - (up @ n) * n)
    back = np.cross(up, n)  # x of the ring frame: posterior for a left shoulder
    acro = sv[(sv[:, 1] > r * 0.9) & (sv[:, 0] > -1.0)]
    shaft = hv[(hv[:, 1] < bottom + 3.5)].mean(0)
    extras = {
        "r": r,
        "glenoid": {
            "c": gc,
            "n": n,
            "up": up,
            "back": back,
            "q": quat_from_basis(back, up, n),
            "a": float(
                min(
                    np.percentile(np.abs(flat @ up), 96),
                    1.35 * np.percentile(np.abs(flat @ back), 96),
                )
            ),
            "b": float(np.percentile(np.abs(flat @ back), 96)),
        },
        "acromion": {"c": acro.mean(0), "top": float(acro[:, 1].max())},
        "bind_abduction": float(np.degrees(np.arctan2(shaft[0], -shaft[1]))),
        "shaft": shaft,
        "cut": bottom,
    }
    return write_glb(out / "mesh-shoulder.glb", a.meshes, rnd(extras))


# ── figure (skin with blend weights) ──────────────────────────────────────

JOINTS = [
    "root", "spine", "chest", "neck", "lSh", "lEl", "rSh", "rEl",
    "lHip", "lKn", "lAn", "rHip", "rKn", "rAn",
]  # fmt: skip


def body_solid(v, f, voxel):
    """The BodyParts3D skin is a shell about 2 mm thick (outer and inner
    surface in one mesh, joined at small openings). Turn it into a solid voxel
    body: flood-fill the space around it; everything else is body."""
    from scipy import ndimage

    lo = v.min(0) - 6 * voxel
    dims = np.ceil((v.max(0) - lo) / voxel).astype(int) + 6
    wall = np.zeros(dims, dtype=bool)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    for i in range(5):
        for j in range(5 - i):
            idx = (((a * i + b * j + c * (4 - i - j)) / 4 - lo) / voxel).astype(int)
            wall[idx[:, 0], idx[:, 1], idx[:, 2]] = True
    grow = 0
    while True:  # thicken the wall until the openings are closed
        lab, _ = ndimage.label(~wall)
        outside = lab == lab[0, 0, 0]
        if (~wall & ~outside).sum() * voxel**3 > 3e7:
            break
        grow += 1
        assert grow <= 5, "the skin shell leaks: no body cavity found"
        wall = ndimage.binary_dilation(wall)
    solid = ~outside
    if grow:
        solid = ndimage.binary_erosion(solid, iterations=grow)
    litres = solid.sum() * voxel**3 / 1e6
    print(f"   skin: wall thickened by {grow} voxels to close it, body {litres:.1f} l")
    return solid, lo


def solid_surface(solid, lo, voxel):
    from scipy import ndimage
    from skimage.measure import marching_cubes

    vol = ndimage.gaussian_filter(solid.astype(np.float32), 1.2)
    verts, faces, _, _ = marching_cubes(vol, 0.5)
    v = verts * voxel + lo + voxel / 2
    f = faces.astype(np.int64)
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    if np.einsum("ij,ij->i", a, np.cross(b, c)).sum() < 0:
        f = f[:, ::-1]
    return relax(*weld(v, f), 12)


def relax(v, f, iterations, amount=0.5):
    """Laplacian smoothing: takes the voxel steps out of the surface."""
    pairs = [f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]], f[:, [1, 0]], f[:, [2, 1]], f[:, [0, 2]]]
    e = np.unique(np.vstack(pairs), axis=0)
    cnt = np.maximum(np.bincount(e[:, 0], minlength=len(v)), 1)[:, None]
    v = v.copy()
    for _ in range(iterations):
        acc = np.zeros_like(v)
        np.add.at(acc, e[:, 0], v[e[:, 1]])
        v += amount * (acc / cnt - v)
    return v, f


def sstep(lo, hi, x):
    t = np.clip((x - lo) / (hi - lo), 0, 1)
    return t * t * (3 - 2 * t)


def build_figure(out: Path) -> int:
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components, dijkstra

    voxel = 3.0
    sv, sf = load("FJ2810")
    solid, lo = body_solid(sv, sf, voxel)
    nx, ny, nz = solid.shape
    xs = lo[0] + (np.arange(nx) + 0.5) * voxel
    ys = lo[1] + (np.arange(ny) + 0.5) * voxel
    zs = lo[2] + (np.arange(nz) + 0.5) * voxel

    # In the source body the upper arms lie against the chest and the skin is
    # fused there. Open a slit where that bridge is thinnest, so an arm can be
    # raised without dragging a web of chest skin with it.
    for sign, humerus in ((1, "FJ3262"), (-1, "FJ3368")):
        hv = load(humerus)[0]
        sh = humeral_head(hv, -sign)[0]
        el = hv[hv[:, 1] < hv[:, 1].min() + 18].mean(0)
        rows = []
        for iy in np.nonzero((ys < sh[1] - 50) & (ys > el[1] - 20))[0]:
            xa = sh[0] + (el[0] - sh[0]) * (sh[1] - ys[iy]) / (sh[1] - el[1])
            cand = np.nonzero(((xa - xs) * sign > 15) & ((xa - xs) * sign < 75))[0]
            depth = solid[cand, iy, :].sum(1)
            rows.append((ys[iy], xs[cand[np.argmin(depth)]], depth.min() * voxel))
        rows = np.array(rows)
        fused = rows[(rows[:, 2] > 0) & (rows[:, 0] < sh[1] - 85)]
        slope, offset = np.polyfit(fused[:, 0], fused[:, 1], 1)
        for iy in np.nonzero((ys < sh[1] - 80) & (ys > fused[:, 0].min() - 12))[0]:
            solid[np.abs(xs - (slope * ys[iy] + offset)) < 1.6 * voxel, iy, :] = False
        side = "left" if sign > 0 else "right"
        print(
            f"   arm slit {side}: fused over {np.ptp(fused[:, 0]):.0f} mm, "
            f"up to {fused[:, 2].max():.0f} mm deep, x = {slope:.3f} y + {offset:.0f}"
        )

    # Neutral mannequin: in a box around the groin keep only what survives a
    # morphological opening with a 30 mm ball. Trunk and thighs do, anything
    # thinner than about 6 cm that sticks out of them does not.
    from scipy import ndimage

    midback = (np.abs(sv[:, 0]) < 4) & (sv[:, 2] < np.median(sv[:, 2])) & (sv[:, 1] > 600)
    crotch = float(sv[midback][:, 1].min())  # where the legs meet
    radius, pad = 10, 14
    ix = np.nonzero(np.abs(xs) < 55)[0]
    iy = np.nonzero((ys > crotch - 60) & (ys < crotch + 110))[0]
    iz = np.nonzero(zs > np.median(sv[:, 2]) - 25)[0]
    box = (
        slice(ix[0] - pad, ix[-1] + 1 + pad),
        slice(iy[0] - pad, iy[-1] + 1 + pad),
        slice(iz[0] - pad, min(iz[-1] + 1 + pad, nz)),
    )
    sub = solid[box]
    core = ndimage.distance_transform_edt(sub) > radius
    opened = ndimage.distance_transform_edt(~core) <= radius
    inner = np.zeros_like(sub)
    inner[pad:-pad, pad:-pad, pad:] = True
    removed = sub & inner & ~opened
    sub[removed] = False
    # thighs apart (the closing step above fuses them near the crotch)
    solid[np.ix_(np.abs(xs) < 1.6 * voxel, ys < crotch - 3, np.ones(nz, dtype=bool))] = False
    print(f"   groin: crotch at {crotch:.0f} mm, {removed.sum() * voxel**3 / 1000:.0f} cm3 removed")

    v, f = solid_surface(solid, lo, voxel)
    v, f = relax(*decimate(v, f, 30000), 2, 0.3)

    def edge_graph():
        e = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
        length = np.linalg.norm(v[e[:, 0]] - v[e[:, 1]], axis=1)
        g = coo_matrix((length, (e[:, 0], e[:, 1])), shape=(len(v), len(v))).tocsr()
        return g.maximum(g.T)

    graph = edge_graph()
    _, comp = connected_components(graph, directed=False)
    keep = comp == np.bincount(comp).argmax()
    if not keep.all():
        v, f = compact(v, f[keep[f].all(1)])
        graph = edge_graph()
    n = len(v)

    j: dict[str, np.ndarray] = {}
    for s, sign, femur, tibia, humerus, radius in (
        ("l", 1, "FJ3259", "FJ3282", "FJ3262", "FJ3277"),
        ("r", -1, "FJ3365", "FJ3387", "FJ3368", "FJ3349"),
    ):
        fv = load(femur)[0]
        medial = fv[:, 0] * sign < (fv[:, 0] * sign).min() + 50
        head = fv[(fv[:, 1] > fv[:, 1].max() - 52) & medial]
        j[s + "Hip"] = fit_sphere(head)[0]
        j[s + "Kn"] = knee_origin(fv)
        tv = load(tibia)[0]
        j[s + "An"] = tv[tv[:, 1] < tv[:, 1].min() + 15].mean(0)
        hv = load(humerus)[0]
        j[s + "Sh"] = humeral_head(hv, -sign)[0]
        j[s + "El"] = hv[hv[:, 1] < hv[:, 1].min() + 18].mean(0)
        rv = load(radius)[0]
        j[s + "Wr"] = rv[rv[:, 1] < rv[:, 1].min() + 12].mean(0)
    mid = (j["lHip"] + j["rHip"]) / 2
    j["root"] = mid + np.array([0, 30.0, 0])
    for name, fid in (("spine", "FJ3216"), ("chest", "FJ3156"), ("neck", "FJ3172")):
        c = load(fid)[0].mean(0)
        j[name] = np.array([0, c[1], c[2]])

    weights = np.zeros((n, len(JOINTS)))
    col = {name: i for i, name in enumerate(JOINTS)}
    limb = np.zeros(n)
    tips = {}
    for s in ("l", "r"):
        # arm: geodesic distance from the fingertips, cut at the armpit
        fore = unit(j[s + "Wr"] - j[s + "El"])
        rel = v - j[s + "El"]
        along = rel @ fore
        off = np.linalg.norm(rel - np.outer(along, fore), axis=1)
        tip = int(np.argmax(np.where(off < 80, along, -1e9)))
        g = dijkstra(graph, indices=tip)
        sign = 1 if s == "l" else -1
        torso = (v[:, 0] * sign < j[s + "Sh"][0] * sign - 12) & (v[:, 1] < j[s + "Sh"][1] - 60)
        armpit = float(g[torso].min())
        # a long blend over the shoulder cap, a short one on the chest wall below the armpit
        reach = 25 + 70 * sstep(j[s + "Sh"][1] - 70, j[s + "Sh"][1] - 10, v[:, 1])
        share = 1 - sstep(armpit - 5, armpit + reach, g)
        bis = unit(unit(j[s + "El"] - j[s + "Sh"]) + fore)
        t_el = sstep(-40, 40, rel @ bis)
        weights[:, col[s + "Sh"]] = share * (1 - t_el)
        weights[:, col[s + "El"]] = share * t_el
        limb += share
        tips[s + "Hand"] = (v[tip], armpit)

        # leg: geodesic distance from the toes, cut at the crotch
        shin = unit(j[s + "An"] - j[s + "Kn"])
        foot = (np.abs(v[:, 0] - j[s + "An"][0]) < 75) & (v[:, 1] < j[s + "An"][1] + 25)
        toe = int(np.argmax(np.where(foot, np.linalg.norm(v - j[s + "An"], axis=1), -1)))
        g = dijkstra(graph, indices=toe)
        crotch = float(g[np.abs(v[:, 0]) < 8].min())
        share = 1 - sstep(crotch - 10, crotch + 130, g)
        thigh = unit(j[s + "Kn"] - j[s + "Hip"])
        t_kn = sstep(-50, 50, (v - j[s + "Kn"]) @ unit(thigh + shin))
        foot_dir = unit(v[toe] - j[s + "An"])
        t_an = np.minimum(t_kn, sstep(-25, 25, (v - j[s + "An"]) @ unit(shin + foot_dir) - 10))
        weights[:, col[s + "Hip"]] = share * (1 - t_kn)
        weights[:, col[s + "Kn"]] = share * (t_kn - t_an)
        weights[:, col[s + "An"]] = share * t_an
        limb += share
        j[s + "Toe"] = v[toe]
        tips[s + "Toe"] = (v[toe], crotch)
    over = np.maximum(limb, 1.0)
    weights /= over[:, None]
    torso = 1 - limb / over
    y = v[:, 1]
    t_neck = sstep(j["neck"][1] - 30, j["neck"][1] + 40, y)
    t_chest = np.maximum(sstep(j["chest"][1] - 60, j["chest"][1] + 60, y), t_neck)
    t_spine = np.maximum(sstep(j["spine"][1] - 50, j["spine"][1] + 50, y), t_chest)
    weights[:, col["root"]] = torso * (1 - t_spine)
    weights[:, col["spine"]] = torso * (t_spine - t_chest)
    weights[:, col["chest"]] = torso * (t_chest - t_neck)
    weights[:, col["neck"]] = torso * t_neck

    top4 = np.argsort(-weights, axis=1)[:, :4]
    w4 = np.take_along_axis(weights, top4, axis=1)
    w4 /= w4.sum(1, keepdims=True)
    q = np.round(w4 * 255).astype(np.int64)
    q[:, 0] += 255 - q.sum(1)

    down = np.array([0, -1.0, 0])
    corr = {
        "lSh": quat_between(j["lEl"] - j["lSh"], down),
        "rSh": quat_between(j["rEl"] - j["rSh"], down),
        "lEl": quat_between(j["lWr"] - j["lEl"], down),
        "rEl": quat_between(j["rWr"] - j["rEl"], down),
        "lHip": quat_between(j["lKn"] - j["lHip"], down),
        "rHip": quat_between(j["rKn"] - j["rHip"], down),
        "lKn": quat_between(j["lAn"] - j["lKn"], down),
        "rKn": quat_between(j["rAn"] - j["rKn"], down),
        "lAn": quat_between(j["lToe"] - j["lAn"], np.array([0, -0.3, 1.0])),
        "rAn": quat_between(j["rToe"] - j["rAn"], np.array([0, -0.3, 1.0])),
    }
    root = j["root"]
    extras = {
        "joints": {k: (p - root) / 1000 for k, p in j.items()},
        "corr": corr,
        "order": JOINTS,
        "height": float(np.ptp(v[:, 1]) / 1000),
    }
    for k, (p, d) in tips.items():
        print(f"   {k}: tip {np.round(p).tolist()} cut at geodesic {d:.0f} mm")
    mesh = {
        "name": "body",
        "v": (v - root) / 1000,
        "f": f,
        "attrs": [("_JOINTS", top4, False), ("_WEIGHTS", q, True)],
    }
    return write_glb(out / "mesh-figure.glb", [mesh], {**rnd(extras), "order": JOINTS})


def main() -> None:
    global SRC  # noqa: PLW0603
    SRC = Path(sys.argv[1])
    out = Path(sys.argv[2])
    only = set(sys.argv[3:])
    total = 0
    for name, fn in (
        ("knee", build_knee),
        ("spine", build_spine),
        ("shoulder", build_shoulder),
        ("figure", build_figure),
    ):
        if not only or name in only:
            total += fn(out)
    print("total bytes:", total)


if __name__ == "__main__":
    main()
