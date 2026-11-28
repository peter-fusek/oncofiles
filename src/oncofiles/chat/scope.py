"""The chat scope: one fixed patient for the duration of a chat turn (#1020).

Inside ``chat_scope(pid)``:

* ``chat_patient_id()`` returns ``pid``; ``tools._helpers._resolve_patient_id``
  then returns ``pid`` for no slug or this patient's own slug and refuses any
  other slug with the ordinary "not found or access denied" text, whoever the
  caller is;
* ``tools._helpers._is_admin_caller()`` is False, so no admin bypass (slug
  routing, ownership checks) can widen the scope, even for an admin caller;
* the request patient ContextVar is ``pid`` and the caller's OAuth e-mail is
  cleared (no caregiver match can name another patient).

The core enters it inside its own task, so the values never leak into the
caller's context. Imports nothing from the rest of the package on purpose
(``tools._helpers`` imports this module).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_chat_patient: ContextVar[str | None] = ContextVar("oncofiles_chat_patient", default=None)


def chat_patient_id() -> str | None:
    """The patient a chat turn is fixed to, or None outside a chat turn."""
    return _chat_patient.get()


@contextmanager
def chat_scope(patient_id: str) -> Iterator[None]:
    if not patient_id or not isinstance(patient_id, str):
        raise ValueError("chat_scope needs a patient_id")
    from oncofiles.patient_middleware import _current_patient_id
    from oncofiles.persistent_oauth import (
        _verified_caller_email,
        _verified_caller_is_admin,
        _verified_patient_id,
    )

    tokens = [
        (_chat_patient, _chat_patient.set(patient_id)),
        (_current_patient_id, _current_patient_id.set(patient_id)),
        (_verified_patient_id, _verified_patient_id.set(patient_id)),
        (_verified_caller_is_admin, _verified_caller_is_admin.set(False)),
        (_verified_caller_email, _verified_caller_email.set("")),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
