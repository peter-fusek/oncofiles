"""Internationalization string tables for generated content."""

from __future__ import annotations

from oncofiles.config import PREFERRED_LANG

# String tables keyed by language code.
# EN is always the primary language; PREFERRED_LANG is the secondary.

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "treatment_timeline": "# Treatment Timeline",
        "no_treatment_events": "No treatment events recorded.",
        "research_library": "# Research Library",
        "no_research_entries": "No research entries saved.",
        "tags": "Tags",
        "page_break": "--- Page Break ---",
        "extracted_text": "--- Extracted Text ---",
        "document_images": "--- Document Images ---",
        "document_catalog": "Document Catalog",
        "latest_lab_results": "Latest Lab Results",
        "conversation_archive": "Conversation Archive",
        "activity_timeline": "Activity Timeline",
    },
    "sk": {
        "treatment_timeline": "# Priebeh liečby",
        "no_treatment_events": "Žiadne zaznamenané liečebné udalosti.",
        "research_library": "# Výskumná knižnica",
        "no_research_entries": "Žiadne uložené výskumné záznamy.",
        "tags": "Štítky",
        "page_break": "--- Zlom strany ---",
        "extracted_text": "--- Extrahovaný text ---",
        "document_images": "--- Obrázky dokumentu ---",
        "document_catalog": "Katalóg dokumentov",
        "latest_lab_results": "Najnovšie laboratórne výsledky",
        "conversation_archive": "Archív rozhovorov",
        "activity_timeline": "Časová os aktivít",
    },
}


def t(key: str, lang: str = "en") -> str:
    """Get a translated string by key and language code."""
    table = STRINGS.get(lang, STRINGS["en"])
    return table.get(key, STRINGS["en"].get(key, key))


def preferred_lang() -> str:
    """Return the configured preferred language (default 'sk')."""
    return PREFERRED_LANG


def needs_secondary() -> bool:
    """Return True if a secondary language file is needed (preferred != en)."""
    return PREFERRED_LANG != "en"
