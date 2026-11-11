"""Read-only HL7 FHIR R4 (4.0.1) projection of the hard-data model (#716, phase 3a).

Design: ``docs/architecture/hard_data_model.md`` §2-§4. Oncofiles is not a FHIR
server internally: the relational tables stay the store, and this package
projects them to FHIR JSON at the MCP boundary.

* ``ids``         — stable resource ids (``fhir_id`` column or UUIDv5)
* ``terminology`` — code systems, small mapping tables, UCUM, date helpers
* ``projection``  — one pure function per resource type (DB row → FHIR JSON)
* ``store``       — loads one patient's rows (every query filters on patient_id)
* ``search``      — search parameters, paging with a byte budget, bundles, IPS
* ``structure``   — a small hand-written structural check (required elements,
  cardinalities, choice types, id/date formats); the official HL7 validator
  (Java) is a follow-up for CI.

Read only: nothing in this package writes to the database.
"""

FHIR_VERSION = "4.0.1"
