"""The chat answers in the language of the question (#1020, maintainer 2026-11-28).

Language detection (stdlib heuristic), the fixed phrase tables, the prompt's
language rule, SK/EN-only keyword routes (any other language takes the JSON
loop), adapters passing the UI language only as the fallback, and the
scorecard's ``answer_language_match``.

Synthetic data only (the invented patient of the chat core tests, dates
2027+). The model is a scripted fake of ``oncofiles.curated.llm.chat``.
"""

from __future__ import annotations

import logging

import pytest

from oncofiles.chat import core, language, prompts, router
from oncofiles.chat import scorecard as chs
from oncofiles.curated import llm
from tests import test_1020_chat_core as _base
from tests.test_1020_chat_core import (  # noqa: F401 (fixtures)
    PRINCIPAL,
    A,
    _chat_state,
    _fake,
    _seq,
    act,
    ask,
)

seeded = _base.seeded  # the chat core tests' synthetic patient (fixture)

# ── detection ──


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("Aká je posledná hodnota HGB?", "sk"),
        ("Aka je posledna hodnota HGB?", "sk"),  # Slovak typed without diacritics
        ("Čo bolo v mojej poslednej správe?", "sk"),
        ("Jaká byla poslední hodnota HGB?", "cs"),
        ("Co bylo v mé poslední zprávě?", "cs"),
        ("What is my latest HGB value?", "en"),
        ("Was war der letzte Wert von HGB?", "de"),
        ("Wann ist mein nächster Termin?", "de"),
        ("Mi volt a legutóbbi HGB érték?", "hu"),
        ("Mikor van a következő időpont?", "hu"),
        ("Jaki był ostatni wynik HGB?", "pl"),
        ("Kiedy jest następny termin?", "pl"),
        ("Яким був останній результат HGB?", "uk"),
        ("Коли наступний прийом у лікаря?", "uk"),
        ("Каким был последний результат анализа?", "ru"),
        ("Quelle était la dernière valeur de HGB ?", "fr"),
    ],
)
def test_detect_per_language(text, lang):
    assert language.detect(text, "en") == lang


@pytest.mark.parametrize(
    "text",
    ["HGB?", "CEA", "ok", "", "   ", "12.5 g/L", "Kedy?", "Aké dokumenty mám z Lucnica?"],
)
def test_short_or_ambiguous_text_gives_the_fallback(text):
    assert language.detect(text, "sk") == "sk"
    assert language.detect(text, "en") == "en"
    assert language.detect(text, None) is None


def test_shared_words_never_decide_slovak_versus_czech():
    # "je", "toto", "co" (folded "čo") are listed for both: neither scores
    assert language.scores("je toto co")["sk"] == language.scores("je toto co")["cs"] == 0


def test_detect_is_deterministic():
    text = "Was war der letzte Wert von HGB?"
    assert {language.detect(text, None) for _ in range(20)} == {"de"}


def test_phrase_lang_and_names():
    assert language.phrase_lang("de") == "de" and language.phrase_lang("fr") == "en"
    assert language.phrase_lang(None) == "en"
    assert language.name("de") == "German" and language.name(None) is None
    assert set(language.PHRASE_LANGS) <= set(language.LANGUAGE_NAMES)


# ── fixed phrase tables ──


def test_phrase_tables_are_complete():
    expected = set(prompts.PHRASE_LANGS)
    assert expected == {"sk", "cs", "en", "de", "hu", "pl", "uk"}
    assert set(prompts.PHRASE_TABLES) >= {
        "DISCLAIMER", "NOT_FOUND", "REFUSE_OTHER_PERSON", "UNVERIFIED_NOTE",
        "ERRORS.model_busy", "ERRORS.model_unavailable", "ERRORS.busy",
    }  # fmt: skip
    assert {f"ERRORS.{c}" for c in prompts.ERRORS} <= set(prompts.PHRASE_TABLES)
    for key, table in prompts.PHRASE_TABLES.items():
        assert set(table) == expected, key
        for lang, text in table.items():
            assert isinstance(text, str) and text.strip() == text and text, (key, lang)
        # every language has its own wording, apart from words that really are equal
        if key not in ("DOCUMENTS_SEEN",):
            assert table["en"] not in [table[x] for x in expected - {"en"}], key


def test_long_phrases_read_as_their_own_language():
    for lang, text in prompts.DISCLAIMER.items():
        assert language.detect(text, None) == lang, lang


def test_phrase_falls_back_to_english():
    assert prompts.phrase(prompts.DISCLAIMER, "fr") == prompts.DISCLAIMER["en"]
    assert prompts.phrase(prompts.NOT_FOUND, "de") == prompts.NOT_FOUND["de"]
    assert prompts.phrase(prompts.ERRORS["busy"], None) == prompts.ERRORS["busy"]["en"]


ORIGINAL_SK_EN = {
    "DISCLAIMER": (
        "Toto nie je lekárska rada. Pri rozhodnutiach o liečbe sa poraďte so svojím lekárom.",
        "This is not medical advice. Ask your doctor before any decision about treatment.",
    ),
    "NOT_FOUND": ("Toto som v záznamoch nenašiel.", "I could not find that in the records."),
    "SEARCHED": ("Prehľadané", "Searched"),
    "DOCUMENTS_SEEN": ("dokumenty", "documents"),
    "NOTHING_SEARCHED": ("žiadne nástroje", "no tools"),
    "REFUSE_OTHER_PERSON": (
        "Môžem odpovedať iba o záznamoch tohto pacienta, nie o inej osobe.",
        "I can only answer about this patient's records, not about another person.",
    ),
    "UNVERIFIED_NOTE": (
        "Niektoré uvedené hodnoty sú neoverené (strojovo prečítané z neistého textu).",
        "Some of these values are unverified (machine-read from uncertain text).",
    ),
    "ERRORS.model_busy": (
        "Model je práve zaneprázdnený. Skúste to prosím o chvíľu.",
        "The model is busy right now. Please try again in a moment.",
    ),
    "ERRORS.model_unavailable": (
        "Model je momentálne nedostupný. Skúste to prosím neskôr.",
        "The model is unavailable right now. Please try again later.",
    ),
    "ERRORS.busy": (
        "Ešte odpovedám na vašu predchádzajúcu otázku.",
        "I am still answering your previous question.",
    ),
    "ERRORS.daily_limit": (
        "Dnešný limit otázok pre tohto pacienta je vyčerpaný.",
        "Today's question limit for this patient has been reached.",
    ),
    "ERRORS.invalid_request": (
        "Otázku sa nepodarilo spracovať.",
        "The question could not be processed.",
    ),
    "ERRORS.internal_error": (
        "Nastala chyba. Skúste to prosím znova.",
        "Something went wrong. Please try again.",
    ),
}


def test_sk_and_en_texts_are_unchanged():
    assert set(ORIGINAL_SK_EN) == set(prompts.PHRASE_TABLES)
    for key, (sk, en) in ORIGINAL_SK_EN.items():
        assert prompts.PHRASE_TABLES[key]["sk"] == sk, key
        assert prompts.PHRASE_TABLES[key]["en"] == en, key


# ── prompt ──


def test_prompt_names_the_language_and_keeps_sources_untranslated():
    system = prompts.router_system("Alma", "2027-06-05", "de")
    assert "Answer in German (the language of the question)" in system
    assert "Never translate or convert document titles, file names, lab codes, units" in system
    json_sys = prompts.json_system("Alma", "2027-06-05", "tools", answer_lang="hu")
    assert "Answer in Hungarian" in json_sys
    assert "Answer in the language of the question" in prompts.router_system("Alma", "2027-06-05")


# ── router ──


def test_router_keywords_stay_sk_en_and_other_languages_go_to_json():
    vocab = router.Vocab(codes={"HGB": {"hemoglob"}})
    today = "2027-06-05"
    q_de = "Was war der letzte Wert von HGB?"
    assert router.route(q_de, vocab, today) is not None  # the code alone would match
    assert router.route(q_de, vocab, today, "de") is None  # detected German: JSON loop
    assert router.route(q_de, vocab, today, "fr") is None
    assert router.route("Aká je posledná hodnota HGB?", vocab, today, "sk").intent == "labs"
    assert router.route("What is my latest HGB?", vocab, today, "en").intent == "labs"
    assert router.route("Aká je posledná hodnota HGB?", vocab, today, None).intent == "labs"
    assert router.route("Aká je moja krvná skupina?", vocab, today, "sk") is None


def test_german_nouns_are_not_a_person_but_a_named_person_is():
    allowed = {"alma", "vzorova"}
    assert not router.other_person("Zeige Befunde vom Labor", allowed, "de")
    assert not router.other_person("Welche Laborwerte Hämoglobin gibt es?", allowed, "de")
    assert router.other_person("Was sind die Ergebnisse von Zuzana Vymyslená?", allowed, "de")
    assert router.other_person("Blutwerte für Zuzana", allowed, "de")
    assert router.other_person("Zeige mir alle Patienten", allowed, "de")
    assert router.other_person("Ukaž seznam všech pacientů", allowed, "cs")
    # Slovak and English keep the two-capitalised-words rule
    assert router.other_person("Aké sú výsledky Zuzana Vymyslená?", allowed, "sk")


# ── the core ──


async def test_german_question_takes_json_path_with_german_phrases(seeded, db, monkeypatch):
    def script(system, user, n):
        assert prompts.JSON_PROTOCOL_TAG in system  # never a keyword route
        assert "Answer in German" in system and "Never translate" in system
        if n == 1:
            return act("tool", name="fact_series", args={"codes": ["CEA"]})
        return act("answer", text="Der letzte Wert von CEA war 7,9 ug/L am 4. 5. 2027 [doc 2].")

    fake = _fake(monkeypatch, script)
    out = await ask(db, "Was war der letzte Wert von CEA?", lang="sk")  # UI in Slovak
    done = out["done"]
    assert out["error"] is None and done["route"] == "json" and done["result"] == "answered"
    assert done["answer_lang"] == "de" and done["phrase_lang"] == "de"
    assert out["text"].endswith(prompts.DISCLAIMER["de"])
    assert prompts.DISCLAIMER["sk"] not in out["text"]
    assert done["disclaimer"] == prompts.DISCLAIMER["de"]
    assert "7,9 ug/L" in out["text"] and out["citations"]  # the value exactly as in the source
    assert len(fake.calls) == 2


async def test_german_not_found_and_refusal(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq(act("answer", text="")))
    out = await ask(db, "Was ist meine Blutgruppe laut den Unterlagen?")
    assert out["done"]["answer_lang"] == "de" and out["done"]["result"] == "not_found"
    assert out["text"].startswith(prompts.NOT_FOUND["de"]) and prompts.SEARCHED["de"] in out["text"]
    fake = _fake(monkeypatch, _seq("never"))
    out = await ask(db, "Was sind die Ergebnisse von Zuzana Vymyslená?")
    assert out["done"]["result"] == "refused" and fake.calls == []
    assert out["text"].startswith(prompts.REFUSE_OTHER_PERSON["de"])


async def test_unsupported_language_gets_english_phrases(seeded, db, monkeypatch):
    seen: list[str] = []

    def script(system, user, n):
        seen.append(system)
        return act("answer", text="")

    _fake(monkeypatch, script)
    out = await ask(db, "Quelle était la dernière valeur de mon groupe sanguin ?", lang="sk")
    done = out["done"]
    assert done["answer_lang"] == "fr" and done["phrase_lang"] == "en"
    assert done["route"] == "json"
    assert "Answer in French" in seen[0]  # the model is still told the question's language
    assert out["text"].startswith(prompts.NOT_FOUND["en"])
    assert out["text"].endswith(prompts.DISCLAIMER["en"])


async def test_ui_language_is_only_the_fallback(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq("The latest CEA value was 7,9 ug/L on 4. 5. 2027 [doc 2]."))
    out = await ask(db, "What was my latest CEA value?", lang="sk")
    assert out["done"]["answer_lang"] == "en" and out["done"]["route"] == "router:labs"
    assert out["text"].endswith(prompts.DISCLAIMER["en"])
    _fake(monkeypatch, _seq("Posledná hodnota CEA bola 7,9 ug/L z 4. 5. 2027 [doc 2]."))
    out = await ask(db, "Aká bola posledná hodnota CEA?", lang="en")
    assert out["done"]["answer_lang"] == "sk" and out["text"].endswith(prompts.DISCLAIMER["sk"])
    # too short to tell: the UI language
    _fake(monkeypatch, _seq("CEA 7,9 ug/L (4. 5. 2027) [doc 2]."))
    out = await ask(db, "CEA?", lang="en")
    assert out["done"]["answer_lang"] == "en" and out["text"].endswith(prompts.DISCLAIMER["en"])


async def test_errors_come_in_the_question_language(seeded, db, monkeypatch):
    _fake(monkeypatch, lambda s, u, n: llm.LocalLLMTransportError("local LLM HTTP 503"))
    out = await ask(db, "Was war der letzte Wert von CEA?")
    assert out["error"] == {"code": "model_busy", "message": prompts.ERRORS["model_busy"]["de"],
                            "lane_wait_seconds": 0.0, "lane_timeouts": 0}  # fmt: skip
    _fake(monkeypatch, lambda s, u, n: llm.LocalLLMTransportError("unreachable: ConnectError"))
    out = await ask(db, "Jaki był ostatni wynik CEA?")
    assert out["error"]["message"] == prompts.ERRORS["model_unavailable"]["pl"]


async def test_no_question_text_or_language_words_in_logs(seeded, db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)

    def script(system, user, n):
        if n == 1:
            return act("tool", name="fact_series", args={"codes": ["CEA"]})
        return act("answer", text="Der letzte Wert QWERTY-ANTWORT war 7,9 ug/L [doc 2].")

    _fake(monkeypatch, script)
    out = await ask(db, "Was war der letzte Wert von CEA? QWERTY-FRAGE")
    assert out["done"]["answer_lang"] == "de"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "lang=de" in logged  # a code, never text
    for secret in ("QWERTY", "Wert", "letzte", "7,9", PRINCIPAL, "Alma"):
        assert secret not in logged
    async with db.db.execute(
        "SELECT output_summary FROM activity_log WHERE tool_name = 'chat_turn'"
    ) as cur:
        (row,) = [dict(r) for r in await cur.fetchall()]
    assert '"answer_lang": "de"' in row["output_summary"] and "QWERTY" not in row["output_summary"]


# ── adapters ──


async def test_signal_formats_with_the_answer_language(seeded, db):
    from oncofiles.channels import signal as sig

    result = {"text": "Der letzte Wert war 7,9.\n\n" + prompts.DISCLAIMER["de"],
              "citations": [], "done": {"answer_lang": "de"}}  # fmt: skip
    parts = await sig.format_answer(db, A, result, "sk")
    joined = "\n\n".join(parts)
    assert joined.endswith(prompts.DISCLAIMER["de"]) and prompts.DISCLAIMER["sk"] not in joined
    assert joined.count(prompts.DISCLAIMER["de"]) == 1
    assert sig.labels_lang("de", "sk") == "en" and sig.labels_lang("cs", "en") == "sk"
    assert sig.labels_lang(None, "sk") == "sk"


def test_dashboard_strips_a_disclaimer_of_any_language():
    from oncofiles import chat_dashboard

    for text in prompts.DISCLAIMER.values():
        assert chat_dashboard.strip_disclaimer("Antwort.\n\n" + text) == "Antwort."


# ── scorecard ──


def test_default_set_has_two_other_language_questions():
    catalog = [{"code": "HGB", "count": 3, "first_at": "2027-01-02", "last_at": "2027-05-04"}]
    qset = chs.build_default(catalog, {"NemocnicaLucnica": 2}, {"alma"})
    other = {q.category: q for q in qset.questions if q.lang and q.lang != "sk"}
    assert set(other) == {"other_language_de", "other_language_cs"}
    assert other["other_language_de"].lang == "de" and "HGB" in other["other_language_de"].text
    for q in other.values():  # built so that detection reads them as intended
        assert language.detect(q.text, None) == q.lang
    assert chs.SCORECARD_VERSION == "chat-scorecard-v3"
    assert chs.DEFAULT_SET_VERSION == "default-v2"


def test_answer_language_match_metric():
    def card(qid, lang, result, text, model_calls=1):
        q = chs.Question(qid, "c", "otázka", "answer", lang=lang)
        done = {"result": result, "model_calls": model_calls}
        out = {"text": text + "\n\n" + prompts.phrase(prompts.DISCLAIMER, lang), "done": done,
               "citations": [{"document_id": 1}]}  # fmt: skip
        return chs.card(q, out, recorded_texts=[], today="2027-06-05", lang="sk",
                        citation_checks={}, seconds=1.0, run=1)  # fmt: skip

    cards = [
        card("a", "de", "answered", "Der letzte Wert von HGB war hoch."),
        card("b", "de", "answered", "Posledná hodnota HGB bola vysoká."),  # wrong language
        card("c", "cs", "refused", prompts.REFUSE_OTHER_PERSON["cs"], 0),
        card("d", "fr", "not_found", prompts.NOT_FOUND["en"] + "\nSearched: no tools."),
        card("e", "sk", "not_found", prompts.NOT_FOUND["en"]),  # wrong phrase language
    ]
    error = chs.card(chs.Question("f", "c", "x", "answer", lang="de"),
                     {"text": "", "done": None, "error": {"code": "model_busy"}},
                     recorded_texts=[], today="2027-06-05", lang="sk", citation_checks={},
                     seconds=1.0, run=1)  # fmt: skip
    assert [c["answer_language_match"] for c in cards] == [True, False, True, True, False]
    assert error["answer_language_match"] is None
    t = chs.totals([*cards, error])
    assert t["language"]["checked"] == 5 and t["language"]["matched"] == 3
    assert t["language"]["answer_language_match"] == 60.0
    assert "language.answer_language_match" in chs._HIGHER_IS_BETTER


def test_question_file_takes_a_language_per_question(tmp_path):
    import json

    path = tmp_path / "q.json"
    path.write_text(json.dumps({"lang": "sk", "questions": [
        {"id": "a", "category": "x", "question": "Aká je hodnota?"},
        {"id": "b", "category": "y", "question": "Wie hoch ist der Wert?", "lang": "de"},
    ]}))  # fmt: skip
    qset = chs.load_file(str(path))
    assert [q.lang for q in qset.questions] == ["sk", "de"]
    path.write_text(json.dumps({"questions": [
        {"id": "a", "category": "x", "question": "?", "lang": "xx"}]}))  # fmt: skip
    with pytest.raises(chs.QuestionFileError, match="bad_question_lang"):
        chs.load_file(str(path))


def test_core_answer_language_helper():
    assert core.answer_language("Was war der letzte Wert von HGB?", "sk") == ("de", "de")
    assert core.answer_language("HGB?", "sk") == ("sk", None)


# ── reasoning marker, headroom, short retry (#1043 production scorecard) ──


def _with_info(monkeypatch, script):
    """A fake ``llm.chat`` whose ``script(system, user, n, info)`` may fill
    ``info`` (finish_reason, token counts) and return text or raise."""
    calls: list[dict] = []

    def fake(system, user, **kw):
        calls.append({"system": system, "user": user, "kw": kw})
        item = script(system, user, len(calls), kw["info"])
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(llm, "chat", fake)
    return calls


async def test_every_chat_call_sends_the_reasoning_marker(seeded, db, monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)  # the production default

    def script(system, user, n, info):
        if prompts.JSON_PROTOCOL_TAG in system:
            return act("answer", text="")
        return "Posledná hodnota CEA bola 7,9 ug/L z 4. 5. 2027 [doc 2]."

    calls = _with_info(monkeypatch, script)
    await ask(db, "Aká bola posledná hodnota CEA?")  # router
    await ask(db, "Je to tak?")  # JSON loop
    assert len(calls) >= 2
    assert any(prompts.JSON_PROTOCOL_TAG in c["system"] for c in calls)
    for call in calls:
        kw = call["kw"]
        assert kw["thinking"] is False and kw["reasoning_marker"] is True
        sent = llm._system_content(call["system"], kw["thinking"], kw["reasoning_marker"])
        assert sent.endswith(llm.REASONING_OFF_MARKER) and llm.NO_THINK not in sent
    assert core.ROUTER_ANSWER_BUDGET == core.JSON_STEP_BUDGET == 800


def test_every_llm_chat_call_in_chat_passes_the_marker_literally():
    import ast
    from pathlib import Path

    found = 0
    for path in Path(core.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "chat"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "llm"
            ):
                found += 1
                marker = [k for k in node.keywords if k.arg == "reasoning_marker"]
                assert marker and isinstance(marker[0].value, ast.Constant), path.name
                assert marker[0].value.value is True, path.name
    assert found >= 1


async def test_truncation_then_short_retry_answers(seeded, db, monkeypatch):
    def script(system, user, n, info):
        info.update(prompt_tokens=300, completion_tokens=800, reasoning_tokens=700)
        if n == 1:
            info["finish_reason"] = "length"
            return "Posledná hodnota CEA bola 7,9 ug/L a ešte dlhý text, ktorý"
        info["finish_reason"] = "stop"
        return "Posledná hodnota CEA bola 7,9 ug/L z 4. 5. 2027 [doc 2]."

    calls = _with_info(monkeypatch, script)
    out = await ask(db, "Aká bola posledná hodnota CEA?")
    done = out["done"]
    assert done["result"] == "answered" and done["retried_short"] is True
    assert done["truncated"] == 1 and done["finish_reason"] == "stop"
    assert done["not_found_reason"] is None and "dlhý text" not in out["text"]
    assert prompts.SHORT_RETRY_NOTE in calls[1]["user"] and "at most 60 words" in calls[1]["user"]
    assert done["prompt_tokens"] == 600 and done["reasoning_tokens"] == 1400


async def test_truncation_twice_is_not_found_with_reason_and_counted_tokens(
    seeded, db, monkeypatch
):
    def script(system, user, n, info):
        # llm.chat fills the counts, then raises: reasoning used the whole budget
        info.update(finish_reason="length", prompt_tokens=250, completion_tokens=800,
                    reasoning_tokens=800)  # fmt: skip
        return llm.LocalLLMAnswerError("local LLM answer truncated: reasoning used the budget")

    calls = _with_info(monkeypatch, script)
    out = await ask(db, "Aká bola posledná hodnota CEA?")
    done = out["done"]
    assert len(calls) == 2 and done["retried_short"] is True
    assert done["result"] == "not_found" and done["not_found_reason"] == "truncated"
    assert done["finish_reason"] == "length" and done["truncated"] == 2
    assert done["answer_errors"] == 0
    assert done["prompt_tokens"] == 500 and done["completion_tokens"] == 1600
    assert done["reasoning_tokens"] == 1600
    assert out["text"].startswith(prompts.NOT_FOUND["sk"])


async def test_other_answer_error_is_not_retried_and_has_its_reason(seeded, db, monkeypatch):
    def script(system, user, n, info):
        info.update(finish_reason="stop", prompt_tokens=100)
        return llm.LocalLLMAnswerError("unparseable")

    calls = _with_info(monkeypatch, script)
    out = await ask(db, "Aká bola posledná hodnota CEA?")
    assert len(calls) == 1 and out["done"]["retried_short"] is False
    assert out["done"]["not_found_reason"] == "answer_error"
    assert out["done"]["prompt_tokens"] == 100


async def test_grounding_reason(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq("Posledná hodnota CEA bola 555 ug/L."))
    out = await ask(db, "Aká bola posledná hodnota CEA?")
    assert out["done"]["result"] == "grounding_replaced"
    assert out["done"]["not_found_reason"] == "grounding"


def test_scorecard_card_carries_the_new_codes():
    q = chs.Question("q01", "labs_latest", "otázka", "answer", lang="sk")
    done = {"result": "not_found", "model_calls": 2, "finish_reason": "length",
            "reasoning_tokens": 1600, "retried_short": True,
            "not_found_reason": "truncated"}  # fmt: skip
    c = chs.card(q, {"text": prompts.NOT_FOUND["sk"], "done": done, "citations": []},
                 recorded_texts=[], today="2027-06-05", lang="sk", citation_checks={},
                 seconds=1.0, run=1)  # fmt: skip
    assert (c["finish_reason"], c["reasoning_tokens"], c["retried_short"],
            c["not_found_reason"]) == ("length", 1600, True, "truncated")  # fmt: skip
    t = chs.totals([c])
    assert t["model"]["retried_short"] == 1 and t["model"]["reasoning_tokens"] == 1600
    assert t["model"]["not_found_reasons"] == {"truncated": 1}
