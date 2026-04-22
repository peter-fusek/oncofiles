#!/usr/bin/env python3
"""Dashboard load simulator — verification for #469.

Hits the three critical dashboard endpoints (/status, /api/documents,
/api/prompt-log) in two modes:

  parallel  — N clients fire the 3 endpoints concurrently (asyncio.gather
              at the CLIENT level only, never inside the server). Reports
              latency distribution + error rate across all requests.

  prolonged — one client fires the 3 endpoints sequentially every K seconds
              for a duration, polling /readiness in between to observe
              breaker state changes. Catches stale-stream issues that only
              surface after minutes of idle.

Emits CSV to stdout (or --out path): t_iso, endpoint, status, latency_ms,
breaker_state, trip_count_total, retry_after.

Usage examples:

    # Quick parallel check against prod (3 concurrent clients, 10 iterations)
    uv run python scripts/dashboard_load_sim.py parallel \\
        --host https://oncofiles.com \\
        --token "$MCP_BEARER_TOKEN" \\
        --clients 3 --iterations 10

    # Prolonged observation against local server (15 min, 1 tick / 10s)
    MCP_BEARER_TOKEN=test uv run oncofiles-mcp &
    uv run python scripts/dashboard_load_sim.py prolonged \\
        --host http://localhost:8080 --token test \\
        --duration 900 --interval 10 --out /tmp/sim.csv

CSV columns:
  t_iso              — ISO UTC timestamp of the request completion
  endpoint           — '/status' / '/api/documents' / '/api/prompt-log' / '/readiness'
  status             — HTTP status code or 'CONNECT_ERROR'
  latency_ms         — wall-clock latency in milliseconds
  breaker_state      — breaker state at time of request (from /readiness; '-' if unknown)
  trip_count_total   — lifetime trip count (from /readiness; '-' if unknown)
  retry_after        — Retry-After header value if present, else '-'

The intended signal:
- 200s across the board, latency envelope consistent with serialized Turso
  access → healthy behavior
- 503 with Retry-After: 30 → breaker is doing its job + dashboard endpoints
  honor the #412 contract (Phase 1 of #469)
- 500s → regression; report to #469 immediately
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover - runtime diagnostic
    sys.stderr.write("httpx is required: uv run python scripts/dashboard_load_sim.py ...\n")
    sys.exit(2)


CRITICAL_ENDPOINTS = ("/status", "/api/documents", "/api/prompt-log")


async def _readiness_snapshot(client: httpx.AsyncClient, host: str) -> tuple[str, str]:
    """Best-effort fetch of (breaker_state, trip_count_total) from /readiness.

    Returns ('-', '-') on any failure — this is observability, not control
    flow. We never block the load test on /readiness itself being healthy.
    """
    try:
        r = await client.get(host + "/readiness", timeout=5.0)
        if r.status_code != 200:
            return ("-", "-")
        payload = r.json()
        cb = payload.get("circuit_breaker") or {}
        return (str(cb.get("state", "-")), str(cb.get("trip_count_total", "-")))
    except Exception:
        return ("-", "-")


async def _hit(
    client: httpx.AsyncClient,
    host: str,
    endpoint: str,
    headers: dict,
) -> dict:
    t0 = asyncio.get_event_loop().time()
    try:
        r = await client.get(host + endpoint, headers=headers, timeout=30.0)
        t1 = asyncio.get_event_loop().time()
        return {
            "t_iso": datetime.now(UTC).isoformat(),
            "endpoint": endpoint,
            "status": r.status_code,
            "latency_ms": round((t1 - t0) * 1000, 1),
            "retry_after": r.headers.get("Retry-After", "-"),
        }
    except Exception as exc:
        t1 = asyncio.get_event_loop().time()
        return {
            "t_iso": datetime.now(UTC).isoformat(),
            "endpoint": endpoint,
            "status": f"CONNECT_ERROR({type(exc).__name__})",
            "latency_ms": round((t1 - t0) * 1000, 1),
            "retry_after": "-",
        }


async def run_parallel(
    host: str,
    token: str,
    clients: int,
    iterations: int,
    writer: csv.DictWriter,
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient() as http:
        for it in range(iterations):
            # Grab breaker state before the burst
            state, trips = await _readiness_snapshot(http, host)

            tasks = [
                _hit(http, host, ep, headers) for _ in range(clients) for ep in CRITICAL_ENDPOINTS
            ]
            results = await asyncio.gather(*tasks)
            for row in results:
                row["breaker_state"] = state
                row["trip_count_total"] = trips
                writer.writerow(row)
            sys.stderr.write(
                f"[parallel iter {it + 1}/{iterations}] "
                f"{len(results)} requests — "
                f"200s={sum(1 for r in results if r['status'] == 200)}, "
                f"503s={sum(1 for r in results if r['status'] == 503)}, "
                f"500s={sum(1 for r in results if r['status'] == 500)}, "
                f"breaker={state}\n"
            )


async def run_prolonged(
    host: str,
    token: str,
    duration_s: int,
    interval_s: int,
    writer: csv.DictWriter,
) -> None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = asyncio.get_event_loop().time() + duration_s
    tick = 0
    async with httpx.AsyncClient() as http:
        while asyncio.get_event_loop().time() < deadline:
            state, trips = await _readiness_snapshot(http, host)
            for ep in CRITICAL_ENDPOINTS:
                row = await _hit(http, host, ep, headers)
                row["breaker_state"] = state
                row["trip_count_total"] = trips
                writer.writerow(row)
            tick += 1
            if tick % 6 == 0:  # emit a progress line every minute-ish
                sys.stderr.write(f"[prolonged tick {tick}] breaker={state} trips={trips}\n")
            await asyncio.sleep(interval_s)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)

    p = sub.add_parser("parallel", help="Concurrent burst")
    p.add_argument("--host", default=os.environ.get("HOST", "http://localhost:8080"))
    p.add_argument("--token", default=os.environ.get("MCP_BEARER_TOKEN", ""))
    p.add_argument("--clients", type=int, default=3)
    p.add_argument("--iterations", type=int, default=10)
    p.add_argument("--out", type=Path, default=None)

    pl = sub.add_parser("prolonged", help="Steady load for observation")
    pl.add_argument("--host", default=os.environ.get("HOST", "http://localhost:8080"))
    pl.add_argument("--token", default=os.environ.get("MCP_BEARER_TOKEN", ""))
    pl.add_argument("--duration", type=int, default=900, help="seconds (default 15 min)")
    pl.add_argument("--interval", type=int, default=10, help="seconds between ticks")
    pl.add_argument("--out", type=Path, default=None)

    args = ap.parse_args()

    out_stream = args.out.open("w", newline="") if args.out else sys.stdout
    fieldnames = [
        "t_iso",
        "endpoint",
        "status",
        "latency_ms",
        "breaker_state",
        "trip_count_total",
        "retry_after",
    ]
    writer = csv.DictWriter(out_stream, fieldnames=fieldnames)
    writer.writeheader()

    try:
        if args.mode == "parallel":
            asyncio.run(run_parallel(args.host, args.token, args.clients, args.iterations, writer))
        else:
            asyncio.run(run_prolonged(args.host, args.token, args.duration, args.interval, writer))
    finally:
        if args.out:
            out_stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
