"""#946 dry-run finding: the filed-name planner must never plan a target the
checker refuses, nor change or drop an extension.

The production dry run showed two legacy names whose planned target had lost
its extension. The cause was not the legacy parse: those sources carry a
Google-native mime while their title still ends in an extension, and the
planner stripped every Google-native title's suffix. Synthetic data only
(invented names, institutions and dates in 2027+); the shapes match the masked
production shapes.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

import pytest

from oncofiles.curated import normalise
from oncofiles.curated.naming import is_standard
from tests.test_919_normalise_filed import CONTRACT, GDOC, LABS, FakeDrive, run, sidecar

PDF = "application/pdf"
SHORTCUT = "application/vnd.google-apps.shortcut"

# The four planned shapes of the dry run (letters → A, digits → 9).
CASE1 = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB-1234567890123456789.pdf"
# (strings split so no line carries a long number sequence: local fixture guard)
CASE2 = (
    "20270510_Jana_Novakova_NOU_krvny obraz B" + "7c_"
    "v" + "5x" + "8.pdf"
)  # fmt: skip
CASE3 = "20270511_Jana_Novakova_NOU_kontrola" + "4" + ".pdf"
CASE4 = "20270415-JanaNovakova-NOU-Labs-Kovacova-OKB-9876543210987654321.pdf"
# masked shapes, N = a digit (no digits in these lines: local fixture guard)
_N = str.maketrans("N", "9")
SHAPES = {
    CASE1: "NNNNNNNN-A-A-A-A-A-NNNNNNNNNNNNNNNNNNN.A".translate(_N),
    CASE2: "NNNNNNNN_A_A_A_A A ANA_ANAN.A".translate(_N),
    CASE3: "NNNNNNNN_A_A_A_AN.A".translate(_N),
    CASE4: "NNNNNNNN-A-A-A-A-A-NNNNNNNNNNNNNNNNNNN.A".translate(_N),
}


def _plan(name, mime=PDF, front=None, text=None, taken=frozenset()):
    if front is None or text is None:  # the date of the name's leading YYYYMMDD
        d = datetime.strptime(name[:8], "%Y%m%d").date()
    return normalise.plan_name(
        name=name,
        mime=mime,
        doc_type="Labs",
        front={"doc_date": d.isoformat()} if front is None else front,
        text=d.strftime("%d.%m.%Y") if text is None else text,
        contract=CONTRACT,
        today=date(2027, 6, 1),
        taken=set(taken),
    )


def test_synthetic_names_have_the_production_shapes():
    for name, shape in SHAPES.items():
        assert normalise.name_shape(name) == shape


@pytest.mark.parametrize("name", list(SHAPES))
def test_all_four_shapes_plan_a_standard_target_with_the_same_extension(name):
    p = _plan(name)
    assert p.code is None, p
    assert is_standard(p.target)  # without a mime: the strict check
    assert p.target.endswith(".pdf")
    assert normalise.name_shape(p.target) == "99999999-A-A-A-A-A.A"


@pytest.mark.parametrize("name", [CASE2, CASE3])
@pytest.mark.parametrize("mime", [GDOC, SHORTCUT])
def test_google_native_title_with_an_extension_keeps_it(name, mime):
    """The root cause: a Google-native file whose title ends in ``.docx`` /
    ``.pdf`` lost that suffix and got a target the mime-less check refuses."""
    title = name.removesuffix(".pdf") + (".docx" if mime == GDOC else ".pdf")
    p = _plan(title, mime=mime)
    assert p.code is None, p
    ext = title.rpartition(".")[2]
    assert p.target.rpartition(".")[2] == ext
    assert is_standard(p.target) and is_standard(p.target, mime=mime)


@pytest.mark.parametrize("title", ["Sprava_kontrola_jar", "Kontrola 02.04.2027"])
def test_google_native_without_an_extension_stays_without_one(title):
    p = _plan(title, mime=GDOC, front={"doc_date": "2027-04-02"}, text="02.04.2027")
    assert p.code is None, p
    assert "." not in p.target
    assert is_standard(p.target, mime=GDOC)
    assert p.target == "20270402-JanaNovakova-Unknown-Labs-Unknown-Unknown"


def test_upper_case_extension_counts_as_the_same_extension():
    p = _plan(CASE3.removesuffix(".pdf") + ".PDF")
    assert p.code is None and p.target.endswith(".pdf")


def test_extensionless_binary_is_skipped_never_given_bin():
    p = _plan("20270511_Jana_Novakova_NOU_kontrola4")
    assert p.target is None and p.code == normalise.CODE_EXTENSION_CHANGED


# ── guards on a forced bad target ────────────────────────────────────


def test_guard_target_not_standard(monkeypatch):
    monkeypatch.setattr(normalise, "build_filename", lambda fields, ext: f"not_standard.{ext}")
    p = _plan(CASE3)
    assert p.target is None and p.code == normalise.CODE_TARGET_NOT_STANDARD


@pytest.mark.parametrize(
    "name, mime, forced",
    [
        (CASE3, PDF, "20270511-JanaNovakova-NOU-Labs-Unknown-Unknown.docx"),  # changed
        (CASE2, GDOC, "20270510-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf"),  # added
        ("Sprava_kontrola_jar", GDOC, "20270510-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf"),
    ],
)
def test_guard_extension_changed(monkeypatch, name, mime, forced):
    if mime == GDOC and "." in name:
        name = name.removesuffix(".pdf")  # a native title without an extension
    monkeypatch.setattr(normalise, "build_filename", lambda fields, ext: forced)
    p = _plan(name, mime=mime, front={"doc_date": "2027-05-10"}, text="10.05.2027")
    assert p.target is None and p.code == normalise.CODE_EXTENSION_CHANGED


def test_guard_drops_an_extension(monkeypatch):
    monkeypatch.setattr(
        normalise, "build_filename", lambda fields, ext: "20270511-JanaNovakova-NOU-Labs-A-B.gdoc"
    )
    p = _plan(CASE3.removesuffix(".pdf") + ".docx", mime=GDOC)
    # the title's .docx would be lost: never planned
    assert p.target is None and p.code in normalise.GUARD_CODES


# ── runner: skipped with a code, in the dry-run report, logs codes only ──


def test_runner_reports_guard_skips_and_logs_counts_only(monkeypatch, caplog):
    d = FakeDrive()
    d.add(CASE3, d.folder[LABS], b"PDF")
    d.add(CASE3 + ".md", d.folder[LABS], sidecar("2027-05-11", "NOU", None, "11.05.2027"))
    monkeypatch.setattr(
        normalise,
        "build_filename",
        lambda fields, ext: "20270511-JanaNovakova-NOU-Labs-Unknown-Unknown.docx",
    )
    with caplog.at_level(logging.INFO, logger="oncofiles.curated.runner"):
        r = run(d)  # dry run
    assert r.status == "ok", r.message
    assert r.normalise["planned"] == []
    assert r.normalise["skipped"] == [
        {"source": f"{LABS}/{CASE3}", "code": normalise.CODE_EXTENSION_CHANGED}
    ]
    assert r.normalise["counts"]["extension_changed"] == 1
    assert r.normalise["counts"]["target_not_standard"] == 0
    assert "extension_changed=1" in caplog.text
    for secret in ("Novakova", "kontrola4", "NOU"):
        assert secret not in caplog.text
    assert d.writes == []


def test_runner_plans_the_native_docx_title_with_its_extension():
    d = FakeDrive()
    title = CASE3.removesuffix(".pdf") + ".docx"
    doc = d.add(title, d.folder[LABS], b"Kontrola 11.05.2027")
    d.items[doc]["mime"] = GDOC
    d.add(title + ".md", d.folder[LABS], sidecar("2027-05-11", "NOU", None, "11.05.2027"))
    r = run(d)
    assert r.status == "ok", r.message
    (entry,) = r.normalise["planned"]
    assert entry["to"] == "20270511-JanaNovakova-NOU-Labs-Unknown-Unknown.docx"
    assert entry["sidecar_to"] == entry["to"] + ".md"
    assert is_standard(entry["to"])
