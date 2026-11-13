"""Extraction scorecard (#773): one dry run over a fixed page set, counts only.

Every extraction change (prompt, ``fact_codes`` data, sampling, split,
parallelism) is measured on the same documents before it ships. The job in
``tools/curated.py`` runs the existing extractors and verifiers
(``_clinical_document`` / ``_lab_shadow_document``); this module only turns
their per-document reports into a scorecard and compares two scorecards.

Everything here is pure and keeps **numbers and codes only**: a card is built
from an allow-list of keys, every string is a short code (``[a-z0-9_:.-]``),
and nothing from a page, an answer or an evidence passage is ever copied —
not even the masked answer shape of the page diagnostics.

Metrics (per page, per document and in total):

* stability: % ``finish=stop``, timeouts, truncations, pages not assessed,
  % pages with ``reasoning_tokens`` > ``REASONING_TOKENS_MAX`` (≈0 expected
  with thinking off);
* speed: p50 / p95 seconds and completion tokens per page (nearest rank);
* trust: items machine / unverified / dropped per kind, unverified and drop
  reason codes, % machine;
* lab agreement against the stored rule facts: agree / value_mismatch /
  rule_only / llm_only (+ ``parser_crosscheck`` on transcription pages).
  ``comparable`` = agree + value_mismatch (same code and day on both sides);
  ``pct_agree`` = agree / comparable;
* dates (context-loss check of the page split, #773): items without a
  printed date / items, on split vs unsplit pages (``date_null_rate_*``).
"""

from __future__ import annotations

import math
import re

SCORECARD_VERSION = "scorecard-v1"
REASONING_TOKENS_MAX = 50
LAB_KIND = "lab"

_CODE = re.compile(r"[A-Za-z0-9_:.\-]{1,64}")
_PAGE_NUMBERS = ("seconds", "prompt_tokens", "completion_tokens", "reasoning_tokens", "items",
                 "date_null_items")  # fmt: skip
_PAGE_CODES = ("finish_reason", "error_kind", "answer_source")
_LAB_COUNTS = (
    "rule_facts", "llm_facts", "agree", "agree_machine", "value_mismatch", "rule_only",
    "llm_only", "parser_crosscheck",
)  # fmt: skip
# higher is better / lower is better: a move the wrong way is a regression
_HIGHER_IS_BETTER = ("stability.pct_stop", "trust.pct_machine", "lab.pct_agree")
_LOWER_IS_BETTER = (
    "stability.timeouts", "stability.truncations", "stability.page_errors",
    "stability.pages_not_assessed", "stability.pct_reasoning_over_max",
)  # fmt: skip


def _code(value) -> str | None:
    """A short code (finish reason, error kind, reason, category) or None."""
    if isinstance(value, str) and _CODE.fullmatch(value):
        return value
    return None


def _num(value):
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _error_code(err) -> str | None:
    """``"no page text"`` → ``no_page_text``; an exception class stays as is."""
    if not err:
        return None
    return _code(str(err).replace(" ", "_")) or "error"


def _code_counts(d) -> dict[str, int]:
    """A {code: count} map; anything that is not a code and an int is dropped."""
    out: dict[str, int] = {}
    for k, v in (d or {}).items() if isinstance(d, dict) else ():
        if _code(k) and isinstance(v, int) and not isinstance(v, bool):
            out[k] = out.get(k, 0) + v
    return dict(sorted(out.items()))


def page_card(diag: dict) -> dict:
    """finish / error kind / seconds / tokens of one page — never its text."""
    diag = diag if isinstance(diag, dict) else {}
    out = {k: _num(diag.get(k)) for k in _PAGE_NUMBERS}
    out.update({k: _code(diag.get(k)) for k in _PAGE_CODES})
    out["split"] = diag.get("split") is True
    return out


def _pages(entry: dict) -> dict[str, dict]:
    diags = entry.get("page_diagnostics") or {}
    return {str(n): page_card(d) for n, d in diags.items() if str(n).isdigit()}


def _base(entry: dict, kind: str) -> dict:
    return {
        "document_id": _num(entry.get("document_id")),
        "extractor": kind,
        "category": _code(entry.get("category")),
        "result": _code(entry.get("result")),
        "error": _error_code(entry.get("error")),
        "pages": _num(entry.get("pages")) or 0,
        "pages_not_assessed": len(entry.get("pages_not_assessed") or []),
        "page": _pages(entry),
        "seconds": _num(entry.get("seconds")),
    }


def clinical_card(entry: dict) -> dict:
    """A clinical (#726) document entry → counts per kind, reasons, pages."""
    card = _base(entry, "clinical")
    items: dict[str, dict[str, int]] = {}
    for kind, c in (entry.get("counts") or {}).items():
        if _code(kind) and isinstance(c, dict):
            items[kind] = {"machine": int(c.get("machine") or 0),
                           "unverified": int(c.get("unverified") or 0), "dropped": 0}  # fmt: skip
    drops = _code_counts(entry.get("dropped"))
    for key, n in drops.items():  # "<kind>:<why>"
        kind = key.split(":", 1)[0]
        items.setdefault(kind, {"machine": 0, "unverified": 0, "dropped": 0})["dropped"] += n
    card.update(items=items, unverified_reasons=_code_counts(entry.get("unverified_reasons")),
                dropped_reasons=drops)  # fmt: skip
    return card


def lab_card(entry: dict) -> dict:
    """A lab shadow (#714) document entry → counts, reasons, agreement, pages."""
    card = _base(entry, "lab")
    drops = _code_counts(entry.get("dropped"))
    card["items"] = {
        LAB_KIND: {
            "machine": int(_num(entry.get("llm_machine")) or 0),
            "unverified": int(_num(entry.get("llm_unverified")) or 0),
            "dropped": sum(drops.values()),
        }
    } if entry.get("result") == "compared" else {}  # fmt: skip
    card["unverified_reasons"] = _code_counts(entry.get("unverified_reasons"))
    card["dropped_reasons"] = drops
    card["lab"] = {k: int(_num(entry.get(k)) or 0) for k in _LAB_COUNTS}
    return card


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in (0, 1]); None for no values."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    return vals[max(0, math.ceil(q * len(vals)) - 1)]


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _dates(pages: list[dict]) -> dict:
    """Items without a date / items, split vs unsplit pages (#773). Pages not
    assessed carry no items, so they never dilute either rate."""
    out: dict = {}
    for name, flag in (("split", True), ("unsplit", False)):
        sel = [p for p in pages if bool(p.get("split")) is flag]
        items = sum(p.get("items") or 0 for p in sel)
        null = sum(p.get("date_null_items") or 0 for p in sel)
        out.update({f"pages_{name}": len(sel), f"items_{name}": items,
                    f"date_null_items_{name}": null,
                    f"date_null_rate_{name}": _rate(null, items)})  # fmt: skip
    return out


def totals(cards: list[dict], *, requested: int = 0, missing: int = 0) -> dict:
    """Sums over the document cards — counts, percentages and codes only."""
    pages = [p for c in cards for p in (c.get("page") or {}).values()]
    n = len(pages)
    stop = sum(1 for p in pages if p["finish_reason"] == "stop")
    over = sum(1 for p in pages if (p["reasoning_tokens"] or 0) > REASONING_TOKENS_MAX)
    seconds = [p["seconds"] for p in pages if p["seconds"] is not None]
    tokens = [p["completion_tokens"] for p in pages if p["completion_tokens"] is not None]
    by_kind: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    drops: dict[str, int] = {}
    for c in cards:
        for kind, k in (c.get("items") or {}).items():
            tot = by_kind.setdefault(kind, {"machine": 0, "unverified": 0, "dropped": 0})
            for key in tot:
                tot[key] += k.get(key, 0)
        for r, v in (c.get("unverified_reasons") or {}).items():
            reasons[r] = reasons.get(r, 0) + v
        for r, v in (c.get("dropped_reasons") or {}).items():
            drops[r] = drops.get(r, 0) + v
    machine = sum(k["machine"] for k in by_kind.values())
    unverified = sum(k["unverified"] for k in by_kind.values())
    labs = [
        c["lab"] for c in cards if c.get("extractor") == "lab" and c.get("result") == "compared"
    ]
    lab = {k: sum(x[k] for x in labs) for k in _LAB_COUNTS}
    lab["documents"] = len(labs)
    lab["comparable"] = lab["agree"] + lab["value_mismatch"]
    lab["pct_agree"] = _pct(lab["agree"], lab["comparable"])
    results: dict[str, int] = {}
    for c in cards:
        key = c.get("result") or "unknown"
        results[key] = results.get(key, 0) + 1
    return {
        "documents": {
            "requested": requested,
            "missing": missing,
            "processed": len(cards),
            "results": dict(sorted(results.items())),
        },  # fmt: skip
        "stability": {
            "pages": n,
            "finish_stop": stop,
            "pct_stop": _pct(stop, n),
            "timeouts": sum(1 for p in pages if p["error_kind"] == "timeout"),
            "truncations": sum(
                1 for p in pages if p["finish_reason"] == "length" or p["error_kind"] == "truncated"
            ),
            "page_errors": sum(1 for p in pages if p["error_kind"] is not None),
            "pages_not_assessed": sum(c.get("pages_not_assessed") or 0 for c in cards),
            "pages_reasoning_over_max": over,
            "pct_reasoning_over_max": _pct(over, n),
            "reasoning_tokens_max": REASONING_TOKENS_MAX,
            "answers_from_reasoning": sum(1 for p in pages if p["answer_source"] == "reasoning"),
        },
        "speed": {
            "seconds_p50": percentile(seconds, 0.5),
            "seconds_p95": percentile(seconds, 0.95),
            "seconds_total": round(sum(seconds), 1),
            "completion_tokens_p50": percentile(tokens, 0.5),
            "completion_tokens_p95": percentile(tokens, 0.95),
        },
        "trust": {
            "items": machine + unverified,
            "machine": machine,
            "unverified": unverified,
            "dropped": sum(k["dropped"] for k in by_kind.values()),
            "pct_machine": _pct(machine, machine + unverified),
            "by_kind": dict(sorted(by_kind.items())),
            "unverified_reasons": dict(sorted(reasons.items())),
            "dropped_reasons": dict(sorted(drops.items())),
        },
        "lab": lab,
        "dates": _dates(pages),
    }


def flatten(tot: dict, prefix: str = "") -> dict[str, float | None]:
    """``{"stability": {"pct_stop": 100}}`` → ``{"stability.pct_stop": 100}``."""
    out: dict[str, float | None] = {}
    for k, v in (tot or {}).items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, f"{key}."))
        elif v is None or _num(v) is not None:
            out[key] = v
    return out


def delta(previous: dict | None, current: dict) -> dict | None:
    """Metric by metric: previous, current and change (None when either side
    has no value). ``regressions`` lists the metrics that moved the wrong way
    (stability, % machine, lab agreement): the #773 ship rule."""
    if not previous:
        return None
    prev, cur = flatten(previous), flatten(current)
    metrics = {}
    for key in sorted(set(prev) | set(cur)):
        a, b = prev.get(key), cur.get(key)
        if key.startswith(("trust.unverified_reasons.", "trust.dropped_reasons.")):
            a, b = a or 0, b or 0  # a reason absent on one side counts 0
        change = round(b - a, 3) if a is not None and b is not None else None
        metrics[key] = {"previous": a, "current": b, "change": change}
    regressions = [
        k for k in _HIGHER_IS_BETTER if (metrics.get(k) or {}).get("change") not in (None, 0)
        and metrics[k]["change"] < 0
    ] + [
        k for k in _LOWER_IS_BETTER if (metrics.get(k) or {}).get("change") not in (None, 0)
        and metrics[k]["change"] > 0
    ]  # fmt: skip
    return {"metrics": metrics, "regressions": regressions}


def persisted(report: dict) -> dict:
    """What goes into ``curated_runs.report_json``: label, settings, versions
    and the aggregated totals. No per-document card, no value, no text."""
    keys = ("scorecard", "label", "settings", "extractors", "totals", "dry_run")
    return {k: report[k] for k in keys if k in report}
