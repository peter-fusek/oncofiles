"""``GET /api/public-config``: the landing's trial flags (live operation, 2026-11-21).

Public (no auth), no DB call, runtime env only — the same rules as /health.
``inbound_welcome_enabled`` is true only when every inbound variable is set,
the sender verification is ready and infra has set ``INBOUND_GO_LIVE=true``
(#950: DKIM mode needs no authserv-id, so the CTA would otherwise turn on as
soon as this is deployed).
"""

from __future__ import annotations

import inspect
import itertools
import json

import pytest
from starlette.requests import Request

from oncofiles import config, public_config, ratelimit
from oncofiles.server import api_public_config

ENVS = public_config.INBOUND_WELCOME_ENVS
COMBINATIONS = [c for n in range(len(ENVS) + 1) for c in itertools.combinations(ENVS, n)]


def _req() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/public-config", "headers": []})


async def _body() -> dict:
    return json.loads((await api_public_config(_req())).body)


@pytest.fixture
def no_inbound_env(monkeypatch):
    for name in ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("INBOUND_BREVO_AUTHSERV_ID", raising=False)
    monkeypatch.setenv(public_config.GO_LIVE_ENV, "true")
    return monkeypatch


def test_the_three_inbound_variables():
    # #950: the authserv-id is no longer required (DKIM mode is the default)
    assert set(ENVS) == {
        "INBOUND_BREVO_TOKEN",
        "INBOUND_JOBS_SECRET",
        "HOLDING_STORE_SECRET",
    }
    assert public_config.GO_LIVE_ENV == "INBOUND_GO_LIVE"


@pytest.mark.parametrize("value", [None, "", "false", "1", "yes", "TRUE ", "True"])
async def test_go_live_gates_the_flag(no_inbound_env, value):
    """Every variable set: the CTA is on only for INBOUND_GO_LIVE=true."""
    for name in ENVS:
        no_inbound_env.setenv(name, "x")
    if value is None:
        no_inbound_env.delenv(public_config.GO_LIVE_ENV, raising=False)
    else:
        no_inbound_env.setenv(public_config.GO_LIVE_ENV, value)
    expected = value is not None and value.strip().lower() == "true"
    assert (await _body())["inbound_welcome_enabled"] is expected


async def test_go_live_alone_is_not_enough(no_inbound_env):
    assert (await _body())["inbound_welcome_enabled"] is False


async def test_dkim_mode_needs_no_authserv_id(no_inbound_env):
    from oncofiles import inbound_email

    for name in ENVS:
        no_inbound_env.setenv(name, "x")
    assert inbound_email.verification_mode() == "dkim"
    assert (await _body())["inbound_welcome_enabled"] is True
    # #966: the old authserv-id is ignored (DKIM only); it changes nothing
    no_inbound_env.setenv("INBOUND_BREVO_AUTHSERV_ID", "mx.synthetic.test")
    assert inbound_email.verification_mode() == "dkim"
    assert (await _body())["inbound_welcome_enabled"] is True


@pytest.mark.parametrize(
    "present", COMBINATIONS, ids=lambda c: "+".join(v.split("_", 1)[1] for v in c) or "none"
)
async def test_inbound_flag_needs_every_variable(no_inbound_env, present):
    for name in present:
        no_inbound_env.setenv(name, "x")
    assert (await _body())["inbound_welcome_enabled"] is (len(present) == len(ENVS))


async def test_blank_values_do_not_count(no_inbound_env):
    for name in ENVS:
        no_inbound_env.setenv(name, "  ")
    assert (await _body())["inbound_welcome_enabled"] is False


@pytest.mark.parametrize(
    "raw,mode",
    [
        ("open", "open"),
        ("invite", "invite"),
        ("closed", "closed"),
        ("OPEN", "open"),
        ("nope", "closed"),
        ("", "closed"),
    ],
)
async def test_onboarding_mode_per_env(monkeypatch, raw, mode):
    monkeypatch.setattr(config, "ONBOARDING_MODE", raw)
    assert (await _body())["onboarding_mode"] == mode


async def test_shape_address_and_cache_header(no_inbound_env):
    resp = await api_public_config(_req())
    body = json.loads(resp.body)
    assert set(body) == {"inbound_welcome_enabled", "onboarding_mode", "welcome_address"}
    assert body["welcome_address"] == "welcome@docs.oncofiles.com"
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=60"


async def test_never_returns_a_secret_or_a_variable_name(no_inbound_env):
    for name in ENVS:
        no_inbound_env.setenv(name, f"secret-value-{name.lower()}")
    raw = (await api_public_config(_req())).body.decode()
    assert "secret-value" not in raw
    for name in ENVS:
        assert name not in raw


def test_no_db_and_no_auth_gate():
    source = inspect.getsource(api_public_config) + inspect.getsource(public_config)
    for forbidden in ("await db", "db.", "_get_db", "lifespan", "aiosqlite", "sqlite"):
        assert forbidden not in source, forbidden
    for gate in ("_check_dashboard_auth", "_require_admin", "_check_bearer"):
        assert gate not in source, gate


async def test_answers_without_any_credential_or_db(no_inbound_env):
    """A bare request (no headers, no app state, so no DB) gets a 200."""
    resp = await api_public_config(_req())
    assert resp.status_code == 200


def test_is_rate_limited_as_public():
    assert ratelimit.classify("/api/public-config") == ratelimit.PUBLIC
    # the rest of /api/ keeps its class
    assert ratelimit.classify("/api/onboarding-status") == ratelimit.API
