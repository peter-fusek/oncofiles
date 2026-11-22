"""#948: ``GET /api/doc-thumbnails`` — document previews for the dashboard.

Drive is a stub (``FakeDrive``): no test reaches the network (the network
guard would fail it). Patients, e-mails (example.test), filenames, Drive ids
and the PDF content are invented; dates are 2027 or later.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import doc_thumbnails, server
from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient, _sized_thumbnail_url
from oncofiles.models import DocumentCategory
from tests.helpers import PRIMARY_TEST_UUID, make_doc

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"

A_UUID = PRIMARY_TEST_UUID
B_UUID = "00000000-0000-4000-8000-000000000948"
A_EMAIL = "carer-a948@example.test"
B_EMAIL = "carer-b948@example.test"
NOBODY_EMAIL = "stranger948@example.test"

DRIVE_THUMB = "idInvented948Thumb01"
DRIVE_NOPREVIEW = "idInvented948NoPrev1"
DRIVE_PDF = "idInvented948PdfOnly"
DRIVE_RAW = "idInvented948RawCsv1"
DRIVE_FLAGGED = "idInvented948Flagged"
DRIVE_FOREIGN = "idInvented948Foreign"
ROOT = "idInvented948RootDir"

FILENAMES = {
    "thumb": "20270311_Annabel_SynthClinic_Labs_PreviewThumbA.pdf",
    "noprev": "20270312_Annabel_SynthClinic_Other_PreviewNoneA.zip",
    "local": "20270313_Annabel_SynthClinic_Report_PreviewLocalA.pdf",
    "raw": "20270314_Annabel_SynthLab_Genetics_RawDataExport.csv",
    "flagged": "20270315_Annabel_SynthClinic_Report_PreviewFlaggedA.pdf",
    "pdf": "20270316_Annabel_SynthClinic_Report_PreviewRenderA.pdf",
    "foreign": "20270317_Bea_SynthClinic_Labs_PreviewForeignB.pdf",
}


def _png() -> bytes:
    import pymupdf

    tile = pymupdf.IRect(0, 0, 9, 13)  # an invented tiny tile
    pix = pymupdf.Pixmap(pymupdf.csRGB, tile, False)
    pix.set_rect(pix.irect, (37, 71, 149))
    return pix.tobytes("png")


def _tiny_pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=210, height=297)
    page.insert_text((20, 40), "Invented lab sheet 2027-04-01")
    page.draw_rect(pymupdf.Rect(20, 60, 190, 120), color=(0.1, 0.2, 0.5), width=1)
    data = doc.tobytes()
    doc.close()
    return data


PNG = _png()


def _thumb(image: bytes = PNG, mime: str = "application/pdf") -> dict:
    return {"mime": mime, "size": 2048, "trashed": False, "has_thumbnail": True,
            "image": image, "image_state": "ok"}  # fmt: skip


def _no_thumb(mime: str = "application/pdf", size: int | None = 2048) -> dict:
    return {"mime": mime, "size": size, "trashed": False, "has_thumbnail": False,
            "image": None, "image_state": "none"}  # fmt: skip


class FakeHttpError(Exception):
    """Shaped like googleapiclient's HttpError (``.resp.status``)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.resp = SimpleNamespace(status=status)


class FakeDrive:
    """Stands in for the patient's ``GDriveClient``: read methods only."""

    def __init__(self) -> None:
        self.thumbs: dict[str, dict | Exception] = {}
        self.files: dict[str, bytes | None] = {}
        self.calls: list[tuple[str, str, int]] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.delay = 0.0
        self.gate: threading.Event | None = None

    def _enter(self, method: str, drive_id: str) -> None:
        with self.lock:
            self.calls.append((method, drive_id, threading.get_ident()))
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def _leave(self) -> None:
        with self.lock:
            self.in_flight -= 1

    def get_thumbnail(self, drive_id, size_px, *, timeout, max_bytes):
        self._enter("get_thumbnail", drive_id)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.gate is not None:
                self.gate.wait(5)
            answer = self.thumbs.get(drive_id, _no_thumb("application/zip"))
            if isinstance(answer, Exception):
                raise answer
            return dict(answer)
        finally:
            self._leave()

    def download_capped(self, drive_id, mime, *, timeout, max_bytes):
        self._enter("download_capped", drive_id)
        try:
            return self.files.get(drive_id)
        finally:
            self._leave()

    def count(self, method: str | None = None) -> int:
        return sum(1 for m, _, _ in self.calls if method in (None, m))


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    doc_thumbnails._cache.clear()
    doc_thumbnails._semaphores_by_loop.clear()
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", True)
    yield
    doc_thumbnails._cache.clear()
    doc_thumbnails._semaphores_by_loop.clear()


@pytest.fixture
def drive(monkeypatch) -> FakeDrive:
    fake = FakeDrive()
    fake.thumbs[DRIVE_THUMB] = _thumb()
    fake.thumbs[DRIVE_FOREIGN] = _thumb()
    fake.thumbs[DRIVE_RAW] = _thumb()
    fake.thumbs[DRIVE_FLAGGED] = _thumb()
    fake.thumbs[DRIVE_PDF] = _no_thumb("application/pdf", 4096)
    fake.files[DRIVE_PDF] = _tiny_pdf()
    monkeypatch.setattr(
        server, "_create_patient_clients", AsyncMock(return_value=(fake, None, None, ROOT))
    )
    return fake


@pytest.fixture
async def docs(db: Database) -> dict[str, int]:
    """Six documents of patient A, one of patient B (ids 1..7: single digits)."""
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("thumb948-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "thumb948-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.commit()
    when = datetime(2027, 3, 18, 9, 30, tzinfo=UTC)
    rows = {
        "thumb": dict(file_id=f"gdrive:{DRIVE_THUMB}", gdrive_id=DRIVE_THUMB),
        "noprev": dict(file_id="file_948_noprev", gdrive_id=DRIVE_NOPREVIEW,
                       mime_type="application/zip"),
        "local": dict(file_id="file_948_local_only"),  # Files API only: never a source
        "raw": dict(file_id=f"gdrive:{DRIVE_RAW}", gdrive_id=DRIVE_RAW, mime_type="text/csv",
                    category=DocumentCategory.GENETICS),
        "flagged": dict(file_id=f"gdrive:{DRIVE_FLAGGED}", gdrive_id=DRIVE_FLAGGED),
        "pdf": dict(file_id=f"gdrive:{DRIVE_PDF}"),  # Drive id only in the file id
    }  # fmt: skip
    ids: dict[str, int] = {}
    for key, extra in rows.items():
        doc = await db.insert_document(
            make_doc(
                filename=FILENAMES[key],
                original_filename=FILENAMES[key],
                document_date=date(2027, 3, 11),
                gdrive_modified_time=when,
                **extra,
            ),
            patient_id=A_UUID,
        )
        ids[key] = doc.id
    await db.update_structured_metadata(ids["flagged"], json.dumps({"exclude_extract": True}))
    foreign = await db.insert_document(
        make_doc(
            file_id=f"gdrive:{DRIVE_FOREIGN}",
            gdrive_id=DRIVE_FOREIGN,
            filename=FILENAMES["foreign"],
            original_filename=FILENAMES["foreign"],
            document_date=date(2027, 4, 22),
        ),
        patient_id=B_UUID,
    )
    ids["foreign"] = foreign.id
    assert max(ids.values()) < 9  # the unknown id below has the same digit count
    return ids


UNKNOWN_ID = 9


def _request(db: Database, query: dict) -> MagicMock:
    request = MagicMock()
    request.query_params = query
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    return request


@contextmanager
def _caller(email: str | None, *, admin: bool = False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


async def _get(db, ids, *, size=None, patient=None, email=A_EMAIL, admin=False):
    query = {"ids": ",".join(str(i) for i in ids) if isinstance(ids, list) else ids}
    if size is not None:
        query["size"] = size
    if patient is not None:
        query["patient_id"] = patient
    with _caller(email, admin=admin):
        return await server.api_doc_thumbnails(_request(db, query))


def _thumbs(resp) -> dict:
    assert resp.status_code == 200, resp.body
    return json.loads(resp.body)["thumbnails"]


# ── the contract ─────────────────────────────────────────────────────────


async def test_own_document_gets_a_data_uri(docs, db, drive):
    resp = await _get(db, [docs["thumb"]])
    body = _thumbs(resp)
    assert list(body) == [str(docs["thumb"])]
    src = body[str(docs["thumb"])]["src"]
    assert src.startswith("data:image/png;base64,")
    assert resp.headers["cache-control"] == "private, no-store"
    assert drive.count("get_thumbnail") == 1
    assert drive.calls[0][1] == DRIVE_THUMB


async def test_batch_keeps_request_order_and_dedupes(docs, db, drive):
    order = [docs["local"], docs["thumb"], UNKNOWN_ID, docs["foreign"], docs["thumb"]]
    body = _thumbs(await _get(db, order))
    assert list(body) == [str(docs["local"]), str(docs["thumb"]), str(UNKNOWN_ID),
                          str(docs["foreign"])]  # fmt: skip
    assert body[str(docs["thumb"])] is not None
    assert [v for k, v in body.items() if k != str(docs["thumb"])] == [None, None, None]
    assert drive.count() == 1  # only the own Drive document reached Drive


async def test_unknown_foreign_and_no_preview_are_byte_identical(docs, db, drive):
    """#590: one null shape for an id that does not exist, another patient's
    id and an own document Drive has no preview of."""
    asked = (UNKNOWN_ID, docs["foreign"], docs["noprev"], docs["local"])
    responses = [await _get(db, [doc_id]) for doc_id in asked]
    norm = [r.body.replace(str(i).encode(), b"ID") for r, i in zip(responses, asked, strict=True)]
    assert len(set(norm)) == 1 and b"null" in norm[0]
    headers = {tuple(sorted(r.raw_headers)) for r in responses}
    assert len(headers) == 1  # same content-length, same cache-control
    assert {r.status_code for r in responses} == {200}
    assert DRIVE_FOREIGN not in [c[1] for c in drive.calls]  # never loaded


async def test_named_foreign_or_unknown_patient_is_all_null(docs, db, drive):
    ids = [docs["thumb"], docs["foreign"]]
    foreign = await _get(db, ids, patient="thumb948-b")
    unknown = await _get(db, ids, patient="thumb948-nobody")
    stranger = await _get(db, ids, email=NOBODY_EMAIL)
    assert foreign.body == unknown.body == stranger.body
    assert sorted(foreign.raw_headers) == sorted(unknown.raw_headers)
    assert set(_thumbs(foreign).values()) == {None}
    assert drive.count() == 0


async def test_caregiver_and_admin_see_the_named_patient(docs, db, drive):
    body = _thumbs(await _get(db, [docs["foreign"]], patient="thumb948-b", email=B_EMAIL))
    assert body[str(docs["foreign"])]["src"].startswith("data:image/png;base64,")
    body = _thumbs(
        await _get(db, [docs["foreign"]], patient=B_UUID, email=NOBODY_EMAIL, admin=True)
    )
    assert body[str(docs["foreign"])] is not None


async def test_unauthenticated_is_401(docs, db, drive):
    resp = await _get(db, [docs["thumb"]], email=None)
    assert resp.status_code == 401
    assert drive.count() == 0


@pytest.mark.parametrize(
    ("ids", "size", "code"),
    [
        ("", None, "ids_required"),
        (",", None, "ids_required"),
        ("abc", None, "invalid_ids"),
        ("12,x", None, "invalid_ids"),
        ("0", None, "invalid_ids"),
        ("-3", None, "invalid_ids"),
        ("1.5", None, "invalid_ids"),
        ("٣", None, "invalid_ids"),  # a non-ASCII digit
        ("1234567890123456", None, "invalid_ids"),
        (",".join(str(i) for i in range(1, 26)), None, "too_many_ids"),
        ("1", "xl", "invalid_size"),
        ("1,2", "l", "too_many_ids_for_size"),
    ],
)
async def test_malformed_queries_are_400_with_a_code(docs, db, drive, ids, size, code):
    resp = await _get(db, ids, size=size)
    assert resp.status_code == 400
    assert json.loads(resp.body) == {"error": code}
    assert drive.count() == 0


async def test_id_caps_accept_their_maximum(docs, db, drive):
    body = _thumbs(await _get(db, list(range(1, 25))))
    assert len(body) == doc_thumbnails.MAX_IDS == 24
    large = _thumbs(await _get(db, [docs["thumb"]], size="l"))
    assert large[str(docs["thumb"])] is not None
    assert doc_thumbnails.MAX_IDS_LARGE == 1


async def test_kill_switch_answers_null_without_db_or_drive(docs, db, drive, monkeypatch):
    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", False)
    with patch.object(db, "get_document", AsyncMock(wraps=db.get_document)) as get_document:
        body = _thumbs(await _get(db, [docs["thumb"], docs["pdf"]]))
    assert set(body.values()) == {None}
    get_document.assert_not_awaited()
    assert drive.count() == 0
    server._create_patient_clients.assert_not_awaited()


async def test_rows_without_an_own_drive_copy_are_null(docs, db, drive):
    body = _thumbs(await _get(db, [docs["local"]]))
    assert body == {str(docs["local"]): None}
    assert drive.count() == 0
    server._create_patient_clients.assert_not_awaited()  # nothing to fetch, no client


async def test_soft_deleted_document_is_null(docs, db, drive):
    await db.db.execute(
        "UPDATE documents SET deleted_at = '2027-05-01T00:00:00' WHERE id = ?", (docs["thumb"],)
    )
    await db.db.commit()
    assert _thumbs(await _get(db, [docs["thumb"]])) == {str(docs["thumb"]): None}
    assert drive.count() == 0


# ── infra point 3: excluded documents are never loaded ───────────────────


async def test_excluded_documents_never_reach_drive(docs, db, drive):
    """A raw genotype export and an ``exclude_extract: true`` row: null, and
    Drive is not even asked (both have a Drive thumbnail in the stub)."""
    body = _thumbs(await _get(db, [docs["raw"], docs["flagged"]]))
    assert set(body.values()) == {None}
    assert drive.count() == 0
    server._create_patient_clients.assert_not_awaited()


def test_exclusion_rule_is_the_extraction_rule():
    doc = make_doc(file_id="gdrive:idInvented948Rule01", gdrive_id="idInvented948Rule01")
    assert doc_thumbnails.excluded(doc) is False
    raw = make_doc(filename="20270101_X_Lab_Other_Export.txt", mime_type="text/plain")
    assert doc_thumbnails.excluded(raw) is True  # any raw text export
    flagged = make_doc(structured_metadata=json.dumps({"curated": {"exclude_extract": "yes"}}))
    assert doc_thumbnails.excluded(flagged) is True
    src = inspect.getsource(doc_thumbnails.excluded)
    assert "extraction_exclusion" in src and "is_raw_export" in src


# ── cache ────────────────────────────────────────────────────────────────


async def test_second_request_is_served_from_the_cache(docs, db, drive):
    first = await _get(db, [docs["thumb"]])
    second = await _get(db, [docs["thumb"]])
    assert first.body == second.body
    assert drive.count() == 1


async def test_cache_key_follows_the_drive_copy(docs, db, drive):
    await _get(db, [docs["thumb"]])
    await db.db.execute(
        "UPDATE documents SET gdrive_modified_time = ? WHERE id = ?",
        ("2027-06-02T08:00:00+00:00", docs["thumb"]),
    )
    await db.db.commit()
    await _get(db, [docs["thumb"]])
    assert drive.count() == 2  # a changed file is fetched again


async def test_cache_is_per_patient_and_size(docs, db, drive):
    await _get(db, [docs["thumb"]])
    await _get(db, [docs["thumb"]], size="l")
    assert drive.count() == 2
    keys = list(doc_thumbnails._cache._items)
    assert {k[0] for k in keys} == {A_UUID} and {k[2] for k in keys} == {"s", "l"}


async def test_transient_failures_are_null_not_cached_and_not_retried(docs, db, drive):
    drive.thumbs[DRIVE_THUMB] = FakeHttpError(429)
    assert _thumbs(await _get(db, [docs["thumb"]])) == {str(docs["thumb"]): None}
    assert drive.count() == 1  # one attempt, no retry loop
    drive.thumbs[DRIVE_THUMB] = FakeHttpError(500)
    assert _thumbs(await _get(db, [docs["thumb"]]))[str(docs["thumb"])] is None
    assert drive.count() == 2
    drive.thumbs[DRIVE_THUMB] = TimeoutError()
    assert _thumbs(await _get(db, [docs["thumb"]]))[str(docs["thumb"])] is None
    drive.thumbs[DRIVE_THUMB] = _thumb()
    assert _thumbs(await _get(db, [docs["thumb"]]))[str(docs["thumb"])] is not None
    assert drive.count() == 4  # nothing of the failures was cached


async def test_thumbnail_drive_has_but_could_not_fetch_is_null_without_fallback(docs, db, drive):
    drive.thumbs[DRIVE_THUMB] = {**_thumb(), "image": None, "image_state": "http_403"}
    assert _thumbs(await _get(db, [docs["thumb"]]))[str(docs["thumb"])] is None
    assert drive.count("download_capped") == 0


async def test_non_image_bytes_are_never_served(docs, db, drive):
    drive.thumbs[DRIVE_THUMB] = _thumb(image=b"<svg xmlns='http://www.w3.org/2000/svg'/>")
    assert _thumbs(await _get(db, [docs["thumb"]]))[str(docs["thumb"])] is None


def test_preview_cache_lru_byte_cap_and_ttl():
    now = [5000.0]
    overhead = doc_thumbnails.CACHE_ENTRY_OVERHEAD
    image = b"x" * 777
    unit = len(image) + overhead  # what one entry costs
    cache = doc_thumbnails.PreviewCache(max_bytes=unit * 3, ttl_s=90, clock=lambda: now[0])
    for k in "abc":
        cache.put((k,), image, "image/png")
    assert len(cache) == 3
    assert cache.bytes == unit * 3
    assert cache.get(("a",)) is not None  # a is now the most recently used
    cache.put(("d",), image, "image/png")  # evicts the least recently used: b
    assert cache.get(("b",)) is None and cache.get(("a",)) is not None
    assert cache.bytes <= cache.max_bytes
    cache.put(("big",), b"z" * (unit * 4), "image/png")  # bigger than the whole cap
    assert cache.get(("big",)) is None and len(cache) == len("acd")
    cache.put(("a",), None, None)  # replacing an entry re-counts its bytes
    assert cache.bytes == unit * 2 + overhead
    now[0] += 91
    assert cache.get(("c",)) is None and cache.get(("d",)) is None  # TTL
    assert len(cache) == 1 and cache.bytes == overhead  # only the untouched "a" is left


def test_cache_constants_are_documented_limits():
    assert doc_thumbnails.CACHE_MAX_BYTES <= 32 * 1024 * 1024
    assert doc_thumbnails.CACHE_TTL_S <= 3600
    assert doc_thumbnails.DRIVE_CONCURRENCY == 4
    assert doc_thumbnails.FALLBACK_MAX_DOWNLOAD_BYTES <= 10 * 1024 * 1024


# ── Drive load ───────────────────────────────────────────────────────────


async def test_at_most_four_drive_requests_at_once(docs, db, drive):
    """A full batch of 24 Drive documents of one patient."""
    drive.delay = 0.03
    many = [docs["thumb"], docs["pdf"]]
    extra = []
    for n in range(22):
        drive_id = f"idInvented948Many{n:03d}"
        drive.thumbs[drive_id] = _thumb()
        doc = await db.insert_document(
            make_doc(file_id=f"gdrive:{drive_id}", gdrive_id=drive_id,
                     filename=f"20270401_Annabel_SynthClinic_Labs_Many{n:03d}.pdf"),
            patient_id=A_UUID,
        )  # fmt: skip
        extra.append(doc.id)
    body = _thumbs(await _get(db, many + extra))
    assert len(body) == 24
    assert drive.max_in_flight <= doc_thumbnails.DRIVE_CONCURRENCY
    assert drive.max_in_flight > 1  # they did run side by side


async def test_a_slow_drive_makes_the_id_null_by_the_deadline(docs, db, drive, monkeypatch):
    monkeypatch.setattr(doc_thumbnails, "BATCH_DEADLINE_S", 0.2)
    drive.gate = threading.Event()
    started = time.monotonic()
    body = _thumbs(await _get(db, [docs["thumb"]]))
    assert body == {str(docs["thumb"]): None}
    assert time.monotonic() - started < 2
    drive.gate.set()  # let the worker finish; its slot is released when it ends
    for _ in range(50):
        if drive.in_flight == 0:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    drive_sem, _ = doc_thumbnails._semaphores()
    assert drive_sem._value == doc_thumbnails.DRIVE_CONCURRENCY


# ── fallback: local render of page 1 ─────────────────────────────────────


async def test_fallback_renders_page_one_when_drive_has_no_thumbnail(docs, db, drive):
    body = _thumbs(await _get(db, [docs["pdf"]]))
    src = body[str(docs["pdf"])]["src"]
    assert src.startswith("data:image/jpeg;base64,")
    import base64

    image = base64.b64decode(src.split(",", 1)[1])
    assert image[:3] == b"\xff\xd8\xff"
    assert len(image) <= doc_thumbnails.MAX_IMAGE_BYTES["s"]
    assert drive.count("download_capped") == 1
    # the Drive id came from the gdrive:<id> file id (the row has no gdrive_id)
    assert {c[1] for c in drive.calls} == {DRIVE_PDF}


async def test_fallback_is_skipped_while_a_curated_run_is_active(docs, db, drive, monkeypatch):
    """Infra point 5: no download + render during a curated run or re-OCR; the
    cheap thumbnail fetch still runs, and the skipped answer is not cached."""
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: True)
    body = _thumbs(await _get(db, [docs["pdf"], docs["thumb"]]))
    assert body[str(docs["pdf"])] is None
    assert body[str(docs["thumb"])] is not None
    assert drive.count("get_thumbnail") == 2 and drive.count("download_capped") == 0
    monkeypatch.setattr("oncofiles.tools.curated.run_active", lambda: False)
    assert _thumbs(await _get(db, [docs["pdf"]]))[str(docs["pdf"])] is not None
    assert drive.count("download_capped") == 1


async def test_unknown_run_state_counts_as_active(docs, db, drive, monkeypatch):
    def broken():
        raise RuntimeError("invented")

    monkeypatch.setattr("oncofiles.tools.curated.run_active", broken)
    assert _thumbs(await _get(db, [docs["pdf"]]))[str(docs["pdf"])] is None
    assert drive.count("download_capped") == 0


async def test_fallback_refuses_big_or_unsized_files(docs, db, drive):
    drive.thumbs[DRIVE_PDF] = _no_thumb(
        "application/pdf", doc_thumbnails.FALLBACK_MAX_DOWNLOAD_BYTES + 1
    )
    assert _thumbs(await _get(db, [docs["pdf"]]))[str(docs["pdf"])] is None
    doc_thumbnails._cache.clear()
    drive.thumbs[DRIVE_PDF] = _no_thumb("application/pdf", None)
    assert _thumbs(await _get(db, [docs["pdf"]]))[str(docs["pdf"])] is None
    assert drive.count("download_capped") == 0


async def test_unsupported_type_without_thumbnail_is_null(docs, db, drive):
    assert _thumbs(await _get(db, [docs["noprev"]]))[str(docs["noprev"])] is None
    assert drive.count("download_capped") == 0


def test_render_first_page_handles_pdf_images_and_garbage():
    jpeg = doc_thumbnails.render_first_page(_tiny_pdf(), "application/pdf", 220)
    assert jpeg and jpeg[:3] == b"\xff\xd8\xff"
    import pymupdf

    pix = pymupdf.Pixmap(jpeg)
    assert pix.width <= 220 and pix.height <= 330
    assert doc_thumbnails.render_first_page(PNG, "image/png", 220)[:3] == b"\xff\xd8\xff"
    assert doc_thumbnails.render_first_page(b"not a pdf", "application/pdf", 220) is None
    ihdr_length = len("width height bits")  # any length: only the size fields are read
    huge_png_header = (
        b"\x89PNG\r\n\x1a\n"
        + ihdr_length.to_bytes(4, "big")
        + b"IHDR"
        + (19999).to_bytes(4, "big")
        + (17777).to_bytes(4, "big")
    )
    assert doc_thumbnails.render_first_page(huge_png_header, "image/png", 220) is None


# ── threads and the DB ───────────────────────────────────────────────────


async def test_db_on_the_event_loop_drive_in_worker_threads(docs, db, drive):
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = db.get_document

    async def recording(doc_id, *, patient_id):
        seen.append(threading.get_ident())
        return await real(doc_id, patient_id=patient_id)

    with patch.object(db, "get_document", recording):
        await _get(db, [docs["thumb"], docs["pdf"], docs["local"]])
    assert seen and set(seen) == {loop_thread}
    assert drive.calls and loop_thread not in {c[2] for c in drive.calls}


# ── logs ─────────────────────────────────────────────────────────────────


async def test_logs_carry_no_filename_and_no_link(docs, db, drive, caplog):
    drive.thumbs[DRIVE_NOPREVIEW] = FakeHttpError(429)
    with caplog.at_level(logging.DEBUG):
        await _get(db, [docs[k] for k in ("thumb", "noprev", "local", "raw", "pdf")])
    text = caplog.text
    assert "doc-thumbnails:" in text
    for name in FILENAMES.values():
        assert name not in text
        assert name.rsplit(".", 1)[0] not in text
    assert "googleusercontent" not in text and "base64" not in text
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oncofiles"))
    assert A_UUID not in ours  # the patient id is logged as a prefix only
    assert f"patient={A_UUID[:4]} " in ours


# ── GDriveClient read methods ────────────────────────────────────────────


class _Resp(dict):
    """Shaped like ``httplib2.Response``: a header dict with ``.status``."""

    def __init__(self, status: int, headers: dict | None = None) -> None:
        super().__init__(headers or {})
        self.status = status


class _FakeHttp:
    def __init__(self, status=200, body=None, redirects: list[str] | None = None):
        self.status, self.body = status, PNG if body is None else body
        self.redirects = list(redirects or [])
        self.requested: list[str] = []
        self.follow_redirects = True

    def request(self, url, method="GET", **_kw):
        self.requested.append(url)
        if self.redirects:
            assert self.follow_redirects is False  # never let httplib2 follow with our token
            return _Resp(302, {"location": self.redirects.pop(0)}), b""
        return _Resp(self.status), self.body


def _client_with(meta: dict, http: _FakeHttp, monkeypatch) -> tuple[GDriveClient, MagicMock]:
    from oncofiles import google_service

    @contextmanager
    def fake_http(_creds, timeout):
        assert timeout == 3.0
        yield http

    monkeypatch.setattr(google_service, "short_lived_http", fake_http)
    client = object.__new__(GDriveClient)
    service = MagicMock()
    service.files.return_value.get.return_value.execute.return_value = meta
    client._service = service
    return client, service


LINK = "https://lh3.googleusercontent.com/drive-storage/InventedToken948=s220"


def test_get_thumbnail_fetches_the_sized_link_and_never_returns_it(monkeypatch):
    http = _FakeHttp()
    meta = {"mimeType": "application/pdf", "size": "2048", "thumbnailLink": LINK}
    client, service = _client_with(meta, http, monkeypatch)
    out = client.get_thumbnail("idInvented948Read01", 1000, timeout=3.0, max_bytes=10_000)
    assert out["image"] == PNG and out["image_state"] == "ok" and out["size"] == 2048
    assert http.requested == [LINK.replace("=s220", "=s1000")]
    assert "lh3" not in json.dumps({k: v for k, v in out.items() if k != "image"})
    execute = service.files.return_value.get.return_value.execute
    execute.assert_called_once_with(http=http, num_retries=0)


@pytest.mark.parametrize(
    ("meta", "http", "state"),
    [
        ({"mimeType": "application/pdf", "thumbnailLink": "http://lh3.googleusercontent.com/x=s220"},
         _FakeHttp(), "bad_link"),
        ({"mimeType": "application/pdf", "thumbnailLink": "https://evil.example.test/x=s220"},
         _FakeHttp(), "bad_link"),
        ({"mimeType": "application/pdf", "thumbnailLink": "https://googleusercontent.com.example.test/x"},
         _FakeHttp(), "bad_link"),
        ({"mimeType": "application/pdf", "thumbnailLink": LINK}, _FakeHttp(status=404), "http_404"),
        ({"mimeType": "application/pdf", "thumbnailLink": LINK}, _FakeHttp(body=b""), "empty"),
        ({"mimeType": "application/pdf", "thumbnailLink": LINK},
         _FakeHttp(body=b"\x89PNG" + b"0" * 20_000), "too_large"),
        ({"mimeType": "application/pdf"}, _FakeHttp(), "none"),
        ({"mimeType": "application/pdf", "thumbnailLink": LINK, "trashed": True},
         _FakeHttp(), "none"),
    ],
)  # fmt: skip
def test_get_thumbnail_refusals(monkeypatch, meta, http, state):
    client, _ = _client_with(meta, http, monkeypatch)
    out = client.get_thumbnail("idInvented948Read02", 220, timeout=3.0, max_bytes=10_000)
    assert out["image"] is None and out["image_state"] == state
    if state in ("bad_link", "none"):
        assert http.requested == []  # the link is never followed off Google's hosts


class _ScriptedHttp:
    """Answers the real googleapiclient requests: JSON metadata, media, the image."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.follow_redirects = True

    def request(self, uri, method="GET", body=None, headers=None, **_kw):
        self.calls.append((method, uri))
        if "alt=media" in uri or "/export" in uri:
            return _Resp(200, {"content-type": "application/pdf"}), b"%PDF-1.7 invented 948"
        if "/drive/v3/files/" in uri:
            meta = {"mimeType": "application/pdf", "size": "2048", "thumbnailLink": LINK}
            return _Resp(200, {"content-type": "application/json"}), json.dumps(meta).encode()
        return _Resp(200, {"content-type": "image/png"}), PNG


def test_read_methods_work_with_the_real_discovery_client(monkeypatch):
    """The real request objects (bundled discovery, no network) execute on the
    short-lived Http with the expected query."""
    from oncofiles import google_service

    http = _ScriptedHttp()

    @contextmanager
    def fake_http(_creds, timeout):
        yield http

    monkeypatch.setattr(google_service, "short_lived_http", fake_http)
    client = GDriveClient.from_oauth("access", "refresh", "client-id", "client-secret")
    out = client.get_thumbnail("idInvented948Real01", 220, timeout=3.0, max_bytes=10_000)
    assert out["image"] == PNG and out["size"] == 2048
    meta_uri = http.calls[0][1]
    assert "/drive/v3/files/idInvented948Real01" in meta_uri
    assert "supportsAllDrives=true" in meta_uri and "thumbnailLink" in meta_uri
    assert http.calls[1] == ("GET", LINK)
    assert all(method == "GET" for method, _ in http.calls)  # reads only
    pdf = client.download_capped(
        "idInvented948Real01", "application/pdf", timeout=3.0, max_bytes=10_000
    )
    assert pdf == b"%PDF-1.7 invented 948" and "alt=media" in http.calls[-1][1]
    exported = client.download_capped(
        "idInvented948Real01", "application/vnd.google-apps.document", timeout=3.0, max_bytes=999
    )
    assert exported and "/export" in http.calls[-1][1]


def test_get_thumbnail_follows_redirects_only_within_google(monkeypatch):
    meta = {"mimeType": "application/pdf", "thumbnailLink": LINK}
    inside = _FakeHttp(redirects=["https://lh4.googleusercontent.com/moved948=s220"])
    client, _ = _client_with(meta, inside, monkeypatch)
    out = client.get_thumbnail("idInvented948Read05", 220, timeout=3.0, max_bytes=10_000)
    assert out["image_state"] == "ok"
    assert inside.requested[1] == "https://lh4.googleusercontent.com/moved948=s220"

    outside = _FakeHttp(redirects=["https://collector.example.test/steal948"])
    client, _ = _client_with(meta, outside, monkeypatch)
    out = client.get_thumbnail("idInvented948Read06", 220, timeout=3.0, max_bytes=10_000)
    assert out["image"] is None and out["image_state"] == "http_302"
    assert len(outside.requested) == 1  # the off-Google location is never requested

    loop = _FakeHttp(redirects=[f"https://lh3.googleusercontent.com/hop{i}" for i in range(5)])
    client, _ = _client_with(meta, loop, monkeypatch)
    out = client.get_thumbnail("idInvented948Read07", 220, timeout=3.0, max_bytes=10_000)
    assert out["image"] is None and len(loop.requested) == 3  # bounded hops


def test_get_thumbnail_does_not_retry(monkeypatch):
    client, service = _client_with({}, _FakeHttp(), monkeypatch)
    service.files.return_value.get.return_value.execute.side_effect = FakeHttpError(429)
    with pytest.raises(FakeHttpError):
        client.get_thumbnail("idInvented948Read03", 220, timeout=3.0, max_bytes=10_000)
    assert service.files.return_value.get.return_value.execute.call_count == 1
    assert not hasattr(GDriveClient.get_thumbnail, "__wrapped__")  # no retry decorator
    assert not hasattr(GDriveClient.download_capped, "__wrapped__")


def test_download_capped_exports_google_docs_and_caps_size(monkeypatch):
    client, service = _client_with({}, _FakeHttp(), monkeypatch)
    files = service.files.return_value
    files.get_media.return_value.execute.return_value = b"%PDF-1.7 invented"
    files.export_media.return_value.execute.return_value = b"%PDF-1.7 exported"
    assert client.download_capped("idInvented948Read04", "application/pdf", timeout=3.0,
                                  max_bytes=100) == b"%PDF-1.7 invented"  # fmt: skip
    assert (
        client.download_capped(
            "idInvented948Read04",
            "application/vnd.google-apps.document",
            timeout=3.0,
            max_bytes=100,
        )
        == b"%PDF-1.7 exported"
    )
    assert client.download_capped("idInvented948Read04", "application/pdf", timeout=3.0,
                                  max_bytes=5) is None  # fmt: skip
    for request in (files.get_media.return_value, files.export_media.return_value):
        kwargs = request.execute.call_args.kwargs
        assert kwargs["num_retries"] == 0 and isinstance(kwargs["http"], _FakeHttp)


def test_sized_thumbnail_url():
    assert _sized_thumbnail_url(LINK, 1000).endswith("=s1000")
    assert _sized_thumbnail_url("https://drive.google.com/thumbnail?id=x", 220) == (
        "https://drive.google.com/thumbnail?id=x"
    )
    assert _sized_thumbnail_url("javascript:alert(1)", 220) is None
    assert _sized_thumbnail_url("", 220) is None


def test_short_lived_http_has_its_own_timeout_and_is_closed():
    from google.oauth2.credentials import Credentials

    from oncofiles import google_service

    creds = Credentials(token="invented-token-948")
    with google_service.short_lived_http(creds, 3.5) as http:
        assert http.timeout == 3.5
        closed = []
        http.http.close = lambda: closed.append(True)
    assert closed == [True]


# ── source locks ─────────────────────────────────────────────────────────


def test_module_never_touches_files_api_cloud_or_writes():
    tree = ast.parse((SRC / "doc_thumbnails.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    imports = {(n.module or "") for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    from tests.test_curated_folder import WRITE_CALLS

    assert not names & set(WRITE_CALLS), names & set(WRITE_CALLS)
    assert not names & {"contract_writes", "_get_client", "FilesClient", "download", "commit",
                        "execute", "upload_document"}  # fmt: skip
    assert not any(m.startswith(("oncofiles.files_api", "oncofiles.enhance", "anthropic"))
                   for m in imports)  # fmt: skip


def test_route_and_module_use_the_scoped_lookup_and_the_acl_helper():
    route = inspect.getsource(server.api_doc_thumbnails)
    assert "_get_dashboard_patient_id(request, fallback_to_own=False)" in route
    assert "private, no-store" in route
    batch = inspect.getsource(doc_thumbnails.thumbnails_for)
    assert "get_document(doc_id, patient_id=patient_id)" in batch
    assert "gather" in batch and batch.index("get_document") < batch.index("gather")


def _pdf_with_image(side: int) -> bytes:
    import pymupdf

    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, side, side), False)
    pix.clear_with(200)
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=280)
    page.insert_image(pymupdf.Rect(10, 10, 190, 190), stream=pix.tobytes("png"))
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_with_an_oversized_embedded_image_is_never_rendered(monkeypatch):
    """Decompression-bomb guard: page 1 image dimensions are checked from the
    image dictionary before anything is rasterised."""
    pdf = _pdf_with_image(40)  # 1600 px
    assert doc_thumbnails.render_first_page(pdf, "application/pdf", 220) is not None
    monkeypatch.setattr(doc_thumbnails, "FALLBACK_MAX_PIXELS", 1000)
    assert doc_thumbnails.render_first_page(pdf, "application/pdf", 220) is None


# ── text notes: a local preview of the stored page text (#948 follow-up) ──

NOTE_MD = "20270320_Annabel_Advocate_Advocate_InventedVisitNotes.md"
NOTE_TXT = "20270321_Annabel_Advocate_Advocate_InventedCallNotes.txt"
NOTE_FOREIGN = "20270322_Bea_Advocate_Advocate_InventedForeignNotes.md"
DRIVE_NOTE = "idInvented948NoteMd1"
# An invented sentence that must never show up in a log line.
NOTE_SECRET = "Synteticka poznamka: kontrola 2027-05-04, otázka na lekára č. 7 ľťž"
NOTE_TEXT = "---\nkind: advocate_note\n---\n# Invented visit\n\n" + "\n".join(
    [NOTE_SECRET] + [f"Line {n} of an invented note" for n in range(40)]
)


async def _note(db, filename, mime, *, patient_id=A_UUID, text=NOTE_TEXT, **extra) -> int:
    doc = await db.insert_document(
        make_doc(
            filename=filename,
            original_filename=filename,
            mime_type=mime,
            document_date=date(2027, 3, 20),
            category=DocumentCategory.ADVOCATE,
            **extra,
        ),
        patient_id=patient_id,
    )
    if text is not None:
        await db.save_ocr_page(doc.id, 1, text, "invented-text")
    return doc.id


def _jpeg_of(src: str) -> bytes:
    import base64

    assert src.startswith("data:image/jpeg;base64,")
    data = base64.b64decode(src.split(",", 1)[1])
    assert data[:3] == b"\xff\xd8\xff"
    return data


async def test_text_note_gets_a_jpeg_from_its_stored_text(docs, db, drive):
    md = await _note(
        db, NOTE_MD, "text/markdown", file_id=f"gdrive:{DRIVE_NOTE}", gdrive_id=DRIVE_NOTE
    )
    txt = await _note(db, NOTE_TXT, "text/plain", file_id="file_948_note_txt")
    body = _thumbs(await _get(db, [md, txt, docs["thumb"]]))
    _jpeg_of(body[str(md)]["src"])
    _jpeg_of(body[str(txt)]["src"])
    assert body[str(docs["thumb"])] is not None  # other rows go the Drive way as before
    assert [c[1] for c in drive.calls] == [DRIVE_THUMB]  # the notes never reached Drive
    large = _thumbs(await _get(db, [md], size="l"))
    assert len(_jpeg_of(large[str(md)]["src"])) > len(_jpeg_of(body[str(md)]["src"]))


async def test_text_note_preview_needs_no_google(docs, db, drive, monkeypatch):
    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", False)
    md = await _note(db, NOTE_MD, "text/markdown", file_id="file_948_note_md")
    body = _thumbs(await _get(db, [md]))
    _jpeg_of(body[str(md)]["src"])
    assert drive.count() == 0


async def test_text_note_logs_carry_no_text(docs, db, drive, caplog):
    md = await _note(db, NOTE_MD, "text/markdown", file_id="file_948_note_md")
    with caplog.at_level(logging.DEBUG):
        body = _thumbs(await _get(db, [md]))
    assert body[str(md)] is not None
    text = caplog.text
    for fragment in (NOTE_SECRET, "Synteticka", "Invented visit", "advocate_note", NOTE_MD):
        assert fragment not in text
    assert "text_ok" in text  # reason codes only


async def test_foreign_text_note_is_null(docs, db, drive):
    foreign = await _note(db, NOTE_FOREIGN, "text/markdown", patient_id=B_UUID,
                          file_id="file_948_note_foreign")  # fmt: skip
    own_view = _thumbs(await _get(db, [foreign]))
    named = _thumbs(await _get(db, [foreign], patient="thumb948-b"))
    assert own_view == named == {str(foreign): None}
    body = _thumbs(await _get(db, [foreign], patient="thumb948-b", email=B_EMAIL))
    _jpeg_of(body[str(foreign)]["src"])  # the caregiver of B sees it


async def test_text_preview_runs_the_exclusion_rule_on_the_text(docs, db, drive):
    genotype = "\n".join(f"rs{1000 + n}\tAG" for n in range(5))
    raw = await _note(db, NOTE_TXT, "text/plain", file_id="file_948_note_rs", text=genotype)
    flagged = await _note(db, NOTE_MD, "text/markdown", file_id="file_948_note_flag")
    await db.update_structured_metadata(flagged, json.dumps({"exclude_extract": True}))
    csv = await _note(db, "20270323_Annabel_Lab_Other_Export.csv", "text/plain",
                      file_id="file_948_note_csv", text="a,b\n1,2\n")  # fmt: skip
    body = _thumbs(await _get(db, [raw, flagged, csv]))
    assert set(body.values()) == {None}
    assert drive.count() == 0


async def test_text_note_without_stored_text_is_unchanged(docs, db, drive):
    md = await _note(db, NOTE_MD, "text/markdown", file_id=f"gdrive:{DRIVE_NOTE}",
                     gdrive_id=DRIVE_NOTE, text=None)  # fmt: skip
    assert _thumbs(await _get(db, [md])) == {str(md): None}
    assert [c[1] for c in drive.calls] == [DRIVE_NOTE]  # the Drive path, as before


async def test_edited_note_gets_a_new_preview(docs, db, drive):
    md = await _note(db, NOTE_MD, "text/markdown", file_id="file_948_note_md")
    first = _thumbs(await _get(db, [md]))[str(md)]["src"]
    assert _thumbs(await _get(db, [md]))[str(md)]["src"] == first  # cached
    await db.save_ocr_page(md, 1, "# Invented\n\nA different invented line 2027-06-01", "x")
    assert _thumbs(await _get(db, [md]))[str(md)]["src"] != first


def test_text_like_and_preview_lines():
    assert doc_thumbnails.is_text_like(make_doc(filename="n.md", mime_type="text/markdown"))
    assert doc_thumbnails.is_text_like(make_doc(filename="n.txt", mime_type="text/plain"))
    assert doc_thumbnails.is_text_like(
        make_doc(filename="n.md", mime_type="application/octet-stream")
    )
    for name, mime in (("x.csv", "text/plain"), ("x.tsv", "text/plain"),
                       ("x.vcf", "text/plain"), ("x.dat", "text/csv"),
                       ("x.pdf", "application/pdf")):  # fmt: skip
        assert not doc_thumbnails.is_text_like(
            make_doc(filename=name, original_filename=name, mime_type=mime)
        )
    lines = doc_thumbnails.preview_lines(NOTE_TEXT)
    assert len(lines) == doc_thumbnails.TEXT_PREVIEW_LINES
    assert lines[0] == "# Invented visit"  # front matter dropped
    assert all(len(line) <= doc_thumbnails.TEXT_PREVIEW_LINE_CHARS for line in lines)
    assert doc_thumbnails.render_text_preview("   \n\n", 220) is None
    jpeg = doc_thumbnails.render_text_preview(NOTE_SECRET, 220)
    assert jpeg and jpeg[:3] == b"\xff\xd8\xff"
