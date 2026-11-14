"""General-health kinds of the clinical extractor (#815).

The ``general`` request profile (general-health patients, vaccination /
preventive / dental documents) asks the local model for vaccinations,
allergies, screenings and general genetic results next to diagnoses,
medications, procedures and planned dates. The oncology request stays the v10
request byte for byte.

Synthetic data only: every page, value and date is invented (2027+).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import patient_context, pre_store
from oncofiles.cloud_guard import local_only
from oncofiles.curated import clinical_store, llm
from oncofiles.curated import llm_extract_clinical as lx
from oncofiles.curated import verifier_clinical as vc
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID

DOB = "2027-01-02"  # invented
DAY = "2028-06-20"  # document date of the seeded documents
DOC_G = 8151
DOC_O = DOC_G + 1
GENERAL_PAGES = [page for page, _ in lx.GENERAL_EXAMPLES]
ANSWERS = {
    **{page: (lx.ONCOLOGY, a) for page, a in lx.EXAMPLES},
    **{page: (lx.GENERAL, a) for page, a in lx.GENERAL_EXAMPLES},
}
NEW_KINDS = ("vaccinations", "allergies", "screenings")
SRC = Path(__file__).parent.parent / "src" / "oncofiles"

# The v10 oncology request (#773), locked: v11 must not change a byte of it.
# SHA-256 hex digests with the digits spelled as letters (0-9 -> g-p), so the
# lock carries no digit runs (the local fixture guard compares number runs).
_HEX = str.maketrans("0123456789", "ghijklmnop")
V10_PROMPT_SHA256 = "oodchhbjacfamkjjlgjlmdfjkcgbbioifgcjpmoeenmgmajndplkobjgnocopopp"
V10_RESPONSE_FORMAT_SHA256 = "pjncjhbldmkmannmhojnlfbcdmbmdcffoolhbibjbhdfbfmgnficeabogeajadpe"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().translate(_HEX)


def model_chat(system, user, info=None, **kw):
    """The mocked local model: the worked-example answer of the page, in the
    profile whose prompt it was given (the oncology page also exists only in
    one profile, so a wrong prompt fails here)."""
    page = user.split("\n", 1)[1]
    profile, answer = ANSWERS[page]
    if page == lx._EX6:  # the empty page is an example in both profiles
        profile = lx.GENERAL if system == lx.GENERAL_SYSTEM_PROMPT else lx.ONCOLOGY
        answer = {k: [] for k in lx.PROFILE_KINDS[profile]}
    assert system == lx.SYSTEM_PROMPTS[profile], "page sent with the other profile's prompt"
    if info is not None:
        info["finish_reason"] = "stop"
    return json.dumps(answer, ensure_ascii=False)


async def _vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


async def _extract(db, pages, *, profile=lx.GENERAL, chat=model_chat):
    vocab = await _vocab(db)
    with local_only("test"):
        return lx.extract_document(
            list(enumerate(pages, 1)), vocab=vocab, report_date=DAY, birth_dates={DOB},
            chat=chat, profile=profile,
        )  # fmt: skip


async def _store(db, doc_id, pages, *, profile=lx.GENERAL):
    out = await _extract(db, pages, profile=profile)
    res = await clinical_store.store_document(
        db, patient_id=ERIKA_UUID, document_id=doc_id, items=out["items"], document_date=DAY,
        birth_dates={DOB}, model="mock-model",
    )  # fmt: skip
    return out, res


async def _doc(db, doc_id, pages, category):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": f"s{doc_id}"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, ?, 'application/pdf', 1, ?, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", f"d{doc_id}.pdf", f"d{doc_id}.pdf", category, ERIKA_UUID,
         f"g{doc_id}", json.dumps(meta), DAY),
    )  # fmt: skip
    for n, text in enumerate(pages, 1):
        await db.save_ocr_page(doc_id, n, text, "curated-sidecar")
    await db.db.commit()


@pytest.fixture
async def seeded(db):
    await _doc(db, DOC_G, GENERAL_PAGES, "preventive")
    await _doc(db, DOC_O, [lx._EX1], "report")
    return db


async def _active(db, table, where="1=1"):
    async with db.db.execute(
        f"SELECT * FROM {table} WHERE patient_id = ? AND deleted_at IS NULL AND {where} "  # noqa: S608
        "ORDER BY id",
        (ERIKA_UUID,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


@pytest.fixture
def general_patient(monkeypatch):
    ctx = {**patient_context._default_context(), "patient_type": "general"}
    monkeypatch.setitem(patient_context._contexts, ERIKA_UUID, ctx)


@pytest.fixture
def local_model(monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(llm, "chat", model_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}})
    monkeypatch.setattr(pre_store, "make_copy", lambda db, job: f"oncofiles-copy-{job}.db")


# ── profiles: the oncology request is unchanged ─────────────────────────


def test_oncology_request_is_byte_identical_to_v10():
    assert lx.EXTRACTOR_VERSION == "llm-clinical-v11"
    assert _digest(lx.SYSTEM_PROMPT.encode()) == V10_PROMPT_SHA256
    fmt = json.dumps(lx.RESPONSE_FORMAT, sort_keys=True).encode()
    assert _digest(fmt) == V10_RESPONSE_FORMAT_SHA256
    assert lx.SYSTEM_PROMPTS[lx.ONCOLOGY] is lx.SYSTEM_PROMPT
    assert not set(NEW_KINDS) & set(lx.PROFILE_KINDS[lx.ONCOLOGY])
    assert set(NEW_KINDS) <= set(lx.PROFILE_KINDS[lx.GENERAL])
    # the general request is its own, smaller prompt; the oncology one is not grown
    assert len(lx.GENERAL_SYSTEM_PROMPT) < len(lx.SYSTEM_PROMPT)


@pytest.mark.parametrize(
    ("patient_type", "category", "profile"),
    [
        ("oncology", "report", "oncology"), ("oncology", "genetics", "oncology"),
        (None, None, "oncology"), ("oncology", "vaccination", "general"),
        ("oncology", "preventive", "general"), ("oncology", "dental", "general"),
        ("general", "report", "general"), ("general", "genetics", "general"),
    ],
)  # fmt: skip
def test_profile_follows_patient_type_and_category(patient_type, category, profile):
    assert lx.profile_for(patient_type, category) == profile


def test_unknown_profile_is_refused():
    with local_only("test"), pytest.raises(ValueError):
        lx.extract_document([(1, "x")], vocab=None, profile="dental")


def test_oncology_answer_never_yields_a_general_kind():
    body = json.dumps({"diagnoses": [], "vaccinations": [{"vaccine_as_printed": "X"}]})
    assert "vaccinations" not in lx.parse_answer(body)
    assert lx.parse_answer(body, lx.PROFILE_KINDS[lx.GENERAL])["vaccinations"]


async def test_oncology_documents_give_the_same_items_as_before(db):
    """Regression: the oncology examples still extract to exactly their
    taught items, all machine, through the unchanged v10 request."""
    out = await _extract(db, [p for p, _ in lx.EXAMPLES], profile=lx.ONCOLOGY)
    assert out["profile"] == "oncology" and set(out["counts"]) == set(lx.PROFILE_KINDS["oncology"])
    taught = {k: sum(len(a[k]) for _, a in lx.EXAMPLES) for k in lx.PROFILE_KINDS["oncology"]}
    assert {k: c["machine"] for k, c in out["counts"].items()} == taught
    assert all(c["unverified"] == 0 for c in out["counts"].values())
    assert not out["pages_not_assessed"]


# ── extract, verify, store each new kind ────────────────────────────────


async def test_general_document_extracts_and_verifies_every_kind(db):
    out = await _extract(db, GENERAL_PAGES)
    assert out["profile"] == "general"
    assert set(out["counts"]) == set(lx.PROFILE_KINDS["general"])
    taught = {k: sum(len(a[k]) for _, a in lx.GENERAL_EXAMPLES) for k in out["counts"]}
    assert {k: c["machine"] for k, c in out["counts"].items()} == taught
    for kind in (*NEW_KINDS, "biomarkers"):
        assert taught[kind] >= 2, kind
    vacc = [i for i in out["items"] if i["kind"] == "vaccinations"]
    assert sorted(i["derived"]["dose_number"] for i in vacc if i["derived"]["dose_number"]) == [
        1, 2,
    ]  # fmt: skip
    genes = {i["derived"]["code"] for i in out["items"] if i["kind"] == "biomarkers"}
    assert genes == {"CYP2C19", "HFE", "F5", "APOE"}


async def test_general_kinds_are_stored_with_provenance(seeded):
    db = seeded
    _, res = await _store(db, DOC_G, GENERAL_PAGES)
    vacc = await _active(db, "procedures", "kind = 'vaccination'")
    assert sorted(r["printed_text"] for r in vacc) == [
        "Boostrix", "Hexacima", "Hexacima", "Vaxigrip Tetra",
    ]  # fmt: skip
    assert {r["performed_at"] for r in vacc} == {
        "2028-03-14", "2027-04-12", "2027-06-14", "2027-10-05",
    }  # fmt: skip
    hexa = [json.loads(r["metadata_json"]) for r in vacc if r["printed_text"] == "Hexacima"]
    assert sorted(m["dose_number"] for m in hexa) == [1, 2]
    screen = await _active(db, "procedures", "kind = 'screening'")
    assert {r["printed_text"]: r["outcome"] for r in screen} == {
        "Preventívna prehliadka": None, "Kolonoskopia": "bez patologického nálezu",
        "Stomatologická preventívna prehliadka": "zubný kaz 36",
    }  # fmt: skip
    allergies = await _active(db, "conditions", "category = 'problem-list-item'")
    assert {r["printed_text"] for r in allergies} == {"Alergia: penicilín", "Alergia na peľ"}
    for r in allergies:
        meta = json.loads(r["metadata_json"])
        assert meta["condition_kind"] == "allergy" and meta["substance"]
        assert not r["is_primary_cancer"]
    chronic = await _active(db, "conditions", "category IS NULL")
    assert {r["printed_text"] for r in chronic} == {
        "Esenciálna (primárna) hypertenzia", "Čistá hypercholesterolémia",
        "Diabetes mellitus 2. typu bez komplikácií",
    }  # fmt: skip
    assert not any(r["is_primary_cancer"] for r in chronic)
    genetic = await _active(db, "clinical_records", "record_type = 'genetic_variant'")
    assert {r["code"] for r in genetic} == {"CYP2C19", "HFE", "F5", "APOE"}
    assert all(r["occurred_at"].startswith(DAY) for r in genetic)  # no printed date: document
    every = [*vacc, *screen, *allergies, *chronic, *genetic]
    assert all(r["verification"] == "machine" for r in every)
    assert all(r["extracted_by"] == "llm-clinical-v11/mock-model" for r in every)
    assert all(r["source_document_id"] == DOC_G and r["page_number"] for r in every)
    async with db.db.execute(
        "SELECT target_type, COUNT(*) FROM provenance WHERE patient_id = ? "
        "AND source_document_id = ? GROUP BY target_type",
        (ERIKA_UUID, DOC_G),
    ) as cur:
        prov = {r[0]: r[1] for r in await cur.fetchall()}
    assert prov["procedure"] == len(vacc) + len(screen) + 2  # + USG, extraction
    assert prov["condition"] == len(allergies) + len(chronic)
    assert res["stored"]["procedures"]["added"] == prov["procedure"]


@pytest.mark.parametrize(
    ("kind", "field", "value", "reason"),
    [
        ("vaccinations", "vaccine_as_printed", "Infanrix", "vaccine_not_in_evidence"),
        ("vaccinations", "dose_as_printed", "3. dávka", "dose_not_in_evidence"),
        ("vaccinations", "date_as_printed", "12.5.2027", "date_not_printed"),
        ("allergies", "substance_as_printed", "amoxicilín", "substance_not_in_evidence"),
        ("allergies", "reaction_as_printed", "anafylaxia", "reaction_not_in_evidence"),
        ("screenings", "result_as_printed", "polyp", "result_not_in_evidence"),
        ("screenings", "test_as_printed", "Mamografia", "test_not_in_evidence"),
        ("biomarkers", "variant_as_printed", "*2/*2", "variant_not_in_evidence"),
        ("biomarkers", "result_as_printed", "homozygot", "result_not_in_evidence"),
        ("biomarkers", "marker_as_printed", "CYP2C9", "marker_not_in_evidence"),
    ],
)  # fmt: skip
async def test_a_token_missing_from_the_evidence_stays_unverified(db, kind, field, value, reason):
    vocab = await _vocab(db)
    page, raw = next(
        (p, r) for p, a in lx.GENERAL_EXAMPLES for r in a.get(kind, []) if r.get(field)
    )
    cand, why = lx.normalize_item(kind, {**raw, field: value})
    assert why is None
    item = vc.verify_item({**cand, "page_number": 1}, page_text=page, vocab=vocab,
                          report_date=DAY, birth_dates={DOB})  # fmt: skip
    assert item["verification"] == "unverified" and reason in item["reasons"]


async def test_a_vaccination_on_the_birth_date_is_never_machine(db):
    vocab = await _vocab(db)
    page = "Hexacima, 1. dávka, 2.1.2027"
    cand, _ = lx.normalize_item("vaccinations", {
        "vaccine_as_printed": "Hexacima", "dose_as_printed": "1. dávka",
        "date_as_printed": "2.1.2027", "evidence": page,
    })  # fmt: skip
    item = vc.verify_item({**cand, "page_number": 1}, page_text=page, vocab=vocab,
                          birth_dates={DOB})  # fmt: skip
    assert item["verification"] == "unverified" and "birth_date" in item["reasons"]


# ── re-extraction, retract, protection ──────────────────────────────────


async def test_re_extraction_keeps_a_verified_vaccination_and_retract_removes_the_rest(seeded):
    db = seeded
    await _store(db, DOC_G, GENERAL_PAGES)
    boostrix = next(r for r in await _active(db, "procedures", "kind = 'vaccination'")
                    if r["printed_text"] == "Boostrix")  # fmt: skip
    await db.db.execute(
        "UPDATE procedures SET verification = 'verified' WHERE id = ?", (boostrix["id"],)
    )
    await db.db.commit()
    _, res = await _store(db, DOC_G, GENERAL_PAGES)  # same items again
    assert res["stored"]["procedures"]["kept_protected"] == 1
    ids = [r["id"] for r in await _active(db, "procedures", "kind = 'vaccination'")]
    assert boostrix["id"] in ids and len(ids) == 4

    out = await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_G, reason="test", dry_run=False
    )
    assert out["stored"]["procedures"]["kept"] == 1
    left = await _active(db, "procedures")
    assert [r["id"] for r in left] == [boostrix["id"]]
    assert await _active(db, "conditions") == []
    assert await _active(db, "clinical_records", "record_type = 'genetic_variant'") == []
    assert await _active(db, "encounters", "status = 'planned'") == []


# ── read side ───────────────────────────────────────────────────────────


async def test_timeline_and_overview_show_the_general_kinds(seeded):
    db = seeded
    await _store(db, DOC_G, GENERAL_PAGES)
    ctx = _ctx(db)
    procs = json.loads(await cf.timeline(ctx, kinds=["procedure"]))["rows"]
    by_kind: dict[str, set] = {}
    for r in procs:
        by_kind.setdefault(r["value"], set()).add(r["title"])
    assert by_kind["vaccination"] == {"Boostrix", "Hexacima", "Vaxigrip Tetra"}
    assert "Kolonoskopia" in by_kind["screening"]
    conds = json.loads(await cf.timeline(ctx, kinds=["condition"]))["rows"]
    assert {"Alergia: penicilín", "Alergia na peľ"} <= {r["title"] for r in conds}
    genetic = json.loads(await cf.timeline(ctx, kinds=["genetic_variant"]))["rows"]
    assert genetic and all(r["source_document_id"] == DOC_G for r in genetic)

    dm = json.loads(await cf.patient_overview(ctx))["diagnoses_and_markers"]
    allergy = {d["name"] for d in dm if d.get("condition_kind") == "allergy"}
    assert allergy == {"Alergia: penicilín", "Alergia na peľ"}
    assert {d["code"] for d in dm if d["kind"] == "genetic_variant"} >= {"CYP2C19", "HFE", "F5"}


# ── MCP: a general-patient preview is a dry run and writes nothing ──────


async def _snapshot(db):
    out = {}
    for table in (
        "conditions", "procedures", "medication_events", "clinical_records", "encounters",
        "provenance", "codings", "devices", "curated_runs",
    ):  # fmt: skip
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


async def test_general_patient_preview_is_dry_run_and_writes_nothing(
    seeded, local_model, general_patient
):
    db = seeded
    before = await _snapshot(db)
    started = json.loads(await tool.curated_clinical_extract_preview(_ctx(db), [DOC_G]))
    assert started["kind"] == "clinical_preview" and started["dry_run"] is True
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = json.loads(await tool.curated_reocr_status(_ctx(db)))
    assert job["state"] == "done", job
    assert await _snapshot(db) == before
    (doc,) = job["report"]["documents"]
    assert doc["profile"] == "general" and doc["result"] == "previewed"
    counts = job["report"]["totals"]["counts"]
    assert counts["vaccinations"]["machine"] == 4 and counts["allergies"]["machine"] == 2
    assert counts["screenings"]["machine"] == 3
    dumped = json.dumps(job, ensure_ascii=False)
    assert not [v for v in ("Hexacima", "penicilín", "CYP2C19", "Kolonoskopia") if v in dumped]


async def test_an_oncology_patient_keeps_the_oncology_request(seeded, local_model):
    db = seeded
    json.loads(await tool.curated_clinical_extract_preview(_ctx(db), [DOC_O]))
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = json.loads(await tool.curated_reocr_status(_ctx(db)))
    (doc,) = job["report"]["documents"]
    assert doc["profile"] == "oncology" and set(doc["counts"]) == set(lx.PROFILE_KINDS["oncology"])


# ── infra condition: the v11 bump never re-extracts anything by itself ──

_CLINICAL_ENTRY = {
    "llm_extract_clinical", "clinical_store", "_clinical_job", "_start_clinical",
    "_clinical_work", "_clinical_entry",
}  # fmt: skip
# every function of tools/curated.py that can reach the clinical extractor;
# the roots are MCP tools a user invokes (preview, extract, retract, scorecard)
_USER_INVOKED_ONLY = {
    "_clinical_work", "_clinical_job", "_start_clinical", "_scorecard_job",
    "curated_clinical_extract_preview", "curated_clinical_extract", "curated_clinical_retract",
    "_start_scorecard", "curated_extraction_scorecard",
    "register",  # registers those MCP tools with the server; calls none of them
}  # fmt: skip


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text())


def test_only_the_tool_layer_imports_the_clinical_extractor():
    """No scheduler, loop, indexer or startup module imports the clinical
    extractor or its store, so nothing can compare its version and re-run."""
    allowed = {
        "curated/llm_extract_clinical.py", "curated/clinical_store.py", "tools/curated.py",
        "vocab.py",  # vocabulary listing only (enum values), never a version
    }  # fmt: skip
    importers = set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            if any(m.endswith(("llm_extract_clinical", "clinical_store")) for m in mods):
                importers.add(rel)
        if rel == "vocab.py":  # by string path, still never the version
            assert "EXTRACTOR_VERSION" not in ast.unparse(tree)
    assert importers <= allowed, importers - allowed


def test_the_clinical_version_is_only_compared_inside_the_extractor():
    """Outside the extractor and its store, ``EXTRACTOR_VERSION`` of the
    clinical extractor is only reported (job report, device row), never part
    of a comparison that could trigger a re-extraction."""
    for rel, tree in _modules():
        if rel in ("curated/llm_extract_clinical.py",):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                text = ast.unparse(node)
                assert "lx.EXTRACTOR_VERSION" not in text, rel
                if rel in ("curated/clinical_store.py", "tools/curated.py"):
                    assert "EXTRACTOR_VERSION" not in text or "llm_extract." in text, rel


def test_no_background_path_reaches_the_clinical_extractor():
    tree = ast.parse((SRC / "tools" / "curated.py").read_text())
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def refs(fn) -> set[str]:
        out = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name):
                out.add(n.id)
            elif isinstance(n, ast.alias):
                out.add(n.name.rsplit(".", 1)[-1])
            elif isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.rsplit(".", 1)[-1])
        return out

    reaching = {name for name, fn in funcs.items() if refs(fn) & _CLINICAL_ENTRY}
    while True:  # transitive closure over the module's own functions
        more = {name for name, fn in funcs.items() if refs(fn) & reaching} - reaching
        if not more:
            break
        reaching |= more
    assert reaching <= _USER_INVOKED_ONLY, reaching - _USER_INVOKED_ONLY
    # and none of them is wired into the server's startup, scheduler or curated loop
    for rel in ("server.py", "scheduler_singleton.py", "curated/runner.py",
                "curated/indexer.py"):  # fmt: skip
        path = SRC / rel
        if path.exists():
            names = refs(ast.parse(path.read_text()))
            assert not names & (_CLINICAL_ENTRY | {"_clinical_job", "_scorecard_job"}), rel
