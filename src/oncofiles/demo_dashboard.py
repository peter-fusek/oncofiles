"""Invented data for the public demo of the dashboard (``/demo``).

``/demo`` serves the SAME page and script as ``/dashboard`` (``<html
data-demo="1">``); instead of the authenticated API the script reads one JSON
document from ``/api/demo-data``. Its ``dashboard`` section, built here, holds
for every API path the dashboard calls an answer of the same shape:

    patients   ~ GET /api/patients
    status     ~ GET /status
    documents  ~ GET /api/documents
    details    ~ GET /api/doc-detail/{id}   (keyed by document id)
    activity   ~ GET /api/activity
    inbound_address ~ GET /api/inbound-address   (#950, invented address)
    trial_status ~ GET /api/trial-status   (#950 phase 2, not a trial)
    inbound_quarantine ~ GET /api/inbound-quarantine   (#950 quarantine, one
                 invented item from an invented sender)
    thumbnails ~ GET /api/doc-thumbnails  (keyed by document id; the script
                 answers each request with the ids it asked for, #948)

Everything is made up (names, institution, values, dates in 2027), per the
synthetic-fixtures rule. A few text fields carry an English twin (``*_en``)
so the demo reads naturally in both languages; real API rows never have them.
The previews are SVG drawings of the invented pages (``demo_thumbnails``).
No database, no Drive, no model: a pure function of constants.
"""

from __future__ import annotations

import re

from oncofiles import dashboard_inbox, demo_thumbnails
from oncofiles.document_stage import stage_of
from oncofiles.inbound_email import INBOUND_DOMAIN, TERMS_URL

PATIENT_ID = "demo-patient"
# Invented, never derived from any key: the demo address cannot reach a patient.
DEMO_INBOUND_TOKEN = "7mq3x9k2ta"
_HOSPITAL = ("Nemocnica Príkladov", "Example Hospital")
_FILE_TOKENS = ("JanaTestova_NemPrikladov", "JaneDoe_ExampleHospital")


def _line_text(line: dict, en: bool) -> str:
    cells = line.get("cells_en" if en else "cells") or line.get("cells")
    if cells:
        return "    ".join(str(c) for c in cells)
    return str((line.get("text_en") if en else None) or line.get("text") or "")


def _page_texts(doc: dict) -> list[tuple[str, str]]:
    """(Slovak text, English text) per page of a showcase document."""
    out = []
    for page in doc.get("pages") or []:
        lines = page.get("lines") or []
        out.append(
            (
                "\n".join(_line_text(ln, False) for ln in lines),
                "\n".join(_line_text(ln, True) for ln in lines),
            )
        )
    return out


def _name(date: str, slug: str, en: bool = False) -> str:
    return f"{date.replace('-', '')}_{_FILE_TOKENS[1 if en else 0]}_{slug}.pdf"


# #921: a curated folder names its files by the README standard with hyphens
# (``curated.naming``: date-Patient-Institution-Type-Doctor-Department[-Subject]),
# so the dashboard demo shows its "named" rows that way. The legacy underscore
# names stay the source (the landing and /demo/assistant still use them).
_LEGACY_NAME = re.compile(
    r"^(?P<date>\d{8})_(?P<patient>[A-Za-z]+)_(?P<inst>[A-Za-z]+)_(?P<cat>[A-Za-z]+)"
    r"_(?P<desc>[A-Za-z0-9]+)\.(?P<ext>[a-z0-9]+)$"
)
_README_TYPE = {"CT": "Imaging", "DischargeSummary": "Discharge"}
_DOCTOR = "Hruskova"  # invented surname, the same in both languages
_DEPARTMENT = {  # README DocumentType -> (sk, en) department token
    "Labs": ("Biochemia", "Biochemistry"),
    "Imaging": ("Radiologia", "Radiology"),
    "Pathology": ("Patologia", "Pathology"),
    "Discharge": ("Chirurgia", "Surgery"),
}


def _standard_name(name: str, en: bool) -> str:
    """The README-standard form of a demo name; any other name is returned as is."""
    from oncofiles.curated.naming import NameFields, build_filename

    m = _LEGACY_NAME.match(name or "")
    if not m:
        return name
    doc_type = _README_TYPE.get(m["cat"], m["cat"])
    dept = _DEPARTMENT.get(doc_type, ("Onkologia", "Oncology"))[1 if en else 0]
    fields = NameFields(
        date=m["date"],
        patient=m["patient"],
        institution=m["inst"],
        doc_type=doc_type,
        doctor=_DOCTOR,
        hospital=dept,
        subject=m["desc"],
    )
    return build_filename(fields, m["ext"])


# Documents that exist only in the dashboard demo, so that every state is on
# show: (id, document date, uploaded at, category, file name sk, file name en,
# summary sk, summary en, [(page sk, page en)], named, filed, attention).
_EXTRA = [
    (
        7,
        "2027-01-28",
        "2027-01-30T09:14:00",
        "discharge_summary",
        _name("2027-01-28", "DischargeSummary_Surgery"),
        _name("2027-01-28", "DischargeSummary_Surgery", en=True),
        "Prepúšťacia správa po plánovanom výkone.",
        "Discharge summary after a planned procedure.",
        [
            (
                "Nemocnica Príkladov – Chirurgická klinika\nPrepúšťacia správa\n"
                "Pacientka: Jana Testová (vymyslená)\n"
                "Prijatá: 24.01.2027   Prepustená: 28.01.2027\n\n"
                "Priebeh hospitalizácie bez komplikácií.\nKontrola v ambulancii o 14 dní.\n\n"
                "Vymyslená správa pre ukážku.",
                "Example Hospital – Department of Surgery\nDischarge summary\n"
                "Patient: Jane Doe (invented)\nAdmitted: 24 Jan 2027   Discharged: 28 Jan 2027\n\n"
                "Hospital stay without complications.\nFollow-up at the clinic in 14 days.\n\n"
                "Invented report for the demo.",
            )
        ],
        True,
        True,
        None,
    ),
    (
        8,
        "2027-01-15",
        "2027-01-19T15:02:00",
        "pathology",
        _name("2027-01-15", "Pathology_Biopsy"),
        _name("2027-01-15", "Pathology_Biopsy", en=True),
        "Výsledok histologického vyšetrenia.",
        "Result of the histology examination.",
        [
            (
                "Nemocnica Príkladov – Ústav patológie\nHistologické vyšetrenie\n"
                "Pacientka: Jana Testová (vymyslená)\nMateriál prijatý: 12.01.2027\n"
                "Záver vydaný: 15.01.2027\n\nVymyslený nález pre ukážku.",
                "Example Hospital – Institute of Pathology\nHistology report\n"
                "Patient: Jane Doe (invented)\nSpecimen received: 12 Jan 2027\n"
                "Conclusion issued: 15 Jan 2027\n\nInvented finding for the demo.",
            )
        ],
        True,
        True,
        None,
    ),
    (
        9,
        "2027-04-06",
        "2027-04-07T08:31:00",
        "prescription",
        _name("2027-04-06", "Prescription_Antiemetic"),
        _name("2027-04-06", "Prescription_Antiemetic", en=True),
        "Recept na podpornú liečbu.",
        "Prescription for supportive medication.",
        [
            (
                "Nemocnica Príkladov – Onkologická ambulancia\nLekársky predpis\n"
                "Pacientka: Jana Testová (vymyslená)\nDátum: 06.04.2027\n\n"
                "Podporná liečba podľa odporúčania lekára.\n\nVymyslený predpis pre ukážku.",
                "Example Hospital – Oncology Clinic\nPrescription\n"
                "Patient: Jane Doe (invented)\nDate: 6 Apr 2027\n\n"
                "Supportive medication as advised by the physician.\n\n"
                "Invented prescription for the demo.",
            )
        ],
        True,
        True,
        None,
    ),
    (
        10,
        "2027-04-12",
        "2027-04-13T18:47:00",
        "labs",
        "sken_laboratorium_april.pdf",
        "scan_lab_april.pdf",
        "",
        "",
        [
            (
                "Nemocnica Príkladov – Oddelenie klinickej biochémie\n"
                "Pacientka: Jana Testová (vymyslená)\nOdber: 12.04.2027 06:35\n\n"
                "Krvný obraz\nWBC    5,8    ×10⁹/L    4,0–10,0\nHGB    129    g/L    120–160\n"
                "PLT    236    ×10⁹/L    150–400\n\nVymyslený výsledok pre ukážku.",
                "Example Hospital – Clinical Biochemistry\n"
                "Patient: Jane Doe (invented)\nSampled: 12 Apr 2027 06:35\n\n"
                "Blood count\nWBC    5.8    ×10⁹/L    4.0–10.0\nHGB    129    g/L    120–160\n"
                "PLT    236    ×10⁹/L    150–400\n\nInvented result for the demo.",
            )
        ],
        False,
        False,
        None,
    ),
    (
        11,
        "2027-04-13",
        "2027-04-13T19:05:00",
        "labs",
        "20270413_JanaTestova_NemPrikladov_Labs_BloodCountPhoto.jpg",
        "20270413_JaneDoe_ExampleHospital_Labs_BloodCountPhoto.jpg",
        "Fotografia výsledkov krvného obrazu (mobil).",
        "Phone photo of blood count results.",
        [
            (
                "Nemocnica Príkladov – Oddelenie klinickej biochémie\nVýsledky vyšetrenia\n"
                "Pacientka: Jana Testová (vymyslená)\nOdber: 13.04.2027 07:20\n\n"
                "WBC    6,4    ×10⁹/L    4,0–10,0\nHGB    127    g/L    120–160\n"
                "PLT    241    ×10⁹/L    150–400\nCRP    3,8    mg/L    < 5,0\n\n"
                "Vymyslený výsledok pre ukážku.",
                "Example Hospital – Clinical Biochemistry\nTest results\n"
                "Patient: Jane Doe (invented)\nSampled: 13 Apr 2027 07:20\n\n"
                "WBC    6.4    ×10⁹/L    4.0–10.0\nHGB    127    g/L    120–160\n"
                "PLT    241    ×10⁹/L    150–400\nCRP    3.8    mg/L    < 5.0\n\n"
                "Invented result for the demo.",
            )
        ],
        True,
        True,
        None,
    ),
    (
        12,
        "2027-04-09",
        "2027-04-10T12:20:00",
        "report",
        "poznamka_z_kontroly.jpg",
        "note_from_visit.jpg",
        "",
        "",
        [
            (
                "Nemocnica Príkladov – Onkologická ambulancia\nKontrola 09.04.2027\n"
                "Rukou písaná poznámka: [?] ďalšia kontrola o 3 týždne\n\n"
                "Vymyslená poznámka pre ukážku.",
                "Example Hospital – Oncology Clinic\nVisit 9 Apr 2027\n"
                "Handwritten note: [?] next visit in 3 weeks\n\nInvented note for the demo.",
            )
        ],
        False,
        False,
        "review_needed",
    ),
]

# How the preview of a demo-only document is drawn, when not as a printed page:
# (style, title sk, title en). The sk/en titles name a page whose text has none.
_PICTURE = {
    10: ("scan", "Laboratórny výsledok", "Lab report"),
    11: ("printout_photo", "", ""),
    12: ("note_photo", "", ""),
}

# A photo the caregiver just added, still waiting in the inbox (the shape of
# one entry of ``tools.curated.inbox_snapshot``; invented id and time).
_INBOX = [
    {
        "id": "demo-inbox-1",
        "name": "IMG_2291.jpg",
        "size": 2_310_000,
        "mime": "image/jpeg",
        "modified": "2027-04-14T07:58:00",
        "review": False,
    }
]

# Upload times of the six showcase documents (ids 1–6), a day or two after the
# document date, as if the caregiver scanned them once back home.
_SHOWCASE_UPLOADED = {
    1: "T08:05:00",
    2: "T08:12:00",
    3: "T17:26:00",
    4: "T09:40:00",
    5: "T10:15:00",
    6: "T16:03:00",
}


def _plus_days(iso_date: str, days: int) -> str:
    from datetime import date, timedelta

    y, m, d = (int(x) for x in iso_date.split("-"))
    return (date(y, m, d) + timedelta(days=days)).isoformat()


def _row(
    doc_id, date, uploaded_at, category, name, name_en, *, has_text, named, filed, attention
) -> dict:
    stage = stage_of(extracted=has_text, named=named, filed=filed, attention=attention)
    return {
        "id": doc_id,
        "filename": _standard_name(name, en=False),
        "filename_en": _standard_name(name_en, en=True),
        "category": category,
        "date": date,
        "institution": _HOSPITAL[0] if named else None,
        "institution_en": _HOSPITAL[1] if named else None,
        "gdrive_id": None,
        "uploaded_at": uploaded_at,
        "has_ocr": has_text,
        "has_ai": False,
        "has_metadata": has_text,
        "has_date": date is not None,
        "has_institution": named,
        "is_synced": filed,
        "is_standard_name": named,
        "fully_complete": False,
        "curated": True,
        "attention_reason": attention,
        "review_reasons": [],  # #919: codes; the demo shows the generic help text
        **stage,
    }


def _detail(row: dict, summary: tuple[str, str], pages: list[tuple[str, str]]) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "filename_en": row["filename_en"],
        "gdrive_id": "",
        "gdrive_url": "",
        "preview_url": "",
        "category": row["category"],
        "document_date": row["date"] or "",
        "institution": row["institution"] or "",
        "institution_en": row["institution_en"] or "",
        "ai_summary": summary[0],
        "ai_summary_en": summary[1],
        "ai_tags": [],
        "structured_metadata": {},
        "pages": [
            {"page_number": n, "text": sk, "text_en": en, "char_count": len(sk)}
            for n, (sk, en) in enumerate(pages, 1)
        ],
    }


def build(showcase_documents: list[dict], version: str) -> dict:
    """The ``dashboard`` section of ``/api/demo-data``."""
    rows: list[dict] = []
    details: dict[str, dict] = {}
    pictures: list[dict] = []

    for doc in showcase_documents:
        pictures.append(
            {
                "id": doc["id"],
                "category": doc["category"],
                "title": doc.get("title_sk", ""),
                "title_en": doc.get("title_en", ""),
                "lines": (doc.get("pages") or [{}])[0].get("lines") or [],
            }
        )
        uploaded = _plus_days(doc["date"], 1) + _SHOWCASE_UPLOADED.get(doc["id"], "T09:00:00")
        row = _row(
            doc["id"],
            doc["date"],
            uploaded,
            doc["category"],
            doc["filename"],
            doc["filename_en"],
            has_text=True,
            named=True,
            filed=True,
            attention=None,
        )
        rows.append(row)
        details[str(doc["id"])] = _detail(
            row, (doc.get("title_sk", ""), doc.get("title_en", "")), _page_texts(doc)
        )

    for doc_id, date, up, cat, name, name_en, s_sk, s_en, pages, named, filed, attention in _EXTRA:
        row = _row(
            doc_id,
            date,
            up,
            cat,
            name,
            name_en,
            has_text=bool(pages),
            named=named,
            filed=filed,
            attention=attention,
        )
        rows.append(row)
        details[str(doc_id)] = _detail(row, (s_sk, s_en), pages)
        style, title_sk, title_en = _PICTURE.get(doc_id, ("printed", "", ""))
        pictures.append(
            {
                "id": doc_id,
                "category": cat,
                "style": style,
                "title": title_sk,
                "title_en": title_en,
                "text": pages[0][0] if pages else "",
                "text_en": pages[0][1] if pages else "",
            }
        )

    # Files still in the inbox: the same rows GET /api/documents builds from the
    # probe's snapshot (dashboard_inbox), so they have no detail entry.
    inbox_rows = dashboard_inbox.rows(_INBOX, set())
    rows.extend(inbox_rows)

    rows.sort(key=lambda r: r["uploaded_at"], reverse=True)
    ready = sum(1 for r in rows if r["stage"] == "ready")
    attention_n = sum(1 for r in rows if r["stage"] == "attention")
    summary = {
        "total": len(rows),
        "ready": ready,
        "attention": attention_n,
        "in_progress": len(rows) - ready - attention_n,
        "inbox": len(inbox_rows),
        "extracted": sum(1 for r in rows if r["steps"]["extracted"]),
        "named": sum(1 for r in rows if r["steps"]["named"]),
        "filed": sum(1 for r in rows if r["steps"]["filed"]),
        "with_ocr": sum(1 for r in rows if r["has_ocr"]),
        "with_ai": 0,
        "with_metadata": sum(1 for r in rows if r["has_metadata"]),
        "synced": sum(1 for r in rows if r["is_synced"]),
        "standard_named": sum(1 for r in rows if r["is_standard_name"]),
        "with_date": sum(1 for r in rows if r["has_date"]),
        "with_institution": sum(1 for r in rows if r["has_institution"]),
        "fully_complete": 0,
    }

    def run(
        at: str, trigger: str, documents: int, filed: int, review: int = 0, waiting: int = 0
    ) -> dict:
        return {
            "at": at,
            "kind": "curated_run",
            "status": "ok",
            "trigger": trigger,
            "duration_s": 60 * max(1, documents) * 4,
            "counts": {
                "documents": documents,
                "filed": filed,
                "review": review,
                "not_processed": waiting,
            },
        }

    return {
        "patients": {
            "own": [
                {
                    "patient_id": PATIENT_ID,
                    "slug": "jana-testova",
                    "display_name": "Jana Testová",
                    "display_name_en": "Jane Doe",
                    "diagnosis_summary": "vymyslená pacientka (ukážka)",
                    "diagnosis_summary_en": "invented patient (demo)",
                    "is_active": True,
                }
            ],
            "admin": [],
            "is_admin": False,
        },
        "status": {
            "status": "ok",
            "version": version,
            "documents": len(rows),
            "google_services": {
                "drive": True,
                "gmail": False,
                "calendar": False,
                "folder_id": None,
                "folder_name": "Zdravie – Jana (ukážka)",
                "folder_name_en": "Health – Jane (demo)",
                "owner_email": "jana.testova@example.com",
            },
            "service_sync_times": {},
        },
        "documents": {
            "filter": "all",
            "matched": len(rows),
            "returned": len(rows),
            "offset": 0,
            "limit": 2000,
            "summary": summary,
            "documents": rows,
        },
        "details": details,
        # #950: the demo twin of GET /api/inbound-address (an invented address
        # with an invented Gmail-style prefix; rotating it is blocked in the demo).
        "inbound_address": {
            "enabled": True,
            "address": f"jana.example-{DEMO_INBOUND_TOKEN}@{INBOUND_DOMAIN}",
            "domain": INBOUND_DOMAIN,
            "version": 1,
            "created_at": "2027-01-19T15:20:00Z",
            "terms_url": TERMS_URL,
        },
        # #950 phase 2: the demo twin of GET /api/trial-status (the demo
        # patient did not start through welcome@, so no first-steps card).
        "trial_status": {"trial": False},
        # #950 quarantine: the demo twin of GET /api/inbound-quarantine (one
        # invented item; approving or rejecting only explains itself).
        "inbound_quarantine": {
            "enabled": True,
            "read_only": False,
            "pending": 1,
            "items": [
                {
                    "id": 1,
                    "sender": "ambulancia@example.test",
                    "received_at": "2027-04-14T08:12:00Z",
                    "reason": "unknown_sender",
                    "files": [
                        {"name": "vysledky_2027-04-13.pdf", "type": "PDF", "size": 184320},
                    ],
                    "total_bytes": 184320,
                    "filed_count": 0,
                }
            ],
            # the trash ("Kôš"): a rejected item still restorable (invented)
            "trash": [
                {
                    "id": 2,
                    "sender": "neznamy@example.test",
                    "received_at": "2027-04-09T17:40:00Z",
                    "reason": "unknown_sender",
                    "files": [{"name": "faktura_2027.pdf", "type": "PDF", "size": 51200}],
                    "total_bytes": 51200,
                    "filed_count": 0,
                    "state": "rejected",
                    "days_left": 25,
                }
            ],
        },
        "thumbnails": demo_thumbnails.build(pictures),
        "activity": {
            "events": [
                run("2027-04-13T19:05:00", "auto", 1, 0, waiting=1),
                run("2027-04-10T12:44:00", "auto", 1, 0, review=1),
                run("2027-04-07T08:52:00", "auto", 2, 2),
                {
                    "at": "2027-04-05T10:15:00",
                    "kind": "text_reread",
                    "status": "ok",
                    "trigger": "reocr",
                    "duration_s": 2400,
                    "counts": {"rewritten": 3, "kept": 1},
                },
                run("2027-04-03T01:00:00", "nightly", 0, 0),
                run("2027-03-18T08:31:00", "auto", 1, 1),
                run("2027-03-02T17:50:00", "auto", 1, 1),
                run("2027-02-21T10:36:00", "auto", 2, 2),
                run("2027-01-30T09:41:00", "auto", 1, 1),
                run("2027-01-19T15:20:00", "auto", 1, 1),
            ],
            "pending": {"inbox": 1},
        },
    }
