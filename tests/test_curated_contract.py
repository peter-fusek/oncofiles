"""Lock tests for the README contract parser (#562). Synthetic fixtures only."""

import unicodedata
from pathlib import Path

import pytest

from oncofiles.curated.contract import ContractError, folder_key, nfc, parse_contract

LABS = "`labs — laboratórne výsledky` | `Labs`"
FIXTURE = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()


def test_parses_patient_version_and_folders():
    c = parse_contract(FIXTURE)
    assert c.patient_token == "JanaNovakova"
    assert c.patient_display == "Jana Nováková"
    assert c.version == "1.0"
    assert set(c.doc_types) == {"Report", "Labs", "Imaging", "Treatment", "Other"}
    assert c.folder_for("Labs") == "labs — laboratórne výsledky"
    assert c.inbox.startswith("00-inbox") and c.logs.startswith("98-logs")
    assert c.duplicates.startswith("99-duplicates")


def test_birth_date_extracted_once():
    assert parse_contract(FIXTURE).birth_dates == {"19700203"}


def test_vocab_tokens_include_institutions_and_doctors():
    tokens = parse_contract(FIXTURE).known_tokens
    assert {"NOU", "SampleHospital", "Kovacova", "Hruskova"} <= tokens


def test_nfd_readme_is_normalised():
    c = parse_contract(unicodedata.normalize("NFD", FIXTURE))
    assert "labs — laboratórne výsledky" in c.folders  # NFC key


def test_folder_key():
    assert folder_key("labs — laboratórne výsledky") == "labs"
    assert folder_key(unicodedata.normalize("NFD", "00-inbox — nové skeny")) == "00-inbox"
    assert nfc("a") == "a"


@pytest.mark.parametrize(
    "mutate,msg",
    [
        (lambda t: t.replace("(`JanaNovakova`)", ""), "Patient"),
        (lambda t: t.replace("## 2.", "## 9."), "folder-structure"),
        (lambda t: t.replace(LABS, "`report — x` | `Report`"), "two folders"),
        (lambda t: t.replace(LABS, "`lab — x` | `Labs`"), "does not match"),
        (lambda t: t.replace("| `other", "| " + LABS + " | dup |\n| `other"), "twice"),
        (lambda t: t.replace("| `00-inbox — nové skeny` | — | Drop zone |\n", ""), "00-inbox"),
        (lambda t: t.replace("| `other — ostatné` | `Other` | Fallback |\n", ""), "Other"),
    ],
)
def test_broken_contract_aborts(mutate, msg):
    with pytest.raises(ContractError, match=msg):
        parse_contract(mutate(FIXTURE))
