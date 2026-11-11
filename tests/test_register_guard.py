"""#584: POST /register with a non-JSON body must answer 400, not 500."""

from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock

from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.settings import ClientRegistrationOptions
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from oncofiles.register_guard import MAX_REGISTER_BODY_BYTES, RegisterJSONGuardMiddleware

VALID_METADATA = {
    "redirect_uris": ["https://client.example.test/callback"],
    "client_name": "synthetic-test-client",
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def _app(*, guarded: bool) -> tuple[Starlette, MagicMock]:
    provider = MagicMock()
    provider.register_client = AsyncMock(return_value=None)
    handler = RegistrationHandler(
        provider=provider, options=ClientRegistrationOptions(enabled=True)
    )
    middleware = [Middleware(RegisterJSONGuardMiddleware)] if guarded else []
    app = Starlette(
        routes=[
            Route("/register", handler.handle, methods=["POST"]),
            Route("/other", handler.handle, methods=["POST"]),
        ],
        middleware=middleware,
    )
    return app, provider


def test_unguarded_sdk_handler_still_500s_on_invalid_json():
    """Documents the upstream bug. If this starts failing, the `mcp` SDK
    handles it itself and the guard can be reconsidered."""
    app, _ = _app(guarded=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/register", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 500


def test_invalid_json_returns_400_invalid_client_metadata():
    app, provider = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post(
        "/register", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "invalid_client_metadata"
    assert "JSON" in body["error_description"]
    provider.register_client.assert_not_awaited()


def test_empty_and_binary_bodies_return_400():
    app, _ = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    for payload in (b"", b"\xff\xfe\x00garbage", b"<xml/>"):
        resp = client.post("/register", content=payload)
        assert resp.status_code == 400, payload


def test_oversized_body_rejected():
    app, _ = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/register", content=b" " * (MAX_REGISTER_BODY_BYTES + 1))
    assert resp.status_code == 413
    assert resp.json()["error"] == "invalid_client_metadata"


def test_valid_json_is_replayed_to_sdk_handler():
    app, provider = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/register", content=json.dumps(VALID_METADATA).encode())
    assert resp.status_code == 201, resp.text
    assert resp.json()["client_name"] == "synthetic-test-client"
    provider.register_client.assert_awaited_once()


def test_valid_json_with_bad_metadata_keeps_sdk_400():
    app, _ = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/register", content=b'{"redirect_uris": "not-a-list"}')
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_other_paths_untouched():
    app, _ = _app(guarded=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/other", content=b"{not json")
    # Not guarded: the SDK's own behaviour (500) is visible on other paths.
    assert resp.status_code == 500


def test_main_wires_register_guard():
    from oncofiles import server

    src = inspect.getsource(server.main)
    assert "Middleware(RegisterJSONGuardMiddleware)" in src
