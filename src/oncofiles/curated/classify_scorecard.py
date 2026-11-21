"""Classification scorecard (#926): one dry run over stored sidecar texts, counts only.

Every change to the curated classification (prompt, worked examples,
sampling) is measured on the same documents before it ships. The job in
``tools/curated.py`` (``curated_classify_scorecard``) loads each document's
stored sidecar text through ``llm_input.load_for_llm`` and runs the
production ``classify.propose`` + ``classify.validate`` on it (``run_once``)
— no Drive write, no filing, no sidecar, no fact. This module turns those
runs into cards and totals and compares two scorecards.

Everything kept is a **number or a short code**: never a filename, a value
of the answer, a reason text with a date in it (``reason_code`` maps the
review reasons of ``validate`` to codes), or anything of the page.

Metrics:

* review: share of runs that would wait for a person, reason-code histogram;
* agreement of doc type / date / institution with the currently filed name
  (``naming.parse_filename``; a name outside the standard is not comparable);
* stability: truncations (after the one retry), retries, errors;
* speed: p50 / p95 seconds and completion / reasoning tokens per run;
* repeat (``repeat=2``): per document, whether every run gave the same
  (doc type, date, institution, review) outcome;
* ``metadata_unverified``: the ``verify_metadata`` codes the runs would write.
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import date

from oncofiles.curated import classify, llm, naming
from oncofiles.curated.scorecard import flatten, percentile

SCORECARD_VERSION = "classify-scorecard-v1"
KIND = "classification"
# a change of these does not change what the numbers mean, so a delta is
# still given; they are listed in ``definition_changed`` for the reader
INFO_FIELDS = ("prompt_version", "sampling", "model")
AGREEMENT_FIELDS = ("doc_type", "date", "institution")
_HIGHER_IS_BETTER = tuple(f"agreement.{f}.pct_agree" for f in AGREEMENT_FIELDS) + (
    "repeat.pct_stable",
)
_LOWER_IS_BETTER = ("stability.truncations", "stability.errors")

# validate's review reasons (free text, may hold a date or a token) → codes
_REASON_CODES = (
    ("not in contract", "doc_type_unknown"),
    ("no valid clinical date", "no_date"),
    ("birth date", "birth_date"),
    ("in the future", "future_date"),
    ("implausible", "implausible_date"),
    ("does not appear", "date_not_printed"),
    ("years old", "date_too_old"),
    ("institution unknown", "institution_unknown"),
    ("close to README token", "near_vocabulary_token"),
    ("not in README vocabulary", "new_vocabulary"),
    ("several documents", "split_needed"),
    ("model confidence", "low_confidence"),
    ("names another person", "other_person"),
)
_CODE = re.compile(r"[A-Za-z0-9_:.\-]{1,64}")
# The README-standard parser (patient-independent), not filename_parser's
# legacy ``parse_filename(name, patient_id)`` the #733 source sweep looks for.
_parse_curated_name = naming.parse_filename


def reason_code(reason: str) -> str:
    for needle, code in _REASON_CODES:
        if needle in (reason or ""):
            return code
    return "other"


def _today() -> date:
    return date.today()


def _error_kind(exc: BaseException) -> str:
    msg = str(exc)
    if "truncated" in msg:
        return "truncated"
    if "not valid JSON" in msg or "not a JSON object" in msg:
        return "json"
    if "unreachable" in msg:
        return "unreachable"
    if "HTTP" in msg:
        return "http_status"
    return "other"


def request_settings() -> dict:
    """How ``propose`` asks the model today: ``legacy`` = temperature 0, the
    model's default reasoning, ``/no_think`` in the system prompt."""
    return {"sampling": "legacy", "thinking": "default"}


def prompt_version() -> str:
    """``classify.PROMPT_VERSION`` when the classifier defines one, else the
    hash of today's template and text cut (the same formula)."""
    version = getattr(classify, "PROMPT_VERSION", None)
    if isinstance(version, str):
        return version
    seed = f"{classify.SYSTEM_TEMPLATE}\n{classify.MAX_TEXT_CHARS}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def run_once(contract, text: str, filed_name: str, *, auto_vocab: bool = False) -> dict:
    """One production ``propose`` (exactly the request a curated run sends)
    + ``validate`` on a stored text (blocking; one model slot). Returns
    counts and codes only. Token counts and the finish reason stay None
    until ``propose`` reports them."""
    stats: dict = {}
    t0 = time.monotonic()
    out: dict = {"seconds": None, "error_kind": None, "truncated": False}
    try:
        raw = classify.propose(contract, text)
    except llm.LocalLLMError as exc:
        kind = _error_kind(exc)
        out.update(error_kind=kind, truncated=kind == "truncated")
        raw = None
    out["seconds"] = round(time.monotonic() - t0, 1)
    out["retried"] = bool(stats.get("retried"))
    for k in ("completion_tokens", "reasoning_tokens"):
        v = stats.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) else None
    fr = stats.get("finish_reason")
    out["finish_reason"] = fr if isinstance(fr, str) and _CODE.fullmatch(fr) else None
    if raw is None:
        out.update(
            review=True,
            reasons=["classification_failed"],
            outcome=None,
            agree={f: "missing" for f in AGREEMENT_FIELDS},
            metadata_unverified=[],
        )
        return out  # fmt: skip
    cls = classify.validate(contract, raw, text, today=_today(), auto_vocab=auto_vocab)
    fields = cls.fields
    iso = f"{fields.date[:4]}-{fields.date[4:6]}-{fields.date[6:]}" if fields else None
    _, unverified = classify.verify_metadata(raw, text, iso)
    out["review"] = bool(cls.review_needed)
    out["reasons"] = sorted({reason_code(r) for r in cls.reasons})
    out["metadata_unverified"] = unverified
    # the outcome stays in memory only to compare repeats; ``card`` drops it
    out["outcome"] = (cls.doc_type, fields.date if fields else None,
                      fields.institution if fields else None, out["review"])  # fmt: skip
    filed = _parse_curated_name(filed_name or "")
    if filed is None:
        out["agree"] = {f: "not_comparable" for f in AGREEMENT_FIELDS}
    else:
        got = {"doc_type": cls.doc_type, "date": fields.date if fields else None,
               "institution": fields.institution if fields else None}  # fmt: skip
        want = {"doc_type": filed.doc_type, "date": filed.date, "institution": filed.institution}
        out["agree"] = {
            f: "missing" if got[f] is None else ("agree" if got[f] == want[f] else "differ")
            for f in AGREEMENT_FIELDS
        }
    return out


_RUN_KEYS = ("review", "reasons", "agree", "truncated", "retried", "error_kind", "seconds",
             "finish_reason", "completion_tokens", "reasoning_tokens",
             "metadata_unverified")  # fmt: skip


def card(document_id: int, category, runs: list[dict]) -> dict:
    """One document: its runs (codes and counts) and whether they agreed."""
    outcomes = {r.get("outcome") for r in runs}
    return {
        "document_id": int(document_id),
        "category": category if isinstance(category, str) and _CODE.fullmatch(category) else None,
        "result": "classified",
        "runs": [{k: r.get(k) for k in _RUN_KEYS} for r in runs],
        "stable": (len(outcomes) == 1 and None not in outcomes) if len(runs) > 1 else None,
    }


def skipped_card(document_id: int, category, reason: str) -> dict:
    return {
        "document_id": int(document_id),
        "category": category if isinstance(category, str) and _CODE.fullmatch(category) else None,
        "result": "skipped",
        "reason": reason if _CODE.fullmatch(reason or "") else "other",
        "runs": [],
        "stable": None,
    }


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _hist(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))


def totals(cards: list[dict], *, requested: int = 0, missing: int = 0) -> dict:
    """Sums over the cards — counts, percentages and codes only."""
    runs = [r for c in cards for r in c.get("runs") or []]
    n = len(runs)
    review = sum(1 for r in runs if r.get("review"))
    agreement = {}
    for f in AGREEMENT_FIELDS:
        counts = _hist((r.get("agree") or {}).get(f, "missing") for r in runs)
        cmp = sum(counts.get(k, 0) for k in ("agree", "differ", "missing"))
        agreement[f] = {k: counts.get(k, 0) for k in ("agree", "differ", "missing",
                                                       "not_comparable")}  # fmt: skip
        agreement[f]["pct_agree"] = _pct(counts.get("agree", 0), cmp)
    seconds = [r["seconds"] for r in runs if r.get("seconds") is not None]
    tokens = [r["completion_tokens"] for r in runs if r.get("completion_tokens") is not None]
    reasoning = [r["reasoning_tokens"] for r in runs if r.get("reasoning_tokens") is not None]
    repeated = [c for c in cards if c.get("stable") is not None]
    stable = sum(1 for c in repeated if c["stable"])
    return {
        "documents": {
            "requested": requested,
            "missing": missing,
            "processed": len(cards),
            "results": _hist(c.get("result") or "unknown" for c in cards),
            "skipped_reasons": _hist(c["reason"] for c in cards if c.get("result") == "skipped"),
        },
        "review": {
            "runs": n,
            "review": review,
            "pct_review": _pct(review, n),
            "reasons": _hist(code for r in runs for code in r.get("reasons") or []),
        },
        "agreement": agreement,
        "stability": {
            "runs": n,
            "truncations": sum(1 for r in runs if r.get("truncated")),
            "retried": sum(1 for r in runs if r.get("retried")),
            "errors": sum(1 for r in runs if r.get("error_kind")),
            "error_kinds": _hist(r["error_kind"] for r in runs if r.get("error_kind")),
            "finish": _hist(r["finish_reason"] for r in runs if r.get("finish_reason")),
        },
        "speed": {
            "seconds_p50": percentile(seconds, 0.5),
            "seconds_p95": percentile(seconds, 0.95),
            "seconds_total": round(sum(seconds), 1),
            "completion_tokens_p50": percentile(tokens, 0.5),
            "completion_tokens_p95": percentile(tokens, 0.95),
            "reasoning_tokens_p50": percentile(reasoning, 0.5),
        },
        "repeat": {
            "documents": len(repeated),
            "stable": stable,
            "pct_stable": _pct(stable, len(repeated)),
        },
        "metadata_unverified": _hist(
            code for r in runs for code in r.get("metadata_unverified") or []
        ),  # fmt: skip
    }


def versions(*, sampling: str) -> dict:
    return {
        "scorecard_version": SCORECARD_VERSION,
        "prompt_version": prompt_version(),
        "sampling": sampling,
        "model": llm.model_id()[:64],
    }


def compare(baseline: dict | None, current_versions: dict, current_totals: dict) -> dict:
    """``delta`` metric by metric against the baseline report. Only a
    different ``scorecard_version`` (what the numbers mean) withholds it; a
    different prompt, sampling or model is exactly what a before/after
    measures and is listed in ``definition_changed``."""
    if not baseline:
        return {"baseline_comparable": None, "definition_changed": {}, "delta": None}
    old = baseline.get("versions") if isinstance(baseline.get("versions"), dict) else {}
    changed = {k: [old.get(k), current_versions.get(k)]
               for k in ("scorecard_version", *INFO_FIELDS)
               if old.get(k) != current_versions.get(k)}  # fmt: skip
    if "scorecard_version" in changed:
        return {"baseline_comparable": False, "definition_changed": changed, "delta": None}
    prev, cur = flatten(baseline.get("totals") or {}), flatten(current_totals)
    metrics = {}
    for key in sorted(set(prev) | set(cur)):
        a, b = prev.get(key), cur.get(key)
        if key.startswith(("review.reasons.", "metadata_unverified.", "stability.error_kinds.")):
            a, b = a or 0, b or 0
        change = round(b - a, 3) if a is not None and b is not None else None
        metrics[key] = {"previous": a, "current": b, "change": change}

    def moved(k, sign):
        c = (metrics.get(k) or {}).get("change")
        return c not in (None, 0) and c * sign < 0

    regressions = [k for k in _HIGHER_IS_BETTER if moved(k, 1)] + [
        k for k in _LOWER_IS_BETTER if moved(k, -1)
    ]
    return {
        "baseline_comparable": True,
        "definition_changed": changed,
        "delta": {"metrics": metrics, "regressions": regressions},
    }


def persisted(report: dict) -> dict:
    """What goes into ``curated_runs.report_json``: kind, label, settings,
    versions and the totals. No card, no document id list, no text."""
    keys = ("kind", "scorecard", "label", "settings", "versions", "totals", "dry_run")
    return {k: report[k] for k in keys if k in report}
