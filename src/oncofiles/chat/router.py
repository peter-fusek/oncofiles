"""Deterministic parts of the chat core (#1020): intent router and person check.

The router is the fast path the spike chose (#1019, protocol c): for a
recognised intent it picks the tool and arguments itself and the model only
writes the answer. Intents, in order:

* ``labs``: the question names a lab code the patient has (code in capitals,
  its Slovak / English name or an alias); with a "since" word and a date it
  becomes a trend (``date_from``), a year alone a ``date_from``/``date_to``
  window; ``fact_series``;
* ``institution``: an institution of the patient's documents or organisations
  is named; ``search_documents`` with that exact stored value;
* ``report``: the last report; ``search_documents(category=report)`` and then
  ``document_detail`` of the newest hit;
* ``planned``: the next planned date; ``timeline(kinds=[planned])`` from today.

Anything else returns None and the core falls back to the JSON loop.

``other_person`` is the deterministic refusal BEFORE any model call: the
question asks about all / other patients, or names a person (two or more
capitalised words, or one after "pre/pacient/patient/brat/brother/…") that is
not the patient, a name on the patient's record (display name, chart name,
caregiver), nor a doctor or institution of the patient's records. It is
conservative on purpose: a phrase after a doctor title is never a refusal,
and Slovak declension is matched by stem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from oncofiles.chat.grounding import fold, tokens

# ── vocabulary of one patient (built by the core from read-only DB helpers) ──


@dataclass
class Vocab:
    codes: dict[str, set[str]] = field(default_factory=dict)  # code → folded terms
    institutions: dict[str, set[str]] = field(default_factory=dict)  # stored value → terms
    allowed_names: set[str] = field(default_factory=set)  # folded name tokens


_LAY = {
    "WBC": ("leukocyt", "white blood", "biele krvink"),
    "HGB": ("hemoglob", "haemoglob"),
    "PLT": ("trombocyt", "platelet", "dostick"),
    "NEUT": ("neutrofil",),
    "CREA": ("kreatin", "creatinin"),
    "CRP": ("c-reaktiv", "c reaktiv"),
}
_STOP_TERMS = frozenset({"na", "ca", "k", "p", "fe", "mg", "cl", "so", "to", "je"})


def _terms_of(code: str, row: dict) -> set[str]:
    terms: set[str] = set(_LAY.get(code, ()))
    for value in (row.get("name_sk"), row.get("name_en"), *(row.get("aliases") or [])):
        t = fold(str(value or "")).strip()
        if len(t) >= 4 and t not in _STOP_TERMS:
            terms.add(t)
    return terms


def build_codes(catalog: list[dict], vocabulary: list[dict]) -> dict[str, set[str]]:
    """Folded name terms per code the patient actually has (catalog codes only)."""
    vocab = {r["code"]: r for r in vocabulary}
    out: dict[str, set[str]] = {}
    for row in catalog:
        code = row.get("code")
        if not code:
            continue
        merged = {**vocab.get(code, {}), **{k: v for k, v in row.items() if v}}
        out[code] = _terms_of(code, merged)
    return out


def _stem(word: str) -> str:
    return word if len(word) <= 4 else word[: max(4, len(word) - 2)]


_WORD = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _term_in(term: str, folded: str, words: list[str]) -> bool:
    if " " in term or "-" in term:
        return term in folded
    stem = _stem(term)
    return any(w.startswith(stem) for w in words)


def find_codes(question: str, codes: dict[str, set[str]]) -> list[str]:
    folded = fold(question)
    words = _WORD.findall(folded)
    upper = set(re.findall(r"\b[A-Z][A-Z0-9_]{1,9}\b", question))
    found = []
    for code, terms in codes.items():
        literal = code in upper or (len(code) >= 4 and code.lower() in words)
        if literal or any(_term_in(t, folded, words) for t in terms):
            found.append(code)
    return found[:8]


# ── routes ──


@dataclass
class Route:
    intent: str
    calls: list[tuple[str, dict]] = field(default_factory=list)
    detail_newest: bool = False


_SINCE = re.compile(r"\b(od|since|from|vyvoj\w*|vyvij\w*|trend\w*|zmen\w*|chang\w*|priebeh\w*)\b")
_REPORT = re.compile(r"\b(sprav[aeuy]?|spravu|spravach|report\w*|zaver\w*)\b")
_PLANNED = re.compile(
    r"\b(planovan\w*|naplanovan\w*|termin\w*|najbliz\w*|next|upcoming|scheduled?|"
    r"kedy\s+(?:bude|ma\s+byt|mam|budem))\b"
)
_YEAR_ONLY = re.compile(r"\b(?:v\s+roku|in|rok|year)\s+(\d{4})\b")


def _date_window(folded: str) -> dict:
    out: dict = {}
    for tok in tokens(folded):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok):
            day = tok
        elif re.fullmatch(r"\d{4}-\d{2}", tok):
            day = tok + "-01"
        else:
            continue
        if _SINCE.search(folded):
            out["date_from"] = day
        return out
    m = _YEAR_ONLY.search(folded)
    if m:
        out = {"date_from": f"{m[1]}-01-01", "date_to": f"{m[1]}-12-31"}
    return out


def _institution(question: str, institutions: dict[str, set[str]]) -> str | None:
    folded = fold(question)
    words = _WORD.findall(folded)
    for value, terms in institutions.items():
        if any(_term_in(t, folded, words) for t in terms if len(t) >= 4):
            return value
    return None


def institution_terms(value: str, names: list[str] | None = None) -> set[str]:
    """Match terms of one stored institution value: the value itself (folded,
    also split at capitals, ``NemocnicaLucnica`` → ``lucnica``) and any
    organisation names given for it."""
    out = set()
    for raw in [value, *(names or [])]:
        parts = re.findall(r"[A-ZÁ-Ž][a-zá-ž]+|[a-zá-ž]{4,}", raw or "")
        for p in [raw, *parts]:
            t = fold(p).strip()
            if len(t) >= 4 and t not in ("nemocnica", "hospital", "ustav", "klinika", "unknown"):
                out.add(t)
    return out


ROUTER_LANGS = ("sk", "en")  # the keyword intents are Slovak / English only


def route(question: str, vocab: Vocab, today: str, lang: str | None = None) -> Route | None:
    """The fixed tool plan of a recognised intent, or None (→ the JSON loop).

    ``lang`` is the DETECTED language of the question (``chat.language``,
    None when unclear): the keywords are SK/EN, so a question detected as any
    other language always goes to the JSON loop, never to a keyword route."""
    if lang is not None and lang not in ROUTER_LANGS:
        return None
    folded = fold(question)
    codes = find_codes(question, vocab.codes)
    if codes:
        args: dict = {"codes": codes, **_date_window(folded)}
        return Route("labs", [("fact_series", args)])
    inst = _institution(question, vocab.institutions)
    if inst:
        return Route("institution", [("search_documents", {"institution": inst, "limit": 20})])
    if _REPORT.search(folded):
        return Route(
            "report", [("search_documents", {"category": "report", "limit": 5})], detail_newest=True
        )
    if _PLANNED.search(folded):
        return Route(
            "planned", [("timeline", {"kinds": ["planned"], "date_from": today, "limit": 10})]
        )
    return None


# ── another person: refused before any model call ──

_ALL_PATIENTS = re.compile(
    r"\b(all|other|every|another)\s+patients?\b|\bvsetk\w*\s+pacient\w*|\bin\w*\s+pacient\w*|"
    r"\bostatn\w*\s+pacient\w*|\bzoznam\s+pacient\w*|\blist\s+(?:of\s+)?patients\b|"
    # #1020: the same in the other chat languages (folded: cs, de, pl, hu)
    r"\bvsech\w*\s+pacient\w*|\bjin\w*\s+pacient\w*|\bseznam\s+pacient\w*|"
    r"\b(?:alle|andere\w*)\s+patient\w*|\bwszystk\w*\s+pacjent\w*|\binn\w*\s+pacjent\w*|"
    r"\b(?:osszes|mas|tobbi)\s+beteg\w*"
)
_TITLES = frozenset({
    "mudr", "mddr", "mvdr", "dr", "doc", "prof", "prim", "phdr", "rndr", "mgr", "ing", "bc",
    "phd", "csc", "doctor", "doktor", "doktorka", "lekar", "lekarka", "onkolog", "onkologicka",
})  # fmt: skip
_CUES = (
    "pre", "pacient", "pacienta", "pacientka", "pacientky", "patient", "brat", "brata", "sestra",
    "sestry", "brother", "sister", "mother", "father", "son", "daughter", "husband", "wife",
    "friend", "kamarat", "kamaratka", "sused", "neighbour", "neighbor",
    # German (#1020): with them a German question names a person after a cue
    "von", "für", "über", "patientin", "bruder", "schwester", "mutter", "vater", "sohn",
    "tochter", "frau", "herr", "freund", "freundin",
)  # fmt: skip
# German capitalises every noun, so two capitalised words in a row are a
# person in a German question only right after one of these cues
_DE_TWO_NAME_CUES = frozenset({"von", "fur", "uber", "patient", "patientin", "frau", "herr"})
_NAME = (
    r"[A-ZÁÄČĎÉÍĽĹŇÓÔŔŠŤÚÝŽÖÜŐŰĚŘŮĄĘŁŚŹŻĆŃ]"
    r"[a-záäčďéíľĺňóôŕšťúýžöüßőűěřůąęłśźżćń]+"
)
_TWO_NAMES = re.compile(rf"\b({_NAME}(?:\s+{_NAME})+)\b")
# the cue word in any case, the name capitalised (case-sensitive)
_CUE_NAME = re.compile(rf"\b(?i:({'|'.join(_CUES)}))\s+({_NAME})\b")
_SENTENCE_WORDS = frozenset({
    # capitalised words that start questions or name things, never persons
    "aka", "ake", "aky", "ako", "kto", "kedy", "co", "ktore", "kolko", "preco", "kde", "je",
    "what", "when", "who", "how", "which", "where", "show", "list", "tell", "give", "and",
    "the", "is", "are", "was", "did", "does", "do", "my", "a", "aj", "ukaz", "zhrn", "summarise",
    "summarize", "nemocnica", "hospital", "ustav", "klinika", "laboratorium", "ambulancia",
    "oddelenie", "onkologicky", "onkologickeho", "narodny", "fakultna", "univerzitna",
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday",
    # question words of the other chat languages (#1020: German capitalises
    # nouns, so "Zeige Befunde" must not read as a person's name)
    "wann", "wie", "welche", "welcher", "welches", "wo", "wer", "zeige", "zeig", "gib",
    "nenne", "ist", "war", "hat", "gibt", "habe", "mein", "meine", "der", "die", "das",
    "jaka", "jaky", "jake", "kdy", "jak", "kolik", "proc",
    "mi", "mikor", "milyen", "mennyi", "mutasd", "melyik", "hol",
    "jaki", "jakie", "kiedy", "pokaz", "ile", "gdzie", "czy",
    "januar", "marz", "mai", "juni", "juli", "oktober", "dezember",
})  # fmt: skip


def name_tokens(*names: str) -> set[str]:
    out = set()
    for name in names:
        for w in _WORD.findall(fold(name or "").replace("_", " ").replace(".", " ")):
            if len(w) >= 3 and w not in _TITLES and not w.isdigit():
                out.add(w)
    return out


def _matches(word: str, allowed: set[str]) -> bool:
    """Stem match for declension (``Jany`` / ``Jana``, ``Testovej`` / ``Testova``)."""
    w = fold(word)
    if w in allowed:
        return True
    for a in allowed:
        n = min(len(a), len(w))
        if n >= 3 and a[: max(3, n - 2)] == w[: max(3, n - 2)]:
            return True
    return False


def _foreign(words: list[str], allowed: set[str]) -> bool:
    content = [w for w in words if fold(w) not in _SENTENCE_WORDS and fold(w) not in _TITLES]
    if not content:
        return False
    return not any(_matches(w, allowed) for w in content)


def other_person(question: str, allowed: set[str], lang: str | None = None) -> bool:
    """True when the question is about other patients or names someone who is
    not this patient (nor a doctor, institution or name on their record).

    ``lang`` = the question's detected language: in German (every noun
    capitalised) two capitalised words count as a name only after a cue
    (``von``, ``für``, ``Frau``, …); the cue + one name rule applies as usual."""
    folded = fold(question)
    if _ALL_PATIENTS.search(folded):
        return True
    for m in _TWO_NAMES.finditer(question):
        before = question[: m.start()].rstrip(" .").split()
        if before and fold(before[-1]).rstrip(".") in _TITLES:
            continue  # "MUDr. Hana Vymyslená": a doctor, the model answers
        if lang == "de" and not (before and fold(before[-1]) in _DE_TWO_NAME_CUES):
            continue  # "Zeige Befunde": German nouns, not a person
        if _foreign(m[1].split(), allowed):
            return True
    for m in _CUE_NAME.finditer(question):
        word = m[2]
        if fold(word) in _SENTENCE_WORDS or fold(word) in _TITLES:
            continue
        if not _matches(word, allowed):
            return True
    return False
