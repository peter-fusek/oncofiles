"""Child process for one re-OCR read (#667).

Started by ``reocr.reread_isolated`` as ``python -m oncofiles.curated.reocr_child``
with an allowlisted environment: no API keys, OAuth secrets or bearer tokens ever
reach it, and the read runs inside ``local_only`` so no cloud path can open even
by mistake. Drive I/O stays in the parent; the child only gets the file bytes.

Protocol: stdin = one JSON header line (``name``, ``mime``, ``passes``) followed
by the raw file bytes; stdout = one JSON object (the ``Reread`` fields plus
``peak_rss_mb``). ``--probe`` instead reports what the child can see, for tests.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from dataclasses import asdict

# Exact names and prefixes the child may inherit. An allowlist, so a new
# secret added to the server env can never leak into the child by default.
ENV_ALLOW = {"PATH", "HOME", "LANG", "TMPDIR", "TZ", "VIRTUAL_ENV", "SSL_CERT_FILE"}
ENV_ALLOW_PREFIXES = ("LOCAL_LLM_", "REOCR_", "LC_", "PYTHON")
SECRET_PROBES = (
    "ANTHROPIC_API_KEY",
    "MCP_BEARER_TOKEN",
    "DASHBOARD_SESSION_SECRET",
    "OAUTH_STATE_SECRET",
    "GOOGLE_CLIENT_SECRET",
)


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    src = os.environ if env is None else env
    return {k: v for k, v in src.items() if k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)}


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def main() -> int:
    from oncofiles.cloud_guard import CloudForbiddenError, check_cloud_allowed, local_only

    with local_only("curated re-OCR child"):
        if "--probe" in sys.argv:
            try:
                check_cloud_allowed("probe")
                forbidden = False
            except CloudForbiddenError:
                forbidden = True
            out = {"cloud_forbidden": forbidden,
                   "secrets": [k for k in SECRET_PROBES if k in os.environ]}  # fmt: skip
        else:
            from oncofiles.curated.reocr import reread

            header = json.loads(sys.stdin.buffer.readline())
            content = sys.stdin.buffer.read()
            rr = reread(header["name"], content, header.get("mime") or "",
                        passes=int(header.get("passes", 2)),
                        parallel=int(header.get("parallel", 1)),
                        doc_ref=header.get("doc_ref"))  # fmt: skip
            out = {**asdict(rr), "peak_rss_mb": round(_peak_rss_mb(), 1)}
    sys.stdout.write(json.dumps(out, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
