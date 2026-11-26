"""Classify an extracted document against its folder contract (#562).

The local model proposes fields from the document **text** (never the
filename — README §1b lesson 7: auto-generated names here were invented). The
deterministic ``validate`` step then enforces the hard rules so a model error
can never rename a real medical record into a wrong place:

* ``doc_type`` must be one of the contract's DocumentTypes;
* the date must be a real calendar date, not in the future, not before 1900,
  never a birth date from the README, and must literally appear in the text;
  a month-precision date (``YYYY-MM``, #919) must have that month and year
  printed on its own (not as part of a full date), outside a birth context,
  and must not be the README birth month; it is filed as ``YYYYMM00``;
* a suspected multi-document bundle, low confidence, or a missing date /
  type / institution → ``review_needed`` (stays in the inbox, README §6);
* an OCR-misspelled vocabulary token is snapped back to the README spelling,
  but only when it is clearly the *same* entity (#754): edit distance <= 2 and the
  same leading CamelCase word; for Institution / Doctor also the README token is
  printed in the text, or the model's token is not;
* a new Institution / Doctor (not in README §3) → ``review_needed``; a new
  HospitalName / Subject is accepted and *flagged* for the run report.
* with ``auto_vocab`` (#854, ``CURATED_AUTO_VOCAB``, on in the runner) a new
  Institution / Doctor is instead proposed for the README vocabulary when its
  spelling is printed in the text (``Classification.additions``; the runner
  writes it) and an unknown one is only flagged. Real ambiguity — another
  person, no or a birth date, unknown type, split, low confidence, a near
  neighbour of a README token that is a different entity — still → review.

#926: ``propose`` asks with reasoning explicitly on (``thinking=True``: no
``/no_think``, no off-marker) and the Qwen3.8 model-card thinking sampling
(``CURATED_CLASSIFY_SAMPLING`` = ``thinking`` | ``greedy`` rollback); the
prompt carries invented worked examples (``EXAMPLES``, one of them with a
month-precision date). ``verify_metadata``
is the generic check of what the classifier writes into the sidecar besides
the filename fields: ICD codes, doctor surname, department and the date
evidence must be printed in the text, otherwise the field is dropped and
``unverified_<field>`` is listed (never a review reason).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from oncofiles import config
from oncofiles.curated import llm
from oncofiles.curated.contract import Contract
from oncofiles.curated.naming import (
    UNKNOWN,
    NameFields,
    camel,
    doctor_surname,
    has_title,
    month_token,
    subject_token,
    surname_text,
    surname_token,
)
from oncofiles.curated.vocabulary import VocabAddition

logger = logging.getLogger(__name__)

MIN_CONFIDENCE = 0.60
SNAP_CUTOFF = 0.8  # OCR typo tolerance when matching README vocabulary tokens
SNAP_MAX_EDITS = 2  # #754: a bigger "correction" is a different entity, not an OCR typo
MAX_TEXT_CHARS = 24_000

SYSTEM_TEMPLATE = """You file medical documents into a patient folder that is governed by the \
contract below. Read the contract carefully: its vocabularies, classification rules and \
date rules are binding.

===== CONTRACT (README.md) =====
{readme}
===== END CONTRACT =====

Classify ONE document from its extracted TEXT. Never use the original filename. Answer with a \
single JSON object and nothing else:
{{
  "doc_date": "YYYY-MM-DD, or YYYY-MM only when the text prints just a month and year \
(no day), or null: clinical (exam/report/sample) date, never birth/print/scan",
  "date_evidence": "the exact text snippet the date was read from",
  "doc_type": "one of: {doc_types}",
  "institution": "Institution token from the contract, or a new CamelCase token, or Unknown",
  "institution_printed": "the issuing institution's name copied verbatim from the TEXT, or null",
  "doctor": "signing doctor as printed, e.g. 'MUDr. Jana Kovacova', or null",
  "hospital": "HospitalName token from the contract vocabulary, or Unknown",
  "hospital_unit": "department as printed (free text) or null",
  "subject": "optional CamelCase slug <= 30 chars, only if needed to disambiguate, else null",
  "diagnoses": ["ICD-10 codes printed on the document"],
  "language": "sk | cs | en | ...",
  "multiple_documents": "true if the text holds several documents (new letterhead + date)",
  "confidence": 0.0,
  "summary": "one sentence in the document's language"
}}
"""

# Worked examples (#926; project rule: every extraction prompt carries 2-5).
# Everything is INVENTED — people, institutions, doctors and dates (2027+) —
# and the examples assume a contract whose vocabulary holds Institution
# ``Lucnica``, doctor ``Lipova`` and HospitalName ``OnkologickaAmbulancia``
# (``EXAMPLE_VOCABULARY``). ``tests/test_926_classification.py`` runs every
# example through ``validate`` and ``verify_metadata`` and checks the
# outcome stated here. (text, expected answer, what it shows)
EXAMPLE_VOCABULARY = {"Institution": ("Lucnica",), "Doctor": ("Lipova",),
                      "HospitalName": ("OnkologickaAmbulancia",)}  # fmt: skip
EXAMPLES: tuple[tuple[str, dict, str], ...] = (
    (
        "Vzorová nemocnica Lúčnica\nOnkologická ambulancia\nAMBULANTNÁ SPRÁVA\n"
        "Vyšetrená dňa: 14.03.2027\nDg.: C50.4\n"
        "Kontrola po 3. cykle liečby, stav stabilizovaný.\nMUDr. Eva Lipová",
        {
            "doc_date": "2027-03-14",
            "date_evidence": "Vyšetrená dňa: 14.03.2027",
            "doc_type": "Report",
            "institution": "Lucnica",
            "institution_printed": "Vzorová nemocnica Lúčnica",
            "doctor": "MUDr. Eva Lipová",
            "hospital": "OnkologickaAmbulancia",
            "hospital_unit": "Onkologická ambulancia",
            "subject": None,
            "diagnoses": ["C50.4"],
            "language": "sk",
            "multiple_documents": False,
            "confidence": 0.9,
            "summary": "Kontrola po 3. cykle liečby, stav stabilizovaný.",
        },
        "filed: institution, doctor and department are contract tokens; the ICD code, the "
        "doctor and the date evidence are copied from the text",
    ),
    (
        "Laboratórium Brezina s.r.o.\nVÝSLEDKY VYŠETRENIA\nDátum odberu: 02.06.2027\n"
        "Krvný obraz bez odchýlok od referenčných hodnôt.\nVyšetril: MUDr. Tomáš Javor",
        {
            "doc_date": "2027-06-02",
            "date_evidence": "Dátum odberu: 02.06.2027",
            "doc_type": "Labs",
            "institution": "LaboratoriumBrezina",
            "institution_printed": "Laboratórium Brezina",
            "doctor": "MUDr. Tomáš Javor",
            "hospital": "Unknown",
            "hospital_unit": None,
            "subject": None,
            "diagnoses": [],
            "language": "sk",
            "multiple_documents": False,
            "confidence": 0.85,
            "summary": "Krvný obraz bez odchýlok.",
        },
        "a NEW institution and doctor: a new CamelCase token plus the name copied verbatim "
        "into institution_printed; no ICD code is printed, so diagnoses is empty",
    ),
    (
        "Vzorová nemocnica Lúčnica\nKONTROLNÉ VYŠETRENIE\n"
        "Pacientka sledovaná od jari 2027, ďalšia kontrola podľa dohody.\nMUDr. Eva Lipová",
        {
            "doc_date": None,
            "date_evidence": None,
            "doc_type": "Report",
            "institution": "Lucnica",
            "institution_printed": "Vzorová nemocnica Lúčnica",
            "doctor": "MUDr. Eva Lipová",
            "hospital": "Unknown",
            "hospital_unit": None,
            "subject": None,
            "diagnoses": [],
            "language": "sk",
            "multiple_documents": False,
            "confidence": 0.7,
            "summary": "Kontrolné vyšetrenie bez uvedeného dátumu.",
        },
        "review: neither a clinical date nor a month and year is printed, so doc_date and "
        "date_evidence are null (never guess a day or a month)",
    ),
    (
        "Vzorová nemocnica Lúčnica\nAMBULANTNÁ SPRÁVA\nDátum: 05.09.2027\nMUDr. Eva Lipová\n"
        "\nRÁDIOLÓGIA CENTRUM JAVORINA\nUSG brucha\nDátum vyšetrenia: 19.09.2027",
        {
            "doc_date": "2027-09-05",
            "date_evidence": "Dátum: 05.09.2027",
            "doc_type": "Report",
            "institution": "Lucnica",
            "institution_printed": "Vzorová nemocnica Lúčnica",
            "doctor": "MUDr. Eva Lipová",
            "hospital": "Unknown",
            "hospital_unit": None,
            "subject": None,
            "diagnoses": [],
            "language": "sk",
            "multiple_documents": True,
            "confidence": 0.6,
            "summary": "Dve rôzne správy v jednom súbore.",
        },
        "review: a second letterhead with its own date — multiple_documents is true",
    ),
    (
        "Vzorová nemocnica Lúčnica\nPREVENTÍVNA PREHLIADKA\nObdobie: 04/2027\n"
        "Bez nálezu, kontrola o rok.\nMUDr. Eva Lipová",
        {
            "doc_date": "2027-04",
            "date_evidence": "Obdobie: 04/2027",
            "doc_type": "Report",
            "institution": "Lucnica",
            "institution_printed": "Vzorová nemocnica Lúčnica",
            "doctor": "MUDr. Eva Lipová",
            "hospital": "Unknown",
            "hospital_unit": None,
            "subject": None,
            "diagnoses": [],
            "language": "sk",
            "multiple_documents": False,
            "confidence": 0.8,
            "summary": "Preventívna prehliadka bez nálezu.",
        },
        "filed with month precision: only a month and year is printed (no day), so doc_date "
        "is YYYY-MM; never invent a day",
    ),
)


def _examples_block() -> str:
    lines = [
        "",
        "===== WORKED EXAMPLES (invented; NOT this patient, NOT this contract) =====",
        "The tokens Lucnica (Institution), Lipova (doctor) and OnkologickaAmbulancia "
        "(HospitalName) stand for tokens of the contract above; never copy them, use the "
        "contract's own vocabulary.",
    ]
    for n, (text, answer, note) in enumerate(EXAMPLES, 1):
        lines += [f"--- Example {n} ({note}) ---", f"TEXT:\n{text}",
                  "ANSWER:", json.dumps(answer, ensure_ascii=False)]  # fmt: skip
    lines.append("===== END EXAMPLES =====")
    return "\n".join(lines) + "\n"


EXAMPLES_BLOCK = _examples_block()

# Identifies the request ``propose`` builds around the README and the text; any
# edit to the prompt, the examples or the text cut rotates it, so cached
# proposals (#936, ``tools.curated.propose_cache_key``) miss. ``PROMPT_VERSION``
# is the v1 request (template + text cut, the formula of #936/#965);
# ``PROMPT_VERSION_V2`` the #926 request (template + worked examples + cut).
PROMPT_VERSION = hashlib.sha256(f"{SYSTEM_TEMPLATE}\n{MAX_TEXT_CHARS}".encode()).hexdigest()[:16]
PROMPT_VERSION_V2 = hashlib.sha256(
    f"{SYSTEM_TEMPLATE}\n{EXAMPLES_BLOCK}\n{MAX_TEXT_CHARS}".encode()
).hexdigest()[:16]

# CURATED_CLASSIFY_PROMPT (#926, merged switched off): ``v1`` (default) = the
# request before #926 (template only, the model's default reasoning,
# temperature 0, ``/no_think`` in the system prompt); ``v2`` = reasoning on,
# the sampling profile of ``sampling_mode``, the worked examples.
PROMPT_VARIANTS = ("v1", "v2")
_warned_variants: set[str] = set()

# CURATED_CLASSIFY_SAMPLING → llm.SAMPLING_PROFILES (#926; v2 only)
SAMPLING_MODES: dict[str, str] = {"thinking": "qwen_thinking", "greedy": "greedy"}
_warned_modes: set[str] = set()


def prompt_variant(override: str | None = None) -> str:
    """The effective classification request: ``override`` (a scorecard job,
    validated by the caller) or ``CURATED_CLASSIFY_PROMPT``; an unknown env
    value is ``v1`` with one warning per value."""
    variant = (override or config.CURATED_CLASSIFY_PROMPT or "v1").strip().lower()
    if variant in PROMPT_VARIANTS:
        return variant
    if variant not in _warned_variants:
        _warned_variants.add(variant)
        logger.warning("CURATED_CLASSIFY_PROMPT=%r unknown: using 'v1'", variant[:20])
    return "v1"


def prompt_version(variant: str | None = None) -> str:
    """The prompt hash of the effective (or the given) variant."""
    return PROMPT_VERSION_V2 if prompt_variant(variant) == "v2" else PROMPT_VERSION


def cache_version() -> str:
    """What the #936 proposal cache keys on: v1 = ``PROMPT_VERSION`` exactly (the
    key is byte-identical to before the switch); v2 = its prompt hash plus the
    sampling mode, so neither a variant switch nor a sampling rollback reuses
    another request's proposals."""
    if prompt_variant() == "v2":
        return f"{PROMPT_VERSION_V2}:{sampling_mode()}"
    return PROMPT_VERSION


def sampling_mode(override: str | None = None) -> str:
    """The effective classification sampling mode (v2 only): ``override`` (a
    scorecard job, validated by the caller) or ``CURATED_CLASSIFY_SAMPLING``;
    an unknown env value is ``thinking`` with one warning per value."""
    mode = (override or config.CURATED_CLASSIFY_SAMPLING or "thinking").strip().lower()
    if mode in SAMPLING_MODES:
        return mode
    if mode not in _warned_modes:
        _warned_modes.add(mode)
        logger.warning("CURATED_CLASSIFY_SAMPLING=%r unknown: using 'thinking'", mode[:20])
    return "thinking"


def system_prompt(contract: Contract, variant: str | None = None) -> str:
    """v1: the template; v2: the template plus the worked examples."""
    system = SYSTEM_TEMPLATE.format(
        readme=contract.readme_text, doc_types=", ".join(sorted(contract.doc_types))
    )
    return system + EXAMPLES_BLOCK if prompt_variant(variant) == "v2" else system


@dataclass
class Classification:
    fields: NameFields | None
    doc_type: str
    raw: dict
    review_needed: bool
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # #854: vocabulary the runner adds to README §3 before filing (auto_vocab only)
    additions: list[VocabAddition] = field(default_factory=list)


_DATE_PATTERNS = (
    "{d}.{m}.{y}",
    "{di}.{mi}.{y}",
    "{d}. {m}. {y}",
    "{di}. {mi}. {y}",
    "{y}-{m}-{d}",
    "{d}/{m}/{y}",
    "{d}.{m}.{yy}",
)


def date_in_text(iso: str, text: str) -> bool:
    """True when the ISO date is literally printed in ``text`` in a common SK form."""
    y, m, d = iso.split("-")
    variants = {
        p.format(d=d, m=m, y=y, di=str(int(d)), mi=str(int(m)), yy=y[2:]) for p in _DATE_PATTERNS
    }
    compact = re.sub(r"[ \t]+", " ", text)
    return any(re.search(r"(?<!\d)" + re.escape(v) + r"(?!\d)", compact) for v in variants)


def _month_number(word: str) -> int | None:
    from oncofiles.curated.verifier_clinical import _month_number as month_number

    return month_number(word)


# A day right before a month ("5.3.2027", "5. marca 2027"): then the month is
# part of a full date, not a printed month on its own.
_DAY_BEFORE_RE = re.compile(r"\d{1,2}\s?[./]\s?$|\d{1,2}\.\s*$")


def month_in_text(year_month: str, text: str) -> bool:
    """A month and year (``2027-03``) printed ON ITS OWN in ``text`` (#919):
    ``03/2027``, ``3.2027``, ``2027-03``, ``marec 2027`` / ``brezen 2027``;
    not as part of a full date (``15.03.2027``, ``2027-03-15``, ``5. marca 2027``)
    and at least once outside a birth context ("narodená 03/2027")."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", year_month or "")
    if not m or not 1 <= int(m.group(2)) <= 12:
        return False
    y, mm = m.group(1), m.group(2)
    hay = _norm(text)
    found = []
    numeric = rf"(?<![\d.,/-])0?{int(mm)}\s?[./]\s?{y}(?!\d|[./-]\d)"
    found += [x.span() for x in re.finditer(numeric, hay)]
    found += [x.span() for x in re.finditer(rf"(?<![\d./-]){y}-{mm}(?!\d|-\d)", hay)]
    for x in re.finditer(rf"(?<![a-z0-9])([a-z]+)\.?\s+{y}(?!\d)", hay):
        if _month_number(x.group(1)) == int(mm):
            found.append(x.span())
    for start, end in found:
        if _DAY_BEFORE_RE.search(hay[max(0, start - 4) : start]):
            continue  # part of a full date
        window = hay[max(0, start - BIRTH_WINDOW) : end + BIRTH_WINDOW]
        if _BIRTH_CONTEXT_RE.search(window):
            continue
        return True
    return False


def _parse_month(value) -> str | None:
    """``YYYY-MM`` (or ``YYYY-MM-00``) with a month 01–12 → ``YYYY-MM`` (#919)."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-00)?", value.strip())
    if not m or not 1 <= int(m.group(2)) <= 12 or int(m.group(1)) < 1:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def _parse_iso(value) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return None


_STAT_KEYS = ("finish_reason", "prompt_tokens", "completion_tokens", "reasoning_tokens")


def propose(
    contract: Contract,
    text: str,
    *,
    variant: str | None = None,
    sampling: str | None = None,
    stats: dict | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Ask the local model for the fields (blocking).

    ``prompt_variant(variant)`` = ``v1`` (default): the pre-#926 request, byte
    for byte (template only, ``llm.chat`` defaults). ``v2`` (#926): reasoning
    explicitly on, the sampling profile of ``sampling_mode(sampling)``, the
    worked examples; ``sampling`` is ignored under v1. ``stats`` (optional)
    receives counts only: ``finish_reason``, token counts of the last call,
    ``retried``, ``prompt_variant``, ``sampling`` (``legacy`` under v1).
    ``model`` / ``reasoning_effort``: per-job overrides of the classification
    scorecard only (dry run, validated there); sent only when given, so a
    curated run's request is unchanged."""
    chosen = prompt_variant(variant)
    system = system_prompt(contract, chosen)
    user = f"TEXT:\n{text[:MAX_TEXT_CHARS]}"
    if chosen == "v2":
        mode = sampling_mode(sampling)
        kwargs = {"thinking": True, **llm.sampling_profile(SAMPLING_MODES[mode])}
    else:
        mode, kwargs = "legacy", {}
    if model is not None:
        kwargs["model"] = model
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    info: dict = {}
    if stats is not None:
        stats.update(prompt_variant=chosen, sampling=mode, retried=False)
    try:
        answer = llm.chat(system, user, info=info, **kwargs)
    except llm.LocalLLMError as exc:
        if "truncated" not in str(exc) or "overflow" in str(exc):
            raise  # an overflowing prompt does not fit any better with a bigger budget
        # 2026-11-08: 3 of 11 documents ran out of reasoning budget. One retry
        # with twice the budget (and time) before the document goes to review,
        # but only if the context window actually leaves more room than before.
        est = llm.estimate_prompt_tokens(system, user)
        if llm.clamp_budget(2 * llm.REASONING_BUDGET, est) <= llm.clamp_budget(
            llm.REASONING_BUDGET, est
        ):
            raise
        if stats is not None:
            stats["retried"] = True
        info = {}
        answer = llm.chat(
            system,
            user,
            max_tokens=2 * llm.REASONING_BUDGET,
            timeout=2 * config.LOCAL_LLM_TIMEOUT_S,
            info=info,
            **kwargs,
        )
    finally:
        if stats is not None:  # counts only: never the answer channels
            stats.update({k: info.get(k) for k in _STAT_KEYS if k in info})
    return llm.parse_json_object(answer)


CAREGIVER_DOC_TYPES = {"Reference", "Advocate"}

# Words that can stand next to a surname without being a given name.
_NOT_GIVEN_NAMES = {
    "pacient",
    "pacientka",
    "meno",
    "priezvisko",
    "rodne",
    "cislo",
    "adresa",
    "poistenec",
    "poistenka",
    "datum",
    "pani",
    "pan",
    "mudr",
    "mgr",
    "ing",
    "phd",
    "bc",
    "rodina",
    "rod",
    "manzel",
    "manzelka",
    "syn",
    "dcera",
    "otec",
    "matka",
}
_NAME = r"[^\W\d_][^\W\d_]+"


def _fold(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).lower()


def other_person_named(patient_display: str, text: str) -> str | None:
    """Name of a different person sharing the patient's surname, if the text names one
    and never names the patient (#578: a son's dermatology report and a husband's
    membership invoice were filed under the patient). Deterministic, no model.
    """
    parts = patient_display.split()
    if len(parts) < 2 or not text:
        return None
    given = _fold(parts[0])
    folded = _fold(text)
    stem = _fold(parts[-1])
    stem = stem[:-3] if stem.endswith("ova") else stem
    if len(stem) < 3:
        return None
    g, st = re.escape(given), re.escape(stem)
    # The patient counts as named only with given name AND surname together
    # (a doctor may share the given name); relatives may then appear too.
    if re.search(rf"\b{g}[,\s]+{st}\w*\b|\b{st}\w*[,\s]+{g}\b", folded):
        return None
    pattern = re.compile(
        rf"\b({_NAME})[,\s]+({re.escape(stem)}\w*)\b|\b({re.escape(stem)}\w*)[,\s]+({_NAME})\b"
    )
    for m in pattern.finditer(folded):
        other, sur = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if other not in _NOT_GIVEN_NAMES and not other.startswith(stem):
            start = m.start()
            return text[start : m.end()].strip()
    return None


_CAMEL_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (case-insensitive) between two short tokens."""
    a, b = a.lower(), b.lower()
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _leading_word(token: str) -> str:
    words = _CAMEL_WORD_RE.findall(token)
    return words[0].lower() if words else token.lower()


def _same_entity(token: str, known: str) -> bool:
    """#754: an OCR typo of ``known``, not a different entity with a similar name.

    ``XyzAmbulancia`` vs ``AbcAmbulancia`` share a long suffix (difflib ratio is
    high) but differ in the leading word, so they are two departments.
    """
    if _edit_distance(token, known) > SNAP_MAX_EDITS:
        return False
    words_t, words_k = _CAMEL_WORD_RE.findall(token), _CAMEL_WORD_RE.findall(known)
    if len(words_t) <= 1 and len(words_k) <= 1:
        return True  # single-word tokens (surnames, acronyms): the distance decides
    return _leading_word(token) == _leading_word(known)


# ── #854: automatic vocabulary ──────────────────────────────────────

MAX_NEW_TOKEN_LEN = 40  # a longer CamelCase name is no usable filename field
BIRTH_WINDOW = 60  # characters around a printed name that must not speak of birth
_BIRTH_CONTEXT_RE = re.compile(
    r"narod|\bdob\b|date of birth|\bborn\b|geburt|rodne\s*cislo|\br\.\s*c\.(?!\w)"
)
_NEW_TOKEN_RE = re.compile(r"[A-Z][A-Za-z0-9]+")


def _norm(value: str) -> str:
    """Folded (no diacritics, lower case) with whitespace runs collapsed."""
    return re.sub(r"\s+", " ", _fold(value or "")).strip()


def _birth_variants(birth_dates) -> set[str]:
    out: set[str] = set()
    for b in birth_dates:
        y, m, d = b[:4], b[4:6], b[6:]
        out |= {
            p.format(d=d, m=m, y=y, di=str(int(d)), mi=str(int(m)), yy=y[2:])
            for p in _DATE_PATTERNS
        }
    return out


def printed_in_text(value: str, text: str, birth_dates=frozenset()) -> bool:
    """``value`` is printed in ``text``: the same spelling ignoring diacritics, case
    and whitespace runs, on word boundaries, at least once outside a birth context
    (a birth place next to "narodená" or the DOB is not an institution)."""
    needle = _norm(value)
    if len(re.sub(r"[^a-z0-9]", "", needle)) < 2:
        return False
    hay = _norm(text)
    births = _birth_variants(birth_dates)
    for m in re.finditer(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", hay):
        window = hay[max(0, m.start() - BIRTH_WINDOW) : m.end() + BIRTH_WINDOW]
        if _BIRTH_CONTEXT_RE.search(window) or any(b in window for b in births):
            continue
        return True
    return False


# ── #926: verified classifier metadata ──────────────────────────────

METADATA_FIELDS = ("diagnoses", "doctor", "hospital_unit", "date_evidence")


def _printed_plain(value, text: str) -> bool:
    """``value`` printed in ``text`` ignoring diacritics, case and whitespace
    runs, on word boundaries (no birth-context rule: these are no vocabulary)."""
    if not isinstance(value, str):
        return False
    needle = _norm(value)
    if len(re.sub(r"[^a-z0-9]", "", needle)) < 2:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
    return re.search(pattern, _norm(text)) is not None


def _icd_printed(code, text: str) -> bool:
    """An ICD code as returned, or the same code with / without its dot."""
    if not isinstance(code, str) or not code.strip():
        return False
    code = code.strip()
    variants = {code, code.replace(".", "")}
    if "." not in code and len(code) > 3:
        variants.add(f"{code[:3]}.{code[3:]}")
    return any(_printed_plain(v, text) for v in variants)


def verify_metadata(raw: dict, text: str, iso_date: str | None) -> tuple[dict, list[str]]:
    """The classifier's sidecar metadata, checked against the text (#926).

    Generic, no layout rules: every ICD code in ``diagnoses`` printed (a code
    that is not is dropped), the doctor's surname printed, ``hospital_unit``
    printed, ``date_evidence`` printed AND holding ``iso_date`` (the validated
    clinical date, ``date_in_text``). A failing field is dropped (None / the
    printed codes only) and ``unverified_<field>`` is listed. An empty field
    is no failure. Returns ``(fields, codes)``."""
    raw = raw if isinstance(raw, dict) else {}
    text = text or ""
    out: dict = {}
    codes: list[str] = []
    diagnoses = raw.get("diagnoses") if isinstance(raw.get("diagnoses"), list) else []
    kept = [d.strip() for d in diagnoses if _icd_printed(d, text)]
    out["diagnoses"] = kept
    if len(kept) != len([d for d in diagnoses if d not in (None, "")]):
        codes.append("unverified_diagnoses")
    doctor = raw.get("doctor")
    if isinstance(doctor, str) and doctor.strip():
        ok = _printed_plain(doctor_surname(doctor), text)
        out["doctor"] = doctor if ok else None
        codes += [] if ok else ["unverified_doctor"]
    else:
        out["doctor"] = None
    unit = raw.get("hospital_unit")
    if isinstance(unit, str) and unit.strip():
        ok = _printed_plain(unit, text)
        out["hospital_unit"] = unit if ok else None
        codes += [] if ok else ["unverified_hospital_unit"]
    else:
        out["hospital_unit"] = None
    evidence = raw.get("date_evidence")
    if isinstance(evidence, str) and evidence.strip():
        ok = (
            iso_date is not None
            and _printed_plain(evidence, text)
            and _date_printed_in(iso_date, evidence)
        )
        out["date_evidence"] = evidence if ok else None
        codes += [] if ok else ["unverified_date_evidence"]
    else:
        out["date_evidence"] = None
    return out, codes


def _date_printed_in(iso_date: str, text: str) -> bool:
    """``date_in_text`` for a day; for a month-precision date (``YYYY-MM`` or
    ``YYYY-MM-00``, #919) the month and year printed in ``text``."""
    month = _parse_month(iso_date)
    if month is not None:
        return month_in_text(month, text)
    return date_in_text(iso_date, text)


def names_patient(value: str, contract: Contract) -> bool:
    """A word of ``value`` is the patient's given name, filename token, or built on
    the surname (``Novak`` for ``Jana Novakova``) — never a vocabulary entry."""
    parts = [_fold(p) for p in contract.patient_display.split()]
    if not parts:
        return False
    given, surname = parts[0], parts[-1]
    stem = surname[:-3] if surname.endswith("ova") else surname
    token = _fold(contract.patient_token)
    words = re.findall(r"[a-z0-9]+", _fold(value))
    if token and token == "".join(words):
        return True
    return any(w in (given, token) or (len(stem) >= 3 and w.startswith(stem)) for w in words)


def _in_text(token: str, folded_text: str) -> bool:
    """``token`` printed in the text, ignoring diacritics, case, spaces and punctuation."""
    return bool(token) and re.sub(r"[^a-z0-9]", "", token.lower()) in folded_text


def validate(
    contract: Contract,
    raw: dict,
    text: str,
    *,
    today: date | None = None,
    max_age_days: int | None = None,
    auto_vocab: bool = False,
) -> Classification:
    """Apply the hard rules. ``auto_vocab`` (#854): see the module docstring."""
    today = today or date.today()
    reasons: list[str] = []
    flags: list[str] = []
    additions: list[VocabAddition] = []

    folded_text = re.sub(r"[^a-z0-9]", "", _fold(text or ""))

    def snap(token: str, label: str) -> str:
        """Snap an OCR-misspelled token to the contract vocabulary (Karnok → Kornok).

        Only a genuine misspelling of the same entity is snapped (#754); a
        similar-looking but different README token is never substituted.
        """
        if token == UNKNOWN or token in contract.known_tokens:
            return token
        same = [k for k in contract.known_tokens if k.lower() == token.lower()]
        if len(same) == 1:  # #900: the README spelling of the same (folded) token
            return same[0]
        for match in difflib.get_close_matches(
            token, contract.known_tokens, n=3, cutoff=SNAP_CUTOFF
        ):
            if not _same_entity(token, match):
                continue
            if label in ("institution", "doctor") and not (
                _in_text(match, folded_text) or not _in_text(token, folded_text)
            ):
                continue  # the model's token is printed and the README one is not
            flags.append(f"snapped {token!r} → {match!r} (README vocabulary)")
            return match
        return token

    def resolve_new(label: str, token: str, printed: list, sources: list, to_token=camel) -> str:
        """#854: a token not in README §3 → README token, new printed token, or Unknown.

        Appends to ``additions`` only for a spelling printed in the text; a near
        neighbour of a README token that #754 says is another entity → review.
        """
        if token == UNKNOWN or token in contract.known_tokens:
            return token
        if any(isinstance(v, str) and "?" in v for v in sources):
            flags.append(f"{label} reading uncertain ('?') — filed as Unknown")
            return UNKNOWN
        lower = {k.lower(): k for k in contract.known_tokens}
        for value in (v.strip() for v in printed if isinstance(v, str) and v.strip()):
            if not printed_in_text(value, text or "", contract.birth_dates):
                continue
            if names_patient(value, contract):  # first: never matched, never added
                flags.append(f"{label} value names the patient — filed as Unknown")
                return UNKNOWN
            cand = to_token(value)
            if cand.lower() in lower:
                return lower[cand.lower()]
            snapped = snap(cand, label)
            if snapped in contract.known_tokens:
                return snapped
            near = sorted(
                k for k in contract.known_tokens if _edit_distance(cand, k) <= SNAP_MAX_EDITS
            )
            if near:
                reasons.append(
                    f"new {label} {cand!r} is close to README token {near[0]!r} — same entity?"
                )
                return cand
            if (
                cand == UNKNOWN
                or not _NEW_TOKEN_RE.fullmatch(cand)
                or len(cand) > MAX_NEW_TOKEN_LEN
            ):
                flags.append(f"{label} value is not a usable token — filed as Unknown")
                return UNKNOWN
            additions.append(VocabAddition(label.capitalize(), cand, value))
            return cand
        flags.append(f"new {label} {token!r} is not printed in the text — filed as Unknown")
        return UNKNOWN

    doc_type = raw.get("doc_type") if isinstance(raw.get("doc_type"), str) else None
    if doc_type not in contract.doc_types:
        reasons.append(f"doc_type {doc_type!r} not in contract")
        doc_type = "Other"

    iso = _parse_iso(raw.get("doc_date"))
    month = None if iso else _parse_month(raw.get("doc_date"))
    if month is not None:
        # #919: a printed month and year without a day → YYYYMM00
        y, mo = int(month[:4]), int(month[5:])
        births = {b[:6] for b in contract.birth_dates}
        if f"{month[:4]}{month[5:]}" in births:
            reasons.append(f"date {month} is the patient's birth month")
            month = None
        elif (y, mo) > (today.year, today.month):
            reasons.append(f"date {month} is in the future")
            month = None
        elif y < 1900:
            reasons.append(f"date {month} is implausible")
            month = None
        elif not month_in_text(month, text or ""):
            reasons.append(f"month {month} is not printed on its own in the document text")
            month = None
        elif max_age_days is not None and (today - date(y, mo, 1)).days > max_age_days:
            reasons.append(f"date {month} is more than {max_age_days // 365} years old — suspect")
            month = None
    elif iso is None:
        reasons.append("no valid clinical date")
    else:
        compact = iso.replace("-", "")
        if compact in contract.birth_dates:
            reasons.append(f"date {iso} is the patient's birth date")
            iso = None
        elif date.fromisoformat(iso) > today:
            reasons.append(f"date {iso} is in the future")
            iso = None
        elif date.fromisoformat(iso).year < 1900:
            reasons.append(f"date {iso} is implausible")
            iso = None
        elif not date_in_text(iso, text):
            reasons.append(f"date {iso} does not appear in the document text")
            iso = None
        elif max_age_days is not None and (today - date.fromisoformat(iso)).days > max_age_days:
            # Oncology contract rule: an old date on a *new* scan is suspect
            # (the classic failure is a historical date read off the page).
            reasons.append(f"date {iso} is more than {max_age_days // 365} years old — suspect")
            iso = None

    institution = snap(
        camel(raw.get("institution")) if raw.get("institution") else UNKNOWN, "institution"
    )
    hospital = snap(camel(raw.get("hospital")) if raw.get("hospital") else UNKNOWN, "hospital")
    doctor = snap(surname_token(raw.get("doctor")), "doctor")
    # #919: the builder's own subject rule (no ID-like digit runs, never a
    # leading digit), so the flagged token is the one the filename will carry
    subject = subject_token(raw.get("subject"))
    subject = snap(subject, "subject") if subject else None
    if auto_vocab:
        # #854: a new institution / doctor printed in the text joins README §3;
        # an unknown one alone is no reason to keep the document in the inbox.
        institution = resolve_new(
            "institution",
            institution,
            [raw.get("institution"), raw.get("institution_printed")],
            [raw.get("institution"), raw.get("institution_printed")],
        )
        raw_doctor = raw.get("doctor") if isinstance(raw.get("doctor"), str) else None
        doctor = resolve_new(
            "doctor", doctor, [surname_text(raw_doctor)], [raw_doctor], to_token=surname_token
        )
        if institution == UNKNOWN:
            flags.append("institution unknown — filed as Unknown")
    else:
        if institution == UNKNOWN:
            reasons.append("institution unknown")
        # #754: a new institution or doctor is an entity the README does not know
        # yet — a human confirms it (and adds it to §3) before anything is filed.
        for label, token in (("institution", institution), ("doctor", doctor)):
            if token != UNKNOWN and token not in contract.known_tokens:
                reasons.append(f"new {label} {token!r} not in README vocabulary")
    if has_title(doctor):  # #900: a title is never a doctor token, new or filed
        flags.append("doctor token still carries a title — filed as Unknown")
        additions[:] = [a for a in additions if a.label != "Doctor"]
        doctor = UNKNOWN
    # A new department / subject is plausible on its own: file it, but say so.
    for label, token in (("HospitalName", hospital), ("Subject", subject)):
        if token and token != UNKNOWN and token not in contract.known_tokens:
            flags.append(f"new {label} value {token!r} (not in README)")

    multiple = raw.get("multiple_documents")
    if multiple is True or (isinstance(multiple, str) and multiple.lower() == "true"):
        reasons.append("looks like several documents in one file (split needed)")

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < MIN_CONFIDENCE:
        reasons.append(f"model confidence {confidence:.2f} < {MIN_CONFIDENCE}")
    # Reference/advocate material is written or collected by the caregiver,
    # whose name is expected there; everything else must be the patient's.
    other = (
        None
        if doc_type in CAREGIVER_DOC_TYPES
        else other_person_named(contract.patient_display, text)
    )
    if other:
        reasons.append(f"document names another person ({other}), not {contract.patient_display}")

    if reasons:
        additions.clear()  # a document that waits for a human adds nothing to the README
    for a in additions:
        flags.append(f"new {a.label} {a.token!r} added to README vocabulary (printed in the text)")

    fields = None
    token = iso.replace("-", "") if iso is not None else None
    if month is not None:
        token = month_token(month)
    if token is not None:
        fields = NameFields(
            date=token,
            patient=contract.patient_token,
            institution=institution,
            doc_type=doc_type,
            doctor=doctor,
            hospital=hospital,
            subject=subject or None,
        )
    return Classification(
        fields=fields,
        doc_type=doc_type,
        raw=raw,
        review_needed=bool(reasons),
        reasons=reasons,
        flags=flags,
        additions=additions,
    )
