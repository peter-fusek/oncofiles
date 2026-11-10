"""ACL gate on `_resolve_patient_id` (#497/#498).

Locks the canonical Option A pattern against the cross-patient slug-bypass
class. Before 2026-04-26, a caller bearer-bound to patient X could pass
`patient_slug=Y` and the resolver would happily return Y's patient_id; every
patient-scoped tool then operated under Y's scope. The gate added in
`_helpers.py:_resolve_patient_id` requires:

  * admin caller, OR
  * bearer-bound pid == resolved pid, OR
  * caller's OAuth-bound caregiver_email == patient.caregiver_email.

Each test below exercises one decision branch and asserts both the
allow-path and the deny-path. The deny-path's error message must NOT echo
the owning email or any other PHI — secondary-leak guard.
"""

from __future__ import annotations

import pytest

from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools._helpers import _resolve_patient_id
from tests.conftest import ERIKA_UUID

JANA_SLUG = "jana-test"
JANA_EMAIL = "jana.caregiver@example.com"
BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-test"
BOB_EMAIL = "bob.caregiver@example.com"


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


@pytest.fixture
async def two_patients(db: Database) -> Database:
    """Seed two patients with distinct caregiver_emails. Jana is also the
    bound pid via the conftest db fixture."""
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        (JANA_SLUG, JANA_EMAIL, ERIKA_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", BOB_EMAIL),
    )
    await db.db.commit()
    return db


@pytest.fixture
def reset_scope():
    """Reset all three verify_token ContextVars before and after each test
    so leakage between tests can't mask a denial bug."""
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    _verified_patient_id.set("")
    yield
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    _verified_patient_id.set("")


# ── Branch 1: admin bypass ────────────────────────────────────────────────


async def test_admin_caller_can_resolve_any_slug(two_patients: Database, reset_scope) -> None:
    """Static MCP_BEARER_TOKEN / DASHBOARD_ADMIN_EMAILS callers bypass ACL."""
    _verified_caller_is_admin.set(True)
    ctx = _StubCtx(two_patients)
    pid = await _resolve_patient_id(BOB_SLUG, ctx)
    assert pid == BOB_UUID


# ── Branch 2: bearer-bound match ──────────────────────────────────────────


async def test_bearer_bound_caller_can_resolve_own_slug(
    two_patients: Database, reset_scope
) -> None:
    """db fixture binds Jana's pid via _current_patient_id; passing
    Jana's slug is allowed (bearer match)."""
    ctx = _StubCtx(two_patients)
    pid = await _resolve_patient_id(JANA_SLUG, ctx)
    assert pid == ERIKA_UUID


async def test_bearer_bound_caller_blocked_from_other_slug(
    two_patients: Database, reset_scope
) -> None:
    """db fixture binds Jana's pid; passing Bob's slug must be denied —
    this is the canonical #497 cross-patient bypass."""
    ctx = _StubCtx(two_patients)
    with pytest.raises(ValueError, match="access denied"):
        await _resolve_patient_id(BOB_SLUG, ctx)


# ── Branch 3: caregiver_email match ───────────────────────────────────────


async def test_oauth_caregiver_can_resolve_own_patient(two_patients: Database, reset_scope) -> None:
    """OAuth caller whose caregiver_email matches the patient → allowed,
    even when bound_pid is a different patient (admin connector binding)."""
    _verified_caller_email.set(BOB_EMAIL)
    ctx = _StubCtx(two_patients)
    pid = await _resolve_patient_id(BOB_SLUG, ctx)
    assert pid == BOB_UUID


async def test_oauth_caregiver_blocked_from_non_caregiver_slug(
    two_patients: Database, reset_scope
) -> None:
    """OAuth caller bound to Bob's email cannot resolve Jana's slug —
    bound pid mismatches AND email doesn't match Jana's caregiver_email."""
    _verified_caller_email.set(BOB_EMAIL)
    # Reset _current_patient_id so the conftest's Jana binding doesn't
    # accidentally satisfy bearer_match.
    tok = _current_patient_id.set("")
    try:
        ctx = _StubCtx(two_patients)
        with pytest.raises(ValueError, match="access denied"):
            await _resolve_patient_id(JANA_SLUG, ctx)
    finally:
        _current_patient_id.reset(tok)


async def test_caregiver_email_match_is_case_insensitive(
    two_patients: Database, reset_scope
) -> None:
    """Real-world OAuth flows can return the email in mixed case — case
    must not be a footgun that converts an authorized caregiver into a
    sentinel-tier denial."""
    _verified_caller_email.set(BOB_EMAIL.upper())
    tok = _current_patient_id.set("")
    try:
        ctx = _StubCtx(two_patients)
        pid = await _resolve_patient_id(BOB_SLUG, ctx)
        assert pid == BOB_UUID
    finally:
        _current_patient_id.reset(tok)


async def test_second_caregiver_in_csv_list_can_resolve_slug(
    two_patients: Database, reset_scope
) -> None:
    """#651: caregiver_email may list several caregivers. The second one must
    pass the slug ACL just like login resolution already lets them in."""
    second = "second.caregiver@example.com"
    await two_patients.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?",
        (f"{BOB_EMAIL}, {second.upper()}", BOB_UUID),
    )
    await two_patients.db.commit()
    _verified_caller_email.set(f"  {second} ")
    tok = _current_patient_id.set("")
    try:
        pid = await _resolve_patient_id(BOB_SLUG, _StubCtx(two_patients))
        assert pid == BOB_UUID
    finally:
        _current_patient_id.reset(tok)


@pytest.mark.parametrize(
    "caller",
    ["second.caregiver@example", "caregiver@example.com", "example.com", ",", " "],
)
async def test_partial_email_never_matches_csv_entry(
    two_patients: Database, reset_scope, caller: str
) -> None:
    """A substring, suffix, separator or blank never matches a CSV entry."""
    await two_patients.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?",
        (f"{BOB_EMAIL},second.caregiver@example.com", BOB_UUID),
    )
    await two_patients.db.commit()
    _verified_caller_email.set(caller)
    tok = _current_patient_id.set("")
    try:
        with pytest.raises(ValueError, match="access denied"):
            await _resolve_patient_id(BOB_SLUG, _StubCtx(two_patients))
    finally:
        _current_patient_id.reset(tok)


def test_one_caregiver_matcher_everywhere() -> None:
    """server.py and the resolver use the persistent_oauth implementation."""
    from oncofiles import server
    from oncofiles.persistent_oauth import _email_matches_caregiver

    for email, csv, expected in [
        ("a@x.sk", "b@y.sk, A@X.SK", True),
        ("a@x.sk", "aa@x.sk", False),
        ("", "a@x.sk", False),
        ("a@x.sk", None, False),
    ]:
        assert _email_matches_caregiver(email, csv) is expected
        assert server._email_matches_caregiver(email, csv) is expected


# ── Branch 4: sentinel / no binding ───────────────────────────────────────


async def test_sentinel_bound_pid_blocked_from_real_slug(
    two_patients: Database, reset_scope
) -> None:
    """OAuth caller stamped with NO_PATIENT_ACCESS_SENTINEL (no caregiver
    match found) cannot smuggle access in by passing a real slug."""
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

    tok = _current_patient_id.set(NO_PATIENT_ACCESS_SENTINEL)
    try:
        ctx = _StubCtx(two_patients)
        with pytest.raises(ValueError, match="access denied"):
            await _resolve_patient_id(BOB_SLUG, ctx)
    finally:
        _current_patient_id.reset(tok)


async def test_no_binding_no_email_blocked(two_patients: Database, reset_scope) -> None:
    """Caller with no bearer binding and no OAuth email → cannot resolve
    any slug. Belt-and-braces against an unauth path that somehow reached
    the resolver."""
    tok = _current_patient_id.set("")
    try:
        ctx = _StubCtx(two_patients)
        with pytest.raises(ValueError, match="access denied"):
            await _resolve_patient_id(BOB_SLUG, ctx)
    finally:
        _current_patient_id.reset(tok)


# ── Secondary-leak guard ─────────────────────────────────────────────────


async def test_denial_message_does_not_echo_caregiver_email(
    two_patients: Database, reset_scope
) -> None:
    """The denial ValueError must not echo the target patient's
    caregiver_email — that would be a secondary leak (the attacker learns
    the owning email by probing slugs)."""
    ctx = _StubCtx(two_patients)
    with pytest.raises(ValueError) as exc_info:
        await _resolve_patient_id(BOB_SLUG, ctx)
    msg = str(exc_info.value)
    assert BOB_EMAIL not in msg
    assert BOB_UUID not in msg


# ── Slug-omitted path is unchanged ────────────────────────────────────────


async def test_slug_omitted_returns_bound_pid(two_patients: Database, reset_scope) -> None:
    """When patient_slug is None, the resolver must keep its pre-ACL
    behavior: return whatever the middleware bound for this request."""
    ctx = _StubCtx(two_patients)
    pid = await _resolve_patient_id(None, ctx)
    assert pid == ERIKA_UUID  # set by conftest db fixture


async def test_slug_omitted_no_binding_raises_when_required(
    two_patients: Database, reset_scope
) -> None:
    """Slug omitted + no bound pid + required=True → the existing
    ValueError from _get_patient_id (not the new ACL ValueError)."""
    tok = _current_patient_id.set("")
    try:
        ctx = _StubCtx(two_patients)
        with pytest.raises(ValueError, match="No patient selected|No patient access"):
            await _resolve_patient_id(None, ctx, required=True)
    finally:
        _current_patient_id.reset(tok)


# ── Unknown slug path is unchanged ────────────────────────────────────────


async def test_unknown_slug_raises_not_found_not_access_denied(
    two_patients: Database, reset_scope
) -> None:
    """Unknown slug must keep returning the friendly 'Patient not found'
    error rather than 'access denied' — clients depend on the distinction
    to give actionable error messages, and the existence check happens
    before the ACL check anyway."""
    ctx = _StubCtx(two_patients)
    with pytest.raises(ValueError, match="Patient not found"):
        await _resolve_patient_id("no-such-patient", ctx)
