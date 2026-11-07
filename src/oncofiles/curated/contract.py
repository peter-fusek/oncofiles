"""Parse a curated folder's README.md contract (#562).

The README is authoritative and written for humans first, so only the parts
that are structurally stable are parsed strictly:

* ``**Patient …:** Display Name (`PatientToken`)`` — the fixed field 2 value
* the §2 folder table: rows ``| `<folder name>` | `<DocumentType>` | … |`` or
  ``| `<folder name>` | — | … |`` for utility folders
* birth dates on lines mentioning DOB / birth / narodenia (never a doc date)

Everything in §3 (institutions, departments, doctors) is kept as raw text and
handed to the classifier as context, plus a lenient token set used only to
*flag* first-time values. Anything that fails to parse raises
``ContractError`` and the run aborts at preflight — a run that stops and
explains itself is a successful run (README §1b, lesson 10).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

INBOX_PREFIX = "00-inbox"
LOGS_PREFIX = "98-logs"
DUPLICATES_PREFIX = "99-duplicates"
ROOT_STATE_FILES = ("README.md", "_index.json", "_changelog.md", "_actions.jsonl")


class ContractError(ValueError):
    """The README could not be read as a contract — abort the run."""


def nfc(value: str) -> str:
    """Folder names are compared in NFC — macOS / some clients hand back NFD."""
    return unicodedata.normalize("NFC", value or "")


def folder_key(folder_name: str) -> str:
    """``labs — laboratórne výsledky`` → ``labs`` (the part before the em dash)."""
    return nfc(folder_name).split(" — ", 1)[0].strip()


_PATIENT_RE = re.compile(
    r"\*\*Patient[^*]*:\*\*\s*(?P<display>[^(`\n]+?)\s*\(`(?P<token>[A-Za-z]+)`\)"
)
_VERSION_RE = re.compile(r"\*\*Standard version:\*\*\s*(?P<v>[0-9][0-9.]*)")
_FOLDER_ROW_RE = re.compile(
    r"^\|\s*`(?P<folder>[^`]+)`\s*\|\s*(?:`(?P<type>[A-Za-z]+)`|—|-)\s*\|", re.M
)
_SECTION_RE = re.compile(r"^## (?P<title>.+)$", re.M)
_BIRTH_KEYWORD_RE = re.compile(r"is the birthdate|\bDOB\b|Date of birth|Dátum narodenia", re.I)
_DMY_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TOKEN_RE = re.compile(r"`([A-Z][A-Za-z0-9]+)`|^\s*([A-Z][A-Za-z0-9]+)\b", re.M)


@dataclass(frozen=True)
class Contract:
    patient_display: str
    patient_token: str
    version: str
    # NFC folder name -> DocumentType (None for utility folders)
    folders: dict[str, str | None]
    vocab_text: str
    known_tokens: frozenset[str] = field(default_factory=frozenset)
    birth_dates: frozenset[str] = field(default_factory=frozenset)  # YYYYMMDD
    readme_text: str = ""  # full contract, handed to the classifier verbatim

    @property
    def doc_types(self) -> dict[str, str]:
        """DocumentType -> folder name."""
        return {t: f for f, t in self.folders.items() if t}

    def utility_folder(self, prefix: str) -> str:
        for name, doc_type in self.folders.items():
            if doc_type is None and name.startswith(prefix):
                return name
        raise ContractError(f"README §2 has no {prefix!r} utility folder")

    @property
    def inbox(self) -> str:
        return self.utility_folder(INBOX_PREFIX)

    @property
    def logs(self) -> str:
        return self.utility_folder(LOGS_PREFIX)

    @property
    def duplicates(self) -> str:
        return self.utility_folder(DUPLICATES_PREFIX)

    def folder_for(self, doc_type: str) -> str | None:
        return self.doc_types.get(doc_type)


def _section(text: str, number: str) -> str:
    """Body of ``## <number>. …`` up to the next ``## `` heading ('' if absent)."""
    heads = list(_SECTION_RE.finditer(text))
    for i, h in enumerate(heads):
        if h.group("title").startswith(f"{number}."):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            return text[h.end() : end]
    return ""


def _birth_dates(text: str) -> set[str]:
    """Dates stated as the patient's birth date — never valid as a document date.

    Only dates within a short window *after* a birth keyword count
    (``DOB is **02.04.1976**``, ``Dátum narodenia:** **06.05.1980**``,
    ``A `1980-05-06` value is the birthdate``), so changelog dates elsewhere on
    the same line are not swept in.
    """
    out: set[str] = set()
    for km in _BIRTH_KEYWORD_RE.finditer(text):
        before = text[max(0, km.start() - 40) : km.start()]
        after = text[km.end() : km.end() + 30]
        window = after if km.group(0).lower() != "is the birthdate" else before
        for d, m, y in _DMY_RE.findall(window):
            out.add(f"{y}{m}{d}")
        for y, m, d in _ISO_RE.findall(window):
            out.add(f"{y}{m}{d}")
    return out


def parse_contract(readme_text: str) -> Contract:
    text = nfc(readme_text)
    pm = _PATIENT_RE.search(text)
    if not pm:
        raise ContractError("README has no '**Patient …:** Name (`Token`)' line")
    vm = _VERSION_RE.search(text)

    folders_section = _section(text, "2")
    if not folders_section:
        raise ContractError("README has no '## 2.' folder-structure section")
    folders: dict[str, str | None] = {}
    for m in _FOLDER_ROW_RE.finditer(folders_section):
        name = nfc(m.group("folder").strip())
        doc_type = m.group("type")
        if name in folders:
            raise ContractError(f"folder {name!r} listed twice in README §2")
        if doc_type and folder_key(name) != doc_type.lower():
            raise ContractError(
                f"README §2: folder {name!r} key does not match DocumentType {doc_type!r}"
            )
        folders[name] = doc_type
    if not any(t for t in folders.values()):
        raise ContractError("README §2 folder table has no DocumentType rows")
    types = [t for t in folders.values() if t]
    if len(types) != len(set(types)):
        raise ContractError("README §2 maps one DocumentType to two folders")

    vocab = _section(text, "3")
    tokens = {a or b for a, b in _TOKEN_RE.findall(vocab)}
    contract = Contract(
        patient_display=pm.group("display").strip(),
        patient_token=pm.group("token"),
        version=vm.group("v") if vm else "unknown",
        folders=folders,
        vocab_text=vocab.strip(),
        known_tokens=frozenset(tokens),
        birth_dates=frozenset(_birth_dates(text)),
        readme_text=text,
    )
    # The utility folders are mandatory — touching their properties validates them.
    _ = (contract.inbox, contract.logs, contract.duplicates)
    if "Other" not in contract.doc_types:
        raise ContractError("README §2 has no 'Other' fallback folder")
    return contract
