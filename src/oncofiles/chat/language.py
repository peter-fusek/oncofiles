"""Language of a chat question (#1020): stdlib only, deterministic.

The chat answers in the language of the QUESTION, not the UI setting. ``detect``
looks at the script, at letters that only some languages use and at short
function words, and returns a code of ``LANGUAGE_NAMES`` or the caller's
``fallback`` (the UI or adapter language) when the text is short or ambiguous.

* Cyrillic → ``uk`` (``ru`` only with letters Ukrainian does not use: ы э ъ ё);
* Latin: every language scores two points per distinct letter of its own set
  (Slovak ä ô ĺ ľ ŕ, Czech ě ř ů, German ä ö ü ß, Hungarian ő ű ö ü, Polish ą ę
  ł ś ź ż ć ń, …) and two per function word. A word with diacritics must
  match exactly; a word without them is compared folded (people often type
  Slovak without diacritics). A word (or its folded form) listed for two
  languages counts for neither, so ``je`` or ``co`` never decide Slovak vs Czech.
* The best language needs at least ``MIN_SCORE`` points, at least
  ``MIN_WORDS`` words and a strict lead over the second; else ``fallback``.

Pure functions, no I/O, no logging.
"""

from __future__ import annotations

import re
import unicodedata

# The fixed phrases of the chat exist in these languages (``prompts``); any
# other detected language gets the English phrases.
PHRASE_LANGS = ("sk", "cs", "en", "de", "hu", "pl", "uk")

# Every code ``detect`` may return, with the English name the prompt uses.
LANGUAGE_NAMES = {
    "sk": "Slovak",
    "cs": "Czech",
    "en": "English",
    "de": "German",
    "hu": "Hungarian",
    "pl": "Polish",
    "uk": "Ukrainian",
    "ru": "Russian",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
}

MIN_SCORE = 4
MIN_WORDS = 2

_LETTERS = {
    "sk": "äôĺľŕ",
    "cs": "ěřů",
    "de": "äöüß",
    "hu": "őűöü",
    "pl": "ąęłśźżćń",
    "fr": "èêçàâîûëïœù",
    "es": "ñ¿¡",
    "it": "àèìòù",
}

_WORDS = {
    "sk": """
        sa som si sme ste mi ma moja moje môj mojej mojom aká aký aké ako čo kedy bol bola
        bolo boli posledná posledný posledné poslednej najbližší najbližšie pre ktoré ktorý
        ktorá sú ešte aj alebo ukáž koľko prečo lekár lekára lekárom lekárska liečba liečbe
        správa správe správy pri so nie je toto tento výsledky hodnota dokumenty termín
        predtým
        """,
    "cs": """
        se jsem jsi jsme jste mě mé moje můj mojí jaká jaký jaké jak co kdy byl byla bylo
        byli poslední nejbližší pro které který která jsou ještě nebo ukaž kolik proč lékař
        lékaře lékařem lékařská léčba léčbě zpráva zprávě zprávy při není je toto tento
        výsledky hodnota dokumenty termín mám
        """,
    "en": """
        the is are what when my of how did does do which last latest show me value values
        next since with about have has it for to any all there been were could not find
        that this your before ask doctor treatment records advice some these
        """,
    "de": """
        der die das ist war wie was wann mein meine meinen meiner letzte letzten letzter
        nächste nächster nächsten und ich nicht mir mich welche welcher welches seit wert
        werte bericht befund gibt habe hatte von zum zur dem den des ein eine einen bei mit
        für auf sind wurde wo wer dies keine ihren ihr unterlagen behandlung arzt
        """,
    "hu": """
        az és mikor milyen mennyi volt van vannak egy nem hogy legutóbbi utolsó következő
        eredmény eredménye értéke érték mit melyik hol miért óta nekem kérem orvos orvosát
        időpont ez meg előtt csak ennek tudok
        """,
    "pl": """
        jest jaki jaka jakie kiedy mój moja mojego ostatni ostatnia ostatnie następny
        następna wynik wyniki wartość był była było czy się dla mam proszę pokaż gdzie ile
        dlaczego lekarz który która które przed swojego tego mogę tylko
        """,
    "fr": """
        le les est quel quelle quand mon mes dernier dernière prochain du et un une résultat
        valeur était depuis pour avec où qui
        """,
    "es": """
        el los las es cuál cuándo último última próxima próximo resultado valor fue desde
        para qué dónde y del mis tengo hay
        """,
    "it": """
        il lo gli è qual quale quando mio mia prossimo prossima risultato valore era da per
        che dove sono della degli ho
        """,
}

_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_RUSSIAN_ONLY = set("ыэъё")
_UKRAINIAN_ONLY = set("іїєґ")
_TOKEN = re.compile(r"[^\W\d_]+")


def _fold(word: str) -> str:
    nfkd = unicodedata.normalize("NFKD", word)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _tables() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(exact words with diacritics, folded ASCII words) per language, every
    word listed for two languages removed from both."""
    exact = {lang: {w for w in words.split() if not w.isascii()} for lang, words in _WORDS.items()}
    folded = {lang: {_fold(w) for w in words.split()} for lang, words in _WORDS.items()}
    for table in (exact, folded):
        seen: dict[str, int] = {}
        for words in table.values():
            for w in words:
                seen[w] = seen.get(w, 0) + 1
        for lang in table:
            table[lang] = {w for w in table[lang] if seen[w] == 1}
    return exact, folded


_EXACT, _FOLDED = _tables()


def phrase_lang(lang: str | None) -> str:
    """The language of the fixed phrases for ``lang``: itself when the phrase
    tables have it, else English."""
    return lang if lang in PHRASE_LANGS else "en"


def name(lang: str | None) -> str | None:
    """The English name of a detected language (for the prompt), or None."""
    return LANGUAGE_NAMES.get(lang or "")


def scores(text: str) -> dict[str, int]:
    """Points per Latin-script language (diagnostics and tests)."""
    lower = (text or "").lower()
    out = {lang: 0 for lang in _WORDS}
    present = set(lower)
    for lang, letters in _LETTERS.items():
        out[lang] += 2 * len(present & set(letters))
    for word in _TOKEN.findall(lower):
        for lang in out:
            if (word in _EXACT[lang]) if not word.isascii() else (word in _FOLDED[lang]):
                out[lang] += 2
    return out


def detect(text: str, fallback: str | None = None) -> str | None:
    """The language code of ``text`` (see the module docstring), else ``fallback``."""
    lower = (text or "").lower()
    words = _TOKEN.findall(lower)
    if len(words) < MIN_WORDS:
        return fallback
    cyrillic = len(_CYRILLIC.findall(lower))
    latin = sum(1 for c in lower if c.isalpha() and c.isascii())
    if cyrillic > latin:
        letters = set(lower)
        if letters & _RUSSIAN_ONLY and not letters & _UKRAINIAN_ONLY:
            return "ru"
        return "uk"
    ranked = sorted(scores(lower).items(), key=lambda kv: kv[1], reverse=True)
    (best, top), (_second, runner_up) = ranked[0], ranked[1]
    if top < MIN_SCORE or top == runner_up:
        return fallback
    return best
