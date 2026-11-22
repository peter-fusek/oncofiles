# Movement-atlas meshes (#841)

The hosted atlas (`/movement-atlas`, `src/oncofiles/atlas/`) draws real
anatomical meshes instead of cylinders and spheres. This folder holds the two
scripts that produce everything binary or vendored in that folder, so each file
can be rebuilt from its public source.

| Output in `src/oncofiles/atlas/` | Script | Source |
| --- | --- | --- |
| `mesh-figure.glb`, `mesh-spine.glb`, `mesh-knee.glb`, `mesh-shoulder.glb` | `build_meshes.py` | BodyParts3D release 4.0 |
| `three.module.js`, `GLTFLoader.js`, `BufferGeometryUtils.js`, `RoomEnvironment.js` | `vendor_three.py` | three.js r149 (npm `three@0.149.0`) |

Licence and credit of the meshes: `src/oncofiles/atlas/ATTRIBUTION.md` (it is
served at `/movement-atlas/ATTRIBUTION.md` and linked from the page). The
derived meshes are shared under CC BY-SA 4.0; that covers the four `.glb` files
only. The licence is referenced by its URI, the legal text is not copied into
the repo (the local fixture guard flags numbered legal clauses).

## Rebuild the meshes

Downloads are untrusted data: keep them in their own directory outside `src/`,
never run anything from them, run Python with `-I`.

```bash
mkdir -p tmp/bp3d && cd tmp/bp3d
curl -O https://dbarchive.biosciencedbc.jp/data/bodyparts3d/LATEST/isa_BP3D_4.0_obj_99.zip
shasum -a 256 isa_BP3D_4.0_obj_99.zip
# compare with the checksum recorded in the pull request of issue #841 (fetched 2026-11-15)
unzip -q isa_BP3D_4.0_obj_99.zip && cd ../..

uv run --no-project --with numpy --with scipy --with trimesh \
  --with fast-simplification --with scikit-image \
  python -I scripts/atlas_meshes/build_meshes.py tmp/bp3d src/oncofiles/atlas
```

The server is slow (about 40 kB/s per connection); several ranged requests in
parallel help. `tmp/` is not committed. The build takes about a minute and
prints triangle counts, the measured landmarks stay inside the files. Pass
stage names (`knee spine shoulder figure`) to rebuild only some.

What the script does, in order: reads the listed OBJ parts (table `SOURCES`),
rotates them to the atlas frame, welds, decimates or subdivides, cuts long
parts near the joint and caps the cut, measures landmarks, writes a plain GLB
(float32 positions and indices, no Draco/meshopt, so the page needs no WASM
decoder). The figure goes through a voxel volume: see `build_figure`.

The tools (`numpy`, `scipy`, `trimesh`, `fast-simplification`, `scikit-image`)
are used only by this script. They are not dependencies of the app.

## Re-vendor three.js

```bash
mkdir -p tmp/three && cd tmp/three
npm pack three@0.149.0 --ignore-scripts && tar xzf three-0.149.0.tgz && cd ../..
python3 -I scripts/atlas_meshes/vendor_three.py tmp/three/package src/oncofiles/atlas
```

The only change to the official files is the import path (`'three'` becomes
`'./three.module.js'`): the atlas CSP allows no inline script, so there is no
import map.

The videos of the landing section are rendered from the page by
`scripts/atlas_video/render.py` (README there).

## After a rebuild

- `uv run pytest tests/test_atlas_showcase.py` (allowlist, sizes, metadata).
- Open `/movement-atlas` and look at every area at levels 2 and 3 in both
  states, and at one exercise per area. The page reads landmarks from the
  files, but camera distances and label offsets in `app.js` were tuned by eye.
- Never commit a mesh whose source or licence is not written down in
  `ATTRIBUTION.md`.
