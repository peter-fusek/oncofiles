"""Data rights of a patient: Drive disconnect, export, and the table registry (#856).

Maintainer decisions 2026-11-19: export and erasure requests are handled within
30 days (the figure ``/privacy`` and ``/terms`` state), and ``patient_consents``
is erased together with the patient.

**The table registry** (:data:`REGISTRY`) is the ONE list of where a patient's
data lives in the database. Every entry is erased by
``patient_erasure.erase_patient`` (in list order, children first, ``patients``
last) and, when ``export`` is set, written to the export ZIP. Every other table
of the migrated schema sits in :data:`NOT_PATIENT_DATA` with the reason it holds
no patient's data. ``tests/test_856_data_rights.py`` builds the schema from
``migrations/`` and fails on a table in neither list, and on a table with a
patient reference (``patient_id``, ``target_patient_id``, an FK to a patient
table) that is not in the registry: a new table is unclassified until someone
decides what an export and an erasure do with it.

**Export** (:func:`export_stream`): a ZIP streamed in chunks (bounded memory:
one page of rows plus one chunk of compressed output; one held original at a
time), JSON Lines per table, a ``manifest.json`` with counts and a bilingual
``README.md``. Rows come from the registry's own ``where`` (the patient's rows
only, SQL-filtered) and never carry credentials, hashes of anyone, encrypted
blobs or other people's e-mail addresses (:func:`exported_columns`); it does
include the patient's own mail and calendar entries (``email_entries``,
``calendar_entries``) with the addresses in them (senders, recipients,
attendees), because they are the patient's own data. The
originals are in the patient's own Google Drive and are not copied, except a
trial's held files (``holding:`` ids), which exist nowhere else.

**Drive disconnect** (:func:`disconnect_drive`): the caregiver's own button.
Deletes the stored token row (which also holds the folder link), switches
curated processing off, forgets the in-memory curated state and then revokes
the grant at Google unless another patient uses the same grant or account (the
revoke runs in a ``finally``, so a failure of the curated switch never skips it).
Refused while any curated work of the patient runs. Drive files are untouched.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

EXPORT_FORMAT = "oncofiles-export-v1"
EXPORT_PAGE_ROWS = 200
EXPORT_CHUNK_BYTES = 256 * 1024

_P = ":pid"
_DOCS = f"SELECT id FROM documents WHERE patient_id = {_P}"
_RECORDS = f"SELECT id FROM clinical_records WHERE patient_id = {_P}"


@dataclass(frozen=True)
class TableRule:
    """Where ONE table keeps a patient's rows and what export / erasure do."""

    table: str
    # SQL condition (named parameter :pid) selecting the rows an erasure deletes:
    # every row that touches the patient, also one that links to someone else.
    erase_scope: str
    export: bool
    reason: str
    exclude: frozenset[str] = field(default_factory=frozenset)  # extra columns left out
    only: tuple[str, ...] | None = None  # export exactly these columns
    # The rows the export may hand out; None = erase_scope. Must be narrower
    # wherever a row can point at another patient's row (e.g. a cross-reference
    # between two documents): the export only includes rows wholly the patient's.
    export_scope: str | None = None

    @property
    def export_where(self) -> str:
        return self.export_scope or self.erase_scope


def _direct(table: str, export: bool, reason: str, **kw) -> TableRule:
    return TableRule(table, f"patient_id = {_P}", export, reason, **kw)


# Erasure order = list order: rows that point at others first, ``patients`` last
# (the erasure also defers FK checks to its commit, so cycles such as
# documents <-> encounters are fine). Every entry is erased.
REGISTRY: tuple[TableRule, ...] = (
    # children reached through a parent row
    TableRule(
        "clinical_record_audit",
        f"record_id IN ({_RECORDS})",
        True,
        "change history of the patient's facts",
    ),
    TableRule(
        "clinical_record_notes",
        f"record_id IN ({_RECORDS})",
        True,
        "notes on the patient's facts",
    ),
    TableRule("document_pages", f"document_id IN ({_DOCS})", True, "recognised text per page"),
    TableRule(
        "document_cross_references",
        f"source_document_id IN ({_DOCS}) OR target_document_id IN ({_DOCS})",
        True,
        "links between the patient's documents",
        # export: BOTH ends the patient's (a stray link to another patient's
        # document never hands out that document's id); erasure: EITHER end
        export_scope=f"source_document_id IN ({_DOCS}) AND target_document_id IN ({_DOCS})",
    ),
    TableRule("lab_values", f"document_id IN ({_DOCS})", True, "legacy lab values"),
    TableRule(
        "prompt_log",
        # also rows with a legacy patient_id ('' / default) that reference one of
        # the patient's documents: their prompts were built from its page text.
        # Before ``documents`` in this list, or the subquery finds nothing.
        f"patient_id = {_P} OR document_id IN ({_DOCS})",
        False,
        "cloud-AI call telemetry (prompts built from the page text, which is exported)",
        # never exported; should that change, only the patient's own rows
        export_scope=f"patient_id = {_P}",
    ),
    TableRule(
        "inbound_claimed_jobs",
        f"bucket IN (SELECT token_hash FROM inbound_addresses WHERE patient_id = {_P})",
        False,
        "unverified mail queued for the patient's address: encrypted payload",
    ),
    TableRule(
        "dashboard_access_log",
        f"target_patient_id = {_P}",
        False,
        "dashboard access log about the patient: other callers' keyed hashes",
    ),
    # inbound e-mail (#950)
    _direct("inbound_quarantine_tokens", False, "single-use action link hashes"),
    _direct("inbound_quarantine", False, "encrypted quarantine items (files erased too)"),
    _direct("inbound_jobs", False, "encrypted mail queue"),
    _direct("inbound_addresses", False, "document e-mail address token hashes (credential)"),
    _direct("trial_accounts", False, "welcome@ trial bookkeeping, keyed sender hash"),
    # facts model (#603, #711)
    _direct("provenance", True, "where each fact came from", exclude=frozenset({"agent_user"})),
    _direct("codings", True, "codes attached to the patient's facts"),
    _direct("identifiers", True, "identifiers attached to the patient's entities"),
    _direct("links", True, "links between the patient's entities"),
    _direct("medication_events", True, "medications and doses"),
    _direct("treatment_cycles", True, "treatment cycles"),
    _direct("treatment_courses", True, "treatment courses"),
    _direct("cancer_stages", True, "cancer stages"),
    _direct("procedures", True, "procedures, vaccinations, screenings"),
    _direct("clinical_records", True, "facts: lab values, markers, findings"),
    _direct("reports", True, "diagnostic reports"),
    _direct("specimens", True, "specimens"),
    _direct("conditions", True, "diagnoses, allergies"),
    _direct("clinical_analyses", True, "stored analyses of the patient's facts"),
    _direct("fact_rereads", True, "verify-only re-read results"),
    _direct("treatment_events", True, "treatment events"),
    _direct("document_references", True, "event references to documents"),
    _direct("calendar_entries", True, "calendar entries"),
    _direct("email_entries", True, "e-mail entries"),
    _direct("encounters", True, "visits and planned appointments"),
    _direct("practitioners", True, "the patient's doctors (patient-scoped)"),
    _direct("documents", True, "the document index (originals stay in Drive)"),
    # the patient's own notes and state
    _direct("conversation_entries", True, "session diary"),
    _direct("research_entries", True, "research notes"),
    _direct("agent_state", True, "assistant state"),
    _direct("patient_context", True, "patient context (profile, treatment)"),
    _direct("curated_runs", True, "curated pipeline runs: codes and counts"),
    _direct("curated_settings", True, "curated processing switch"),
    _direct("sync_history", True, "legacy sync runs", exclude=frozenset({"error_message"})),
    _direct("onboarding_events", True, "onboarding milestones"),
    _direct(
        "activity_log",
        True,
        "access log of AI assistant calls",
        only=("created_at", "tool_name", "status", "agent_id", "auth_method", "duration_ms"),
    ),
    _direct("patient_selection", False, "which patient an account last selected (UI state)"),
    _direct("patient_tokens", False, "bearer token hashes (credential)"),
    _direct("oauth_tokens", False, "Google access / refresh token (credential)"),
    # decision 2026-11-19: the consent row is erased with the patient
    _direct("patient_consents", True, "consent record (version, time, kind)"),
    _direct("patients", True, "the patient profile", exclude=frozenset({"tier_notes"})),
)

# Tables of the migrated schema that hold no patient's rows. May only grow with a reason.
NOT_PATIENT_DATA: dict[str, str] = {
    "departments": "shared directory of hospital departments",
    "organizations": "shared directory of institutions",
    "devices": "shared list of software / models that extracted facts",
    "fact_codes": "shared vocabulary of fact codes",
    "mcp_oauth_clients": "registered MCP OAuth apps",
    "mcp_oauth_tokens": "MCP connector tokens of a Google account, not of a patient",
    "newsletter_subscribers": "waitlist addresses, per e-mail, not per patient",
    "session_revocations": "dashboard logout denylist",
    "schema_migrations": "migration bookkeeping",
    "sqlite_sequence": "SQLite internal",
    "documents_fts": "full-text index kept in step with documents by triggers",
    "documents_fts_config": "FTS5 shadow table of documents_fts",
    "documents_fts_data": "FTS5 shadow table of documents_fts",
    "documents_fts_docsize": "FTS5 shadow table of documents_fts",
    "documents_fts_idx": "FTS5 shadow table of documents_fts",
    "conversation_entries_fts": "full-text index kept in step with the diary by triggers",
    "conversation_entries_fts_config": "FTS5 shadow table of conversation_entries_fts",
    "conversation_entries_fts_data": "FTS5 shadow table of conversation_entries_fts",
    "conversation_entries_fts_docsize": "FTS5 shadow table of conversation_entries_fts",
    "conversation_entries_fts_idx": "FTS5 shadow table of conversation_entries_fts",
}

_SECRET_EXACT = frozenset(
    {
        # who did something: a keyed hash or an identity of a caller, maybe not the patient's
        "caller_identity",
        "created_by",
        "updated_by",
        "deleted_by",
        "changed_by",
        "decided_by",
        # e-mail addresses of accounts (other caregivers, the Drive owner)
        "caregiver_email",
        "owner_email",
        "user_email",
    }
)
_SECRET_RE = re.compile(r"(_hash$|_enc$|token|secret|password)")
# Matches the pattern but is no secret: the doctor's surname as used in filenames.
_NOT_SECRET = frozenset({"surname_token"})


def is_secret_column(name: str) -> bool:
    """A column never exported from any table: credentials, keyed hashes,
    encrypted blobs, caller identities and account e-mail addresses."""
    if name in _NOT_SECRET:
        return False
    return name in _SECRET_EXACT or bool(_SECRET_RE.search(name))


def rule_for(table: str) -> TableRule:
    for rule in REGISTRY:
        if rule.table == table:
            return rule
    raise KeyError(table)


def _quote(identifier: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", identifier):
        raise ValueError("bad identifier")
    return f'"{identifier}"'


async def _columns(db, table: str) -> list[str]:
    async with db.db.execute(f"PRAGMA table_info({_quote(table)})") as cursor:
        return [r[1] for r in await cursor.fetchall()]


def exported_columns(rule: TableRule, columns: list[str]) -> list[str]:
    """The columns of ``rule.table`` that go into the export."""
    if not rule.export:
        return []
    keep = [c for c in columns if (rule.only is None or c in rule.only)]
    return [c for c in keep if c not in rule.exclude and not is_secret_column(c)]


async def count_rows(db, patient_id: str) -> dict[str, int]:
    """Rows per registry table for ONE patient (the erasure's dry run)."""
    out: dict[str, int] = {}
    for rule in REGISTRY:
        async with db.db.execute(
            f"SELECT COUNT(*) FROM {_quote(rule.table)} WHERE {rule.erase_scope}",
            {"pid": patient_id},
        ) as cursor:
            row = await cursor.fetchone()
        out[rule.table] = int(row[0] or 0) if row else 0
    return out


async def _iter_rows(db, rule: TableRule, columns: list[str], patient_id: str):
    """The patient's rows of one table, a page at a time (keyset on rowid)."""
    select = ", ".join(_quote(c) for c in columns)
    sql = (
        f"SELECT rowid AS _rid, {select} FROM {_quote(rule.table)} "
        f"WHERE ({rule.export_where}) AND rowid > :after ORDER BY rowid LIMIT {EXPORT_PAGE_ROWS}"
    )
    after = -1
    while True:
        async with db.db.execute(sql, {"pid": patient_id, "after": after}) as cursor:
            page = await cursor.fetchall()
        if not page:
            return
        for raw in page:
            after = raw[0]
            yield {c: raw[i + 1] for i, c in enumerate(columns)}
        if len(page) < EXPORT_PAGE_ROWS:
            return


def _json_value(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


class _Sink:
    """Unseekable file object for ``zipfile``: collects output to hand out in chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data) -> int:
        self._buf += data
        return len(data)

    def flush(self) -> None:
        return None

    def __len__(self) -> int:
        return len(self._buf)

    def take(self) -> bytes:
        out = bytes(self._buf)
        self._buf.clear()
        return out


README = """# Oncofiles export / Export z Oncofiles

## English

This archive holds what Oncofiles stores about ONE patient in its database, as
of the time in `manifest.json`.

- `tables/<table>.jsonl`: one JSON object per line, one file per table. The
  main ones: `patients` (profile), `patient_context`, `patient_consents`
  (the consent record), `documents` (the document index), `document_pages`
  (recognised text per page), `clinical_records` (values with their source
  document, page, printed evidence and verification state) and the other fact
  tables (`conditions`, `procedures`, `medication_events`, `treatment_*`,
  `encounters`, `provenance`, ...), `conversation_entries` (session diary) and
  `activity_log` (which AI assistant tools were called, when).
- Your original files are NOT in this archive: they stay in your own Google
  Drive folder, where Oncofiles left them. Documents held for a trial before a
  Drive was connected are included under `originals/`.
- Left out on purpose: access tokens and other credentials, keyed hashes,
  encrypted mail queues and quarantine items, and e-mail addresses of accounts.
  `manifest.json` lists the tables that are not exported and why.
- To have the data deleted, write to the address on https://oncofiles.com/privacy
  (handled within 30 days). Your files in Google Drive stay where they are.

## Slovensky

Tento archív obsahuje to, čo Oncofiles ukladá o JEDNOM pacientovi vo svojej
databáze, k času uvedenému v `manifest.json`.

- `tables/<tabuľka>.jsonl`: jeden JSON objekt na riadok, jeden súbor na
  tabuľku: profil, kontext, záznam o súhlase, zoznam dokumentov, rozpoznaný
  text strán, hodnoty s odkazom na zdrojový dokument, stranu, vytlačený text a
  stav overenia, ďalšie fakty, denník a záznam o volaniach AI asistentov.
- Vaše pôvodné súbory v archíve NIE SÚ: zostávajú vo vašom priečinku na Google
  Drive. Dokumenty uložené počas skúšobnej doby pred pripojením Drive sú v
  priečinku `originals/`.
- Zámerne vynechané: prístupové tokeny a iné prihlasovacie údaje, kľúčované
  hashe, zašifrované fronty e-mailov a karanténa a e-mailové adresy účtov.
- Vymazanie údajov si vyžiadate e-mailom na adresu na
  https://oncofiles.com/privacy (do 30 dní). Súbory na Google Drive zostanú.
"""


def _safe_member_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "")[:120].lstrip("._")
    return cleaned or "file"


async def export_stream(
    db, patient_id: str, *, now: datetime | None = None
) -> AsyncIterator[bytes]:
    """The patient's export ZIP, streamed in chunks (see the module docstring)."""
    from oncofiles import holding_store

    now = now or datetime.now(UTC)
    sink = _Sink()
    counts: dict[str, int] = {}
    originals = {"included": 0, "unavailable": 0}
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", README)
        for rule in REGISTRY:
            if not rule.export:
                continue
            columns = exported_columns(rule, await _columns(db, rule.table))
            n = 0
            with zf.open(f"tables/{rule.table}.jsonl", "w") as fh:
                async for row in _iter_rows(db, rule, columns, patient_id):
                    line = json.dumps(
                        {k: _json_value(v) for k, v in row.items()}, ensure_ascii=False, default=str
                    )
                    fh.write(line.encode("utf-8") + b"\n")
                    n += 1
                    if len(sink) >= EXPORT_CHUNK_BYTES:
                        yield sink.take()
            counts[rule.table] = n
            if len(sink):
                yield sink.take()
        # A trial's held originals exist nowhere else (no Drive yet): one at a time.
        async with db.db.execute(
            "SELECT id, file_id, filename FROM documents WHERE patient_id = ? "
            "AND file_id LIKE 'holding:%' ORDER BY id",
            (patient_id,),
        ) as cursor:
            held = [(r[0], r[1], r[2]) for r in await cursor.fetchall()]
        for doc_id, file_id, filename in held:
            try:
                data = await asyncio.to_thread(holding_store.read, file_id, patient_id)
            except Exception as exc:  # noqa: BLE001 - one missing copy never stops the export
                originals["unavailable"] += 1
                logger.warning(
                    "export: held original unavailable doc=%s (%s)", doc_id, type(exc).__name__
                )
                continue
            zf.writestr(f"originals/{doc_id}_{_safe_member_name(filename)}", data)
            del data
            originals["included"] += 1
            yield sink.take()
        manifest = {
            "format": EXPORT_FORMAT,
            "exported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "patient_id": patient_id,
            "row_counts": counts,
            "originals": originals,
            "not_exported": {r.table: r.reason for r in REGISTRY if not r.export},
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    yield sink.take()


# ── Drive disconnect ─────────────────────────────────────────────────────


async def revoke_google(db, patient_id: str) -> str:
    """Revoke the patient's Google grant(s) at Google; a code, never raises.

    ``no_token`` (nothing stored), ``revoked``, ``already_revoked``, ``failed``
    (network or Google error: the caller still deletes the stored token, and
    the person can remove the app in their Google Account), or
    ``skipped_shared_or_unknown`` (another patient uses the same grant or
    Google account, OR we cannot prove that none does: revoking would cut
    that patient's link too, so we only delete our own token). With several
    rows the least final code wins (failed > skipped > already > revoked).
    """
    return await revoke_grants(await db.oauth_grants_for_disconnect(patient_id=patient_id))


async def revoke_grants(grants: list[dict]) -> str:
    """Revoke grants read earlier by ``db.oauth_grants_for_disconnect`` (codes as
    :func:`revoke_google`). Callers read them first, remove the stored token,
    and revoke only then, so a failed local removal never leaves a stored
    token whose grant Google has already ended."""
    from oncofiles import oauth

    if not grants:
        return "no_token"
    order = ["revoked", "already_revoked", "skipped_shared_or_unknown", "failed"]
    worst = "revoked"
    done: set[str] = set()
    for grant in grants:
        token = grant["refresh_token"]
        if not grant.get("revocable"):  # fail safe: only a grant proven unshared
            code = "skipped_shared_or_unknown"
        elif token in done:
            continue
        else:
            done.add(token)
            code = await asyncio.to_thread(oauth.revoke_token, token)
        if order.index(code) > order.index(worst):
            worst = code
    return worst


def _caller_hash(email: str | None) -> str | None:
    try:
        from oncofiles.secrets_keys import caller_email_hash

        return caller_email_hash(email or "")
    except Exception:  # noqa: BLE001 - no key configured: no hash
        return None


async def audit(db, *, patient_id: str, tool: str, caller_email: str | None, data: dict) -> None:
    """One ``activity_log`` row: codes, counts and the caller's keyed hash only."""
    from oncofiles.models import ActivityLogEntry

    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=patient_id,
                session_id="data-rights",
                agent_id="dashboard" if tool != "erase_patient" else "mcp",
                tool_name=tool,
                input_summary=json.dumps({"caller_email_hash": _caller_hash(caller_email)}),
                output_summary=json.dumps(data, ensure_ascii=False, default=str),
                status="ok" if data.get("status") not in ("refused", "failed") else "error",
            )
        )
    except Exception:  # noqa: BLE001 - the audit never changes the answer
        logger.warning("data-rights: audit row for %s failed", tool, exc_info=True)


async def disconnect_drive(db, patient_id: str, *, caller_email: str | None) -> dict:
    """The caregiver's "Disconnect Google Drive" (see the module docstring).

    Returns ``{"status": "disconnected" | "not_connected" | "refused", ...}``
    with codes and counts only.
    """
    from oncofiles.tools import curated

    async def _refuse(code: str) -> dict:
        out = {"status": "refused", "code": code}
        await audit(
            db, patient_id=patient_id, tool="drive_disconnect", caller_email=caller_email, data=out
        )
        return out

    busy = curated.patient_work_reason(patient_id)
    if busy:
        return await _refuse(busy)
    async with curated.patient_lock(patient_id):
        # second check under the lock: a job may have started while we waited
        busy = curated.patient_work_reason(patient_id, lock_held_by_caller=True)
        if busy:
            return await _refuse(busy)
        grants = await db.oauth_grants_for_disconnect(patient_id=patient_id)
        removed = await db.delete_oauth_tokens(patient_id=patient_id)
        # The stored token is gone: the grant at Google must be revoked even when
        # switching curated processing off fails (the error still propagates).
        try:
            await db.set_curated_nightly(
                patient_id=patient_id, enabled=False, updated_by="drive disconnect (#856)"
            )
            curated.forget_patient(patient_id)
        finally:
            google = await revoke_grants(grants)
    out = {
        "status": "disconnected" if removed else "not_connected",
        "google_revoke": google,
        "tokens_removed": removed,
        "curated_processing": "off",
    }
    logger.info(
        "data-rights: drive disconnect patient %s: %s, google=%s",
        patient_id[:4],
        out["status"],
        google,
    )
    await audit(
        db, patient_id=patient_id, tool="drive_disconnect", caller_email=caller_email, data=out
    )
    return out
