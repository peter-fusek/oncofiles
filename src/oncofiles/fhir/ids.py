"""Stable FHIR resource ids.

Rule: a row's ``fhir_id`` column wins when it is set (every #725 fact table, and
``clinical_records.fhir_id`` once a writer fills it). Otherwise the id is a
deterministic UUIDv5 over ``"<table>:<row id>"`` in ``ONCOFILES_FHIR_NAMESPACE``,
so the same row always projects to the same id, across calls and restarts.

``ONCOFILES_FHIR_NAMESPACE`` is fixed forever: changing it would change every
derived id an agent may have stored. It is ``uuid5(NAMESPACE_URL,
"https://oncofiles.com/fhir")``, written out so a refactor cannot change it.

Ids are not secrets and not access tokens: every read resolves the patient first
and only looks the id up among that patient's rows.

TODO(#728): when PR #728 lands its ``fact_identity`` module, ``fhir_read`` must
also accept the fact ids minted there (look them up before the UUIDv5 fallback
in ``search.PatientIndex.get``).
"""

from __future__ import annotations

import uuid

ONCOFILES_FHIR_NAMESPACE = uuid.UUID("3e330635-2970-521e-97fe-9146960ef2f7")


def derived_id(table: str, row_id: int | str) -> str:
    """Deterministic id for a row that has no ``fhir_id`` of its own."""
    return str(uuid.uuid5(ONCOFILES_FHIR_NAMESPACE, f"{table}:{row_id}"))


def row_id(table: str, row: dict) -> str:
    """The FHIR id of a DB row: its ``fhir_id`` when set, else the UUIDv5."""
    fid = row.get("fhir_id")
    return str(fid) if fid else derived_id(table, row["id"])
