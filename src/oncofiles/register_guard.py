"""Reject malformed DCR bodies on ``POST /register`` with 400, not 500 (#584).

The MCP SDK's ``RegistrationHandler`` calls ``await request.json()`` and only
catches pydantic ``ValidationError``. A body that is not valid JSON therefore
raises ``json.JSONDecodeError`` and surfaces as ``500 Internal Server Error``
with a full traceback in the logs — every internet probe of ``/register``
produced one (checked up to ``mcp`` 1.30.0).

RFC 7591 §3.2.2 says an invalid registration request gets ``400`` with
``error = invalid_client_metadata``. This raw-ASGI middleware buffers the
(small) request body for ``POST /register`` only, answers ``400`` when it is
not a JSON document, and otherwise replays the exact bytes to the SDK handler
unchanged. Every other path passes straight through.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

REGISTER_PATH = "/register"
# DCR metadata is a few hundred bytes; anything far larger is not a client.
MAX_REGISTER_BODY_BYTES = 64 * 1024


def _error_body(description: str) -> bytes:
    return json.dumps(
        {"error": "invalid_client_metadata", "error_description": description}
    ).encode("utf-8")


class RegisterJSONGuardMiddleware:
    """Map an invalid JSON body on ``POST /register`` to ``400``."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or scope.get("path") != REGISTER_PATH
        ):
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"") or b""
            size += len(chunk)
            if size > MAX_REGISTER_BODY_BYTES:
                await self._reject(send, "request body too large", status=413)
                return
            chunks.append(chunk)
            more_body = message.get("more_body", False)
        body = b"".join(chunks)

        try:
            json.loads(body)
        except (ValueError, UnicodeDecodeError):
            # Single-line log, no traceback: this is client noise (#415 lesson).
            logger.info("DCR /register: rejected non-JSON body (%d bytes)", len(body))
            await self._reject(send, "request body is not valid JSON")
            return

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send, description: str, status: int = 400) -> None:
        payload = _error_body(description)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
