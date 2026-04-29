"""Tests for the export_document_package tool."""

import json
from datetime import date

import pytest

from tests.helpers import ERIKA_UUID, make_doc, make_treatment_event


class TestExportDocumentPackage:
    """Tests for export_document_package data assembly."""

    @pytest.mark.asyncio
    async def test_empty_database(self, db):
        docs = await db.list_documents(limit=200, patient_id=ERIKA_UUID)
        assert len(docs) == 0

    @pytest.mark.asyncio
    async def test_documents_grouped_by_category(self, db):
        from oncofiles.models import DocumentCategory

        await db.insert_document(
            make_doc(file_id="f1", category=DocumentCategory.LABS, filename="labs1.pdf"),
            patient_id=ERIKA_UUID,
        )
        await db.insert_document(
            make_doc(file_id="f2", category=DocumentCategory.LABS, filename="labs2.pdf"),
            patient_id=ERIKA_UUID,
        )
        await db.insert_document(
            make_doc(
                file_id="f3",
                category=DocumentCategory.IMAGING,
                filename="img1.pdf",
            ),
            patient_id=ERIKA_UUID,
        )

        docs = await db.list_documents(limit=200, patient_id=ERIKA_UUID)
        by_cat: dict[str, list] = {}
        for d in docs:
            cat = d.category.value
            if cat not in by_cat:
                by_cat[cat] = []
            by_cat[cat].append(d)

        assert "labs" in by_cat
        assert len(by_cat["labs"]) == 2
        assert "imaging" in by_cat
        assert len(by_cat["imaging"]) == 1

    @pytest.mark.asyncio
    async def test_treatment_timeline_included(self, db):
        await db.insert_treatment_event(
            make_treatment_event(
                event_date=date(2026, 2, 13),
                event_type="chemo",
                title="FOLFOX C1",
            ),
            patient_id=ERIKA_UUID,
        )
        await db.insert_treatment_event(
            make_treatment_event(
                event_date=date(2026, 2, 27),
                event_type="chemo",
                title="FOLFOX C2",
            ),
            patient_id=ERIKA_UUID,
        )

        events = await db.get_treatment_events_timeline(patient_id=ERIKA_UUID)
        assert len(events) == 2
        assert events[0].title == "FOLFOX C1"
        assert events[1].title == "FOLFOX C2"

    @pytest.mark.asyncio
    async def test_structured_metadata_in_export(self, db):
        doc = await db.insert_document(make_doc(file_id="f_meta"), patient_id=ERIKA_UUID)
        metadata = json.dumps({"document_type": "lab_report", "findings": ["anemia"]})
        await db.update_structured_metadata(doc.id, metadata)

        fetched = await db.get_document(doc.id, patient_id=ERIKA_UUID)
        assert fetched.structured_metadata is not None
        parsed = json.loads(fetched.structured_metadata)
        assert parsed["document_type"] == "lab_report"

    @pytest.mark.asyncio
    async def test_new_categories_in_export(self, db):
        from oncofiles.models import DocumentCategory

        for cat in [
            DocumentCategory.IMAGING,
            DocumentCategory.GENETICS,
            DocumentCategory.CHEMO_SHEET,
            DocumentCategory.SURGICAL_REPORT,
            DocumentCategory.DISCHARGE_SUMMARY,
        ]:
            await db.insert_document(
                make_doc(file_id=f"f_{cat.value}", category=cat, filename=f"{cat.value}.pdf"),
                patient_id=ERIKA_UUID,
            )

        docs = await db.list_documents(limit=200, patient_id=ERIKA_UUID)
        categories = {d.category.value for d in docs}
        assert "imaging" in categories
        assert "genetics" in categories
        assert "chemo_sheet" in categories
        assert "surgical_report" in categories
        assert "discharge_summary" in categories


class TestBaselineLabsCheck:
    """Tests for the baseline labs availability check."""

    @pytest.mark.asyncio
    async def test_no_treatment_events_returns_none(self, db):
        from oncofiles.server import _check_baseline_labs

        result = await _check_baseline_labs(db, patient_id=ERIKA_UUID)
        assert result is None

    @pytest.mark.asyncio
    async def test_baseline_labs_missing(self, db):
        from oncofiles.server import _check_baseline_labs

        # Add chemo event but no pre-treatment labs
        await db.insert_treatment_event(
            make_treatment_event(
                event_date=date(2026, 2, 13),
                event_type="chemo",
                title="FOLFOX C1",
            ),
            patient_id=ERIKA_UUID,
        )

        result = await _check_baseline_labs(db, patient_id=ERIKA_UUID)
        assert result is not None
        assert "BASELINE LABS MISSING" in result
        assert "2026-02-13" in result

    @pytest.mark.asyncio
    async def test_baseline_labs_present(self, db):
        from oncofiles.server import _check_baseline_labs

        # Add chemo event
        await db.insert_treatment_event(
            make_treatment_event(
                event_date=date(2026, 2, 13),
                event_type="chemo",
                title="FOLFOX C1",
            ),
            patient_id=ERIKA_UUID,
        )

        # Add lab before chemo date
        await db.insert_document(
            make_doc(
                file_id="pre_chemo_labs",
                document_date=date(2026, 2, 10),
            ),
            patient_id=ERIKA_UUID,
        )

        result = await _check_baseline_labs(db, patient_id=ERIKA_UUID)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_labs_before_date(self, db):
        await db.insert_document(
            make_doc(file_id="lab1", document_date=date(2026, 2, 1)),
            patient_id=ERIKA_UUID,
        )
        await db.insert_document(
            make_doc(file_id="lab2", document_date=date(2026, 2, 15)),
            patient_id=ERIKA_UUID,
        )

        before = await db.get_labs_before_date("2026-02-10", patient_id=ERIKA_UUID)
        assert len(before) == 1
        assert before[0].file_id == "lab1"
