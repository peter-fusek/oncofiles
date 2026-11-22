"""Vendor three.js r149 as same-origin ES modules for the movement atlas.

r149 no longer ships the non-module ``examples/js`` loaders, so the atlas loads
three, its GLTFLoader and the RoomEnvironment (image-based lighting) as ES
modules. The atlas CSP is ``script-src 'self'``
with no inline script, so an import map is not available: the only change made
to the official files is the module specifier (``'three'`` and
``'../utils/…'`` become relative same-directory paths).

Usage (the package directory is untrusted data: nothing in it is executed)::

    npm pack three@0.149.0 --ignore-scripts && tar xzf three-0.149.0.tgz
    python -I scripts/atlas_meshes/vendor_three.py <dir>/package src/oncofiles/atlas
"""

from __future__ import annotations

import sys
from pathlib import Path

FILES = {
    "build/three.module.js": "three.module.js",
    "examples/jsm/loaders/GLTFLoader.js": "GLTFLoader.js",
    "examples/jsm/utils/BufferGeometryUtils.js": "BufferGeometryUtils.js",
    # image-based lighting of the realistic materials (#830 follow-up)
    "examples/jsm/environments/RoomEnvironment.js": "RoomEnvironment.js",
}
REWRITES = {
    "from 'three';": "from './three.module.js';",
    "from '../utils/BufferGeometryUtils.js';": "from './BufferGeometryUtils.js';",
}
BANNER = (
    "// three.js r149 (MIT, Copyright 2010-2023 three.js authors), official file {src};\n"
    "// only the import specifiers were made relative (scripts/atlas_meshes/vendor_three.py).\n"
)


def main(package: Path, out: Path) -> None:
    for src, name in FILES.items():
        text = (package / src).read_text(encoding="utf-8")
        for old, new in REWRITES.items():
            text = text.replace(old, new)
        if "from 'three'" in text or "from '../" in text:
            raise SystemExit(f"unrewritten import left in {src}")
        if name != "three.module.js":  # the build already starts with its @license header
            text = BANNER.format(src=src) + text
        (out / name).write_text(text, encoding="utf-8")
        print(name, len(text.encode()))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
