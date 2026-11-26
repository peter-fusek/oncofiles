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
* ``metadata_unverified``: the ``verify_metadata`` codes (written to the
  sidecar under ``CURATED_CLASSIFY_PROMPT=v2``; under v1 computed for the
  comparison only).

The persisted row also keeps ``per_document`` (ids and codes only) so two
scorecards can be diffed per document (``diff_documents``, exposed as
``per_document_delta`` by ``compare``). Adding it changed no total, so
``SCORECARD_VERSION`` stays: a bump would only withhold the delta against
every earlier baseline although their numbers mean the same.
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
INFO_FIELDS = ("prompt_variant", "prompt_version", "sampling")
# another model (or reasoning effort) is another definition: the aggregate
# delta is withheld, the per-document delta is still given, labelled
# ``across_definitions`` (scorecard model override). ``reasoning_effort`` is
# recorded only when a job overrode it, so earlier baselines stay comparable.
DEFINITION_FIELDS = ("scorecard_version", "model", "reasoning_effort")
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


def request_settings(sampling: str | None = None, prompt_variant: str | None = None) -> dict:
    """How ``propose`` asks the model. ``v1`` (``CURATED_CLASSIFY_PROMPT``
    default): ``legacy`` = temperature 0, the model's default reasoning,
    ``/no_think`` in the system prompt (what step 1 recorded). ``v2`` (#926
    step 2): reasoning explicitly on, the sampling mode of
    ``classify.sampling_mode`` (``thinking`` | ``greedy``)."""
    variant = classify.prompt_variant(prompt_variant)
    if variant == "v2":
        return {"prompt_variant": variant, "sampling": classify.sampling_mode(sampling),
                "thinking": "on"}  # fmt: skip
    return {"prompt_variant": variant, "sampling": "legacy", "thinking": "default"}


def prompt_version(prompt_variant: str | None = None) -> str:
    """The prompt hash of the effective (or given) variant
    (``classify.prompt_version``: the default request keeps ``PROMPT_VERSION``)."""
    version = getattr(classify, "prompt_version", None)
    if callable(version):
        return version(prompt_variant)
    seed = f"{classify.SYSTEM_TEMPLATE}\n{classify.MAX_TEXT_CHARS}"
    return hashlib.sha256(seed.encode()).hexdigest()[:16]


def run_once(
    contract, text: str, filed_name: str, *, prompt_variant: str | None = None,
    sampling: str | None = None, auto_vocab: bool = False, model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:  # fmt: skip
    """One production ``propose`` (the request a curated run sends; only
    ``prompt_variant``, under v2 ``sampling``, and ``model`` /
    ``reasoning_effort`` may override it for a scorecard job, passed
    explicitly) + ``validate`` on a stored text (blocking; one model slot).
    Returns counts and codes only."""
    stats: dict = {}
    t0 = time.monotonic()
    out: dict = {"seconds": None, "error_kind": None, "truncated": False}
    try:
        overrides = {
            k: v
            for k, v in (("model", model), ("reasoning_effort", reasoning_effort))
            if v is not None
        }
        raw = classify.propose(
            contract, text, variant=prompt_variant, sampling=sampling, stats=stats, **overrides
        )  # fmt: skip
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
    iso = naming.iso_of(fields.date) if fields else None  # a day, or a month (#919)
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


def versions(
    *, sampling: str, prompt_variant: str | None = None, model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:  # fmt: skip
    """``model`` is the override when given (full id), else ``LOCAL_LLM_MODEL``
    as before; ``reasoning_effort`` is present only when overridden."""
    out = {
        "scorecard_version": SCORECARD_VERSION,
        "prompt_variant": classify.prompt_variant(prompt_variant),
        "prompt_version": prompt_version(prompt_variant),
        "sampling": sampling,
        "model": model if model is not None else llm.model_id()[:64],
    }
    if reasoning_effort is not None:
        out["reasoning_effort"] = reasoning_effort
    return out


# ── per-document rows (#926 / #942 infra follow-up) ─────────────────────
# The persisted report keeps, per document, only its id, its category code and
# per run the review flag, the review reason codes and the agreement code per
# field, so two scorecards can be diffed document by document. Never a
# filename, a date, a value or any text.

AGREE_CODES = ("agree", "differ", "missing", "not_comparable")


def _code(value) -> str | None:
    return value if isinstance(value, str) and _CODE.fullmatch(value) else None


def _doc_row(c: dict) -> dict | None:
    try:
        doc_id = int(c.get("document_id"))
    except (TypeError, ValueError):
        return None
    row: dict = {"document_id": doc_id, "category": _code(c.get("category")),
                 "result": "skipped" if c.get("result") == "skipped" else "classified"}  # fmt: skip
    if row["result"] == "skipped":
        row["reason"] = _code(c.get("reason")) or "other"
    runs = []
    for r in c.get("runs") or []:
        agree = r.get("agree") if isinstance(r.get("agree"), dict) else {}
        runs.append({
            "review": bool(r.get("review")),
            "reasons": sorted({code for code in (_code(x) for x in r.get("reasons") or [])
                               if code}),
            "agree": {f: agree.get(f) if agree.get(f) in AGREE_CODES else "missing"
                      for f in AGREEMENT_FIELDS},
        })  # fmt: skip
    row["runs"] = runs
    return row


def per_document(cards: list[dict]) -> list[dict]:
    """The compact per-document rows of the cards (ids and codes only),
    ordered by document id."""
    rows = [row for row in (_doc_row(c) for c in cards or [] if isinstance(c, dict)) if row]
    return sorted(rows, key=lambda r: r["document_id"])


def _review_state(runs: list[dict]) -> str:
    flags = {bool(r.get("review")) for r in runs}
    return "mixed" if len(flags) > 1 else ("review" if flags == {True} else "no_review")


def _agree_state(runs: list[dict], field: str) -> str:
    codes = {(r.get("agree") or {}).get(field, "missing") for r in runs}
    return codes.pop() if len(codes) == 1 else "mixed"


def diff_documents(baseline_rows, current_rows) -> dict:
    """Per-document diff of two persisted ``per_document`` lists (pure).

    A document counts only when it was classified (not skipped) in both. Its
    state per scorecard: review = ``review`` / ``no_review`` / ``mixed`` (runs
    disagreed); doc type agreement = the one code of all runs, else
    ``mixed``. A flip is a state change. Counts and document ids only."""
    if not isinstance(baseline_rows, list) or not isinstance(current_rows, list):
        return {"comparable": False, "reason": "per_document_missing"}

    def index(rows):
        return {r["document_id"]: r for r in rows
                if isinstance(r, dict) and isinstance(r.get("document_id"), int)}  # fmt: skip

    old, new = index(baseline_rows), index(current_rows)
    both = sorted(i for i in set(old) & set(new)
                  if old[i].get("result") == new[i].get("result") == "classified"
                  and old[i].get("runs") and new[i].get("runs"))  # fmt: skip
    review = {"flipped": 0, "to_review": 0, "to_no_review": 0, "document_ids": []}
    doc_type = {"flipped": 0, "to_agree": 0, "from_agree": 0, "document_ids": []}
    for i in both:
        a, b = _review_state(old[i]["runs"]), _review_state(new[i]["runs"])
        if a != b:
            review["flipped"] += 1
            review["document_ids"].append(i)
            if a == "no_review":
                review["to_review"] += 1
            elif b == "no_review":
                review["to_no_review"] += 1
        a, b = _agree_state(old[i]["runs"], "doc_type"), _agree_state(new[i]["runs"], "doc_type")
        if a != b:
            doc_type["flipped"] += 1
            doc_type["document_ids"].append(i)
            if b == "agree":
                doc_type["to_agree"] += 1
            elif a == "agree":
                doc_type["from_agree"] += 1
    return {
        "comparable": True,
        "documents_compared": len(both),
        "only_in_baseline": len(set(old) - set(new)),
        "only_in_current": len(set(new) - set(old)),
        "review": review,
        "doc_type": doc_type,
    }


def compare(
    baseline: dict | None,
    current_versions: dict,
    current_totals: dict,
    current_per_document: list[dict] | None = None,
) -> dict:
    """``delta`` metric by metric against the baseline report. A different
    ``DEFINITION_FIELDS`` value (``scorecard_version``: what the numbers mean;
    ``model`` / ``reasoning_effort``: another model is another definition)
    withholds it (``baseline_comparable: false``); a different prompt or
    sampling is exactly what a before/after measures and is only listed in
    ``definition_changed``. With the current per-document rows,
    ``per_document_delta`` diffs them against the baseline's
    (``diff_documents``; a baseline persisted before the rows existed gives
    ``comparable: false``) — also when the aggregate delta is withheld, then
    labelled ``across_definitions: true`` (per document flips between two
    models are what a model evaluation compares)."""
    if not baseline:
        return {"baseline_comparable": None, "definition_changed": {}, "delta": None,
                "per_document_delta": None}  # fmt: skip
    old = baseline.get("versions") if isinstance(baseline.get("versions"), dict) else {}
    changed = {k: [old.get(k), current_versions.get(k)]
               for k in (*DEFINITION_FIELDS, *INFO_FIELDS)
               if old.get(k) != current_versions.get(k)}  # fmt: skip
    per_doc = (
        diff_documents(baseline.get("per_document"), current_per_document)
        if current_per_document is not None
        else None
    )
    if any(k in changed for k in DEFINITION_FIELDS):
        if per_doc is not None:
            per_doc = {**per_doc, "across_definitions": True}
        return {"baseline_comparable": False, "definition_changed": changed, "delta": None,
                "per_document_delta": per_doc}  # fmt: skip
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
        "per_document_delta": per_doc,
    }


def persisted(report: dict) -> dict:
    """What goes into ``curated_runs.report_json``: kind, label, settings,
    versions, the totals and the compact ``per_document`` rows (document id,
    category code, per run review flag / reason codes / agreement codes).
    No full card, no filename, no date, no value, no text."""
    keys = ("kind", "scorecard", "label", "settings", "versions", "totals", "dry_run")
    out = {k: report[k] for k in keys if k in report}
    if "documents" in report:
        out["per_document"] = per_document(report.get("documents") or [])
    return out
