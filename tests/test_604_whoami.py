"""#604 — `whoami`: caller identity, auth method, admin flag, accessible patients.

One test per auth path. The HTTP paths go through the real
``PersistentOAuthProvider.verify_token`` (the code that sets the identity
ContextVars in production), then copy the verified pid into the middleware
ContextVar exactly as ``PatientResolutionMiddleware`` does. All identities
are synthetic.
"""

from __future__ import annotations

import json

import pytest

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.persistent_oauth import (
    PersistentOAuthProvider,
    _verified_auth_method,
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools.patient import whoami
from tests.conftest import ERIKA_UUID
from tests.test_persistent_oauth import _mint_oauth_token_with_email

OWN_UUID = ERIKA_UUID
OWN_SLUG = "alpha-test"
OWN_NAME = "Alpha Example"
OWN_EMAIL = "alpha.caregiver@example.com"
FOREIGN_UUID = "00000000-0000-4000-8000-0000000000f2"
FOREIGN_SLUG = "bravo-test"
FOREIGN_NAME = "Bravo Example"
FOREIGN_EMAIL = "bravo.caregiver@example.com"
ADMIN_EMAIL = "operator@example.com"
STATIC_TOKEN = "static-operator-token-for-tests"


class _Ctx:
    class _Req:
        def __init__(self, db, transport):
            self.lifespan_context = {"db": db, "transport": transport}

    def __init__(self, db, transport="streamable-http"):
        self.request_context = self._Req(db, transport)


@pytest.fixture
async def pdb(db):
    await db.db.execute(
        "UPDATE patients SET slug = ?, display_name = ?, caregiver_email = ? WHERE patient_id = ?",
        (OWN_SLUG, OWN_NAME, OWN_EMAIL, OWN_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (FOREIGN_UUID, FOREIGN_SLUG, FOREIGN_NAME, FOREIGN_EMAIL),
    )
    await db.db.commit()
    return db


@pytest.fixture(autouse=True)
def _scope():
    vars_ = (
        _verified_caller_is_admin,
        _verified_caller_email,
        _verified_patient_id,
        _verified_auth_method,
    )
    toks = [v.set(d) for v, d in zip(vars_, (False, "", "", ""), strict=True)]
    yield
    for v, t in zip(vars_, toks, strict=True):
        v.reset(t)


async def _verify(provider: PersistentOAuthProvider, token: str) -> None:
    """verify_token, then the middleware's copy of the verified pid."""
    assert await provider.verify_token(token) is not None
    _current_patient_id.set(_verified_patient_id.get())


async def _whoami(db, transport="streamable-http") -> tuple[dict, str]:
    raw = await whoami(_Ctx(db, transport))
    return json.loads(raw), raw


def _no_phi(raw: str) -> None:
    # Only slug + display name per patient; never ids, emails of others, or context.
    for forbidden in ("patient_id", "caregiver_email", "date_of_birth", "diagnosis"):
        assert forbidden not in raw


async def test_whoami_oauth_caregiver(pdb):
    provider = PersistentOAuthProvider(db=pdb)
    tok = await _mint_oauth_token_with_email(provider, email=OWN_EMAIL, challenge="w1")
    await _verify(provider, tok.access_token)

    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "oauth"
    assert data["email"] == OWN_EMAIL
    assert data["is_admin"] is False
    assert data["bound_patient"] == {"slug": OWN_SLUG, "name": OWN_NAME}
    assert data["patients"] == [{"slug": OWN_SLUG, "name": OWN_NAME}]
    assert FOREIGN_SLUG not in raw and FOREIGN_NAME not in raw
    _no_phi(raw)


async def test_whoami_oauth_admin_lists_only_own_caregiver_patients(pdb, monkeypatch):
    monkeypatch.setattr("oncofiles.config.DASHBOARD_ADMIN_EMAILS", [ADMIN_EMAIL])
    provider = PersistentOAuthProvider(db=pdb)
    tok = await _mint_oauth_token_with_email(provider, email=ADMIN_EMAIL, challenge="w2")
    await _verify(provider, tok.access_token)

    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "oauth"
    assert data["email"] == ADMIN_EMAIL
    assert data["is_admin"] is True
    # #590: admin with no caregiver binding enumerates nobody.
    assert data["patients"] == []
    assert data["bound_patient"] is None
    assert "guidance" in data
    for needle in (OWN_SLUG, FOREIGN_SLUG, OWN_NAME, FOREIGN_NAME):
        assert needle not in raw


async def test_whoami_oauth_unknown_email_sees_nothing(pdb):
    provider = PersistentOAuthProvider(db=pdb)
    tok = await _mint_oauth_token_with_email(provider, email="stranger@example.com", challenge="w3")
    await _verify(provider, tok.access_token)
    assert _current_patient_id.get() == NO_PATIENT_ACCESS_SENTINEL

    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "oauth"
    assert data["is_admin"] is False
    assert data["patients"] == []
    assert data["bound_patient"] is None
    assert NO_PATIENT_ACCESS_SENTINEL not in raw


async def test_whoami_patient_bearer(pdb):
    token = await pdb.create_patient_token(OWN_UUID, label="test")
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    await _verify(provider, token)

    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "patient_bearer"
    assert data["email"] is None
    assert data["is_admin"] is False
    assert data["bound_patient"] == {"slug": OWN_SLUG, "name": OWN_NAME}
    assert data["patients"] == [{"slug": OWN_SLUG, "name": OWN_NAME}]
    assert FOREIGN_SLUG not in raw
    _no_phi(raw)


async def test_whoami_static_bearer(pdb):
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    await _verify(provider, STATIC_TOKEN)

    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "static_bearer"
    assert data["email"] is None
    assert data["is_admin"] is True
    # Multi-patient deployment: the operator token is not bound to anyone.
    assert data["bound_patient"] is None
    assert {p["slug"] for p in data["patients"]} == {OWN_SLUG, FOREIGN_SLUG}
    _no_phi(raw)


async def test_whoami_stdio(pdb):
    # stdio: no token, middleware binds the default patient.
    _current_patient_id.set(OWN_UUID)
    data, raw = await _whoami(pdb, transport="stdio")
    assert data["auth_method"] == "stdio"
    assert data["email"] is None
    assert data["is_admin"] is False
    assert data["bound_patient"] == {"slug": OWN_SLUG, "name": OWN_NAME}
    assert data["patients"] == [{"slug": OWN_SLUG, "name": OWN_NAME}]
    assert FOREIGN_SLUG not in raw


async def test_whoami_http_without_identity(pdb):
    _current_patient_id.set("")
    data, raw = await _whoami(pdb)
    assert data["auth_method"] == "none"
    assert data["is_admin"] is False
    assert data["patients"] == []
    for needle in (OWN_SLUG, FOREIGN_SLUG):
        assert needle not in raw


async def test_verify_token_resets_auth_method_between_calls(pdb):
    """A failed verification must not inherit the previous caller's method."""
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    await _verify(provider, STATIC_TOKEN)
    assert _verified_auth_method.get() == "static_bearer"
    assert await provider.verify_token("not-a-valid-token") is None
    assert _verified_auth_method.get() == ""
    assert _verified_caller_is_admin.get() is False


async def test_whoami_registered_read_only():
    from fastmcp import FastMCP

    from oncofiles.tools import patient

    mcp = FastMCP("t")
    patient.register(mcp)
    tool = await mcp.get_tool("whoami")
    assert tool.annotations is not None
    assert tool.annotations.readOnlyHint is True
