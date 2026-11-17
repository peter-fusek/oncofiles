"""Operator CLI: call operator MCP tools on the RUNNING server (#887).

Operator tools (curated runs, re-OCR, re-read, extraction, backfills,
audits, repairs, token admin …) are not on the public ``/mcp`` endpoint.
The running server serves them on ``/ops/mcp`` behind the static operator
bearer. This CLI is a thin MCP client for that endpoint, meant to run
inside the app container, e.g.::

    docker exec -it <app container> python -m oncofiles.ops list
    docker exec -it <app container> python -m oncofiles.ops curated_reocr \\
        --patient <slug> --json '{"dry_run": true, "limit": 2}'
    docker exec -it <app container> python -m oncofiles.ops --status reocr --patient <slug>

It deliberately does NOT open the database or run tool code in its own
process: job registries, run locks and patient locks live in the server
process memory, and a second process would race with running curated jobs.
Every call goes through the server, so the same admin checks, audit rows
(``activity_log``, hashed caller) and rate limits apply as for any MCP call.

Configuration (environment of the container; nothing is passed on the
command line, so the token never shows up in ``ps`` or shell history):

* ``MCP_BEARER_TOKEN`` — the static operator bearer (required).
* ``MCP_PORT`` / ``PORT`` — the server port on ``127.0.0.1`` (default 8000).
* ``ONCOFILES_OPS_URL`` — full endpoint URL, overrides the two above.

Output: the tool's own JSON text on stdout (the curated tools already answer
in counts and codes), errors on stderr. Exit code 0 on success, 1 when the
tool reported an error, 2 on a usage / connection / auth problem. The CLI
never prints the token and never logs the arguments (they may name
documents or carry patient text).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

OPS_PATH = "/ops/mcp"

# --status <kind>: which status tool answers for which job kind.
_STATUS_TOOL_BY_KIND = {"run": "curated_folder_status"}
_DEFAULT_STATUS_TOOL = "curated_reocr_status"  # reocr, reread, llm_shadow, clinical_*, compare…


def default_url() -> str:
    url = os.environ.get("ONCOFILES_OPS_URL", "").strip()
    if url:
        return url
    port = os.environ.get("MCP_PORT") or os.environ.get("PORT") or "8000"
    return f"http://127.0.0.1:{int(port)}{OPS_PATH}"


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m oncofiles.ops",
        description="Call an operator tool on the running Oncofiles server (/ops/mcp).",
    )
    p.add_argument("tool", nargs="?", help="tool name, or 'list' to list the served tools")
    p.add_argument("--json", dest="json_args", default=None, help="tool arguments as JSON object")
    p.add_argument("--patient", default=None, help="patient_slug (added to the arguments)")
    p.add_argument(
        "--status",
        metavar="KIND",
        default=None,
        help="poll the status of the patient's last job of KIND "
        "(run → curated_folder_status; reocr/reread/llm_shadow/clinical_*… → "
        "curated_reocr_status); needs --patient",
    )
    p.add_argument("--url", default=None, help=f"endpoint URL (default {default_url()})")
    p.add_argument("--timeout", type=float, default=120.0, help="seconds per request")
    return p


def _parse_args_json(raw: str | None) -> dict[str, Any]:
    if raw is None or raw.strip() == "":
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("--json must be a JSON object")
    return value


def _result_text(result) -> str:
    parts = [getattr(c, "text", None) for c in (getattr(result, "content", None) or [])]
    text = "\n".join(p for p in parts if p)
    if text:
        return text
    data = getattr(result, "structured_content", None)
    return json.dumps(data, ensure_ascii=False, default=str) if data is not None else ""


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status behind a (possibly wrapped / grouped) client error."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        status = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        stack.extend(x for x in (e.__cause__, e.__context__) if x is not None)
        stack.extend(getattr(e, "exceptions", None) or [])
    return None


async def run(argv: list[str] | None = None, *, out=None, err=None) -> int:
    """Entry point usable in-process (tests) and from ``main()``."""
    out = out or sys.stdout
    err = err or sys.stderr
    args = _parser().parse_args(argv)

    token = os.environ.get("MCP_BEARER_TOKEN", "")
    if not token:
        print("error: MCP_BEARER_TOKEN is not set in this environment", file=err)
        return 2

    try:
        arguments = _parse_args_json(args.json_args)
    except ValueError as exc:
        print(f"error: invalid --json ({exc})", file=err)
        return 2
    if args.patient:
        arguments["patient_slug"] = args.patient

    if args.status:
        if not args.patient:
            print("error: --status needs --patient", file=err)
            return 2
        tool = _STATUS_TOOL_BY_KIND.get(args.status, _DEFAULT_STATUS_TOOL)
        arguments = {"patient_slug": args.patient}
    elif not args.tool:
        print("error: give a tool name, 'list' or --status KIND", file=err)
        return 2
    else:
        tool = args.tool

    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(
        args.url or default_url(),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        async with Client(transport, timeout=args.timeout) as client:
            if tool == "list" and not args.status:
                from oncofiles.tool_surface import OPERATOR_TOOLS

                for t in sorted(await client.list_tools(), key=lambda t: t.name):
                    surface = "operator" if t.name in OPERATOR_TOOLS else "user"
                    print(f"{t.name}\t{surface}", file=out)
                return 0
            result = await client.call_tool(tool, arguments, raise_on_error=False)
    except Exception as exc:  # connection refused, 401, protocol errors
        status = _http_status(exc)
        detail = f" (HTTP {status})" if status else ""
        print(f"error: request failed: {type(exc).__name__}{detail}", file=err)
        return 2

    text = _result_text(result)
    if getattr(result, "is_error", False):
        print(text or "error: the tool reported an error", file=err)
        return 1
    if args.status and text:
        try:
            kind = (json.loads(text) or {}).get("kind")
        except (ValueError, AttributeError):
            kind = None
        if kind and args.status not in _STATUS_TOOL_BY_KIND and kind != args.status:
            print(f"note: the patient's last job is of kind {kind!r}", file=err)
    print(text, file=out)
    return 0


def main() -> None:
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
