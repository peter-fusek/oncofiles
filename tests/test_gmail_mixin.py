"""Tests for Gmail email entry database operations."""

from datetime import datetime

from oncofiles.database import Database
from oncofiles.models import EmailEntry, EmailQuery


def _make_email(**kwargs) -> EmailEntry:
    defaults = {
        "gmail_message_id": "msg_001",
        "thread_id": "thread_001",
        "subject": "Lab results - CBC",
        "sender": "doctor@hospital.sk",
        "recipients": '["patient@gmail.com"]',
        "date": datetime(2026, 3, 15, 10, 0),
        "body_snippet": "Your CBC results are attached.",
        "body_text": "Dear patient, your CBC results are attached.",
        "labels": '["INBOX"]',
        "has_attachments": True,
        "is_medical": True,
    }
    defaults.update(kwargs)
    return EmailEntry(**defaults)


async def test_upsert_and_get(db: Database):
    entry = _make_email()
    saved = await db.upsert_email_entry(entry)
    assert saved.id is not None
    assert saved.subject == "Lab results - CBC"
    assert saved.is_medical is True

    fetched = await db.get_email_entry(saved.id)
    assert fetched is not None
    assert fetched.gmail_message_id == "msg_001"


async def test_get_by_gmail_id(db: Database):
    entry = _make_email()
    await db.upsert_email_entry(entry)
    fetched = await db.get_email_entry_by_gmail_id("msg_001", patient_id="erika")
    assert fetched is not None
    assert fetched.subject == "Lab results - CBC"


async def test_upsert_idempotent(db: Database):
    entry = _make_email()
    first = await db.upsert_email_entry(entry)
    entry.subject = "Updated subject"
    second = await db.upsert_email_entry(entry)
    assert first.id == second.id
    assert second.subject == "Updated subject"


async def test_search_by_text(db: Database):
    await db.upsert_email_entry(_make_email(gmail_message_id="m1", subject="Chemo schedule"))
    await db.upsert_email_entry(_make_email(gmail_message_id="m2", subject="Invoice payment"))
    results = await db.search_email_entries(EmailQuery(text="Chemo"), patient_id="erika")
    assert len(results) == 1
    assert results[0].subject == "Chemo schedule"


async def test_search_by_sender(db: Database):
    await db.upsert_email_entry(_make_email(gmail_message_id="m1", sender="dr@hospital.sk"))
    await db.upsert_email_entry(_make_email(gmail_message_id="m2", sender="shop@amazon.com"))
    results = await db.search_email_entries(EmailQuery(sender="hospital"), patient_id="erika")
    assert len(results) == 1


async def test_search_medical_only(db: Database):
    await db.upsert_email_entry(_make_email(gmail_message_id="m1", is_medical=True))
    await db.upsert_email_entry(_make_email(gmail_message_id="m2", is_medical=False))
    results = await db.search_email_entries(EmailQuery(is_medical=True), patient_id="erika")
    assert len(results) == 1


async def test_list_entries(db: Database):
    for i in range(5):
        await db.upsert_email_entry(_make_email(gmail_message_id=f"m{i}"))
    entries = await db.list_email_entries(limit=3, patient_id="erika")
    assert len(entries) == 3


async def test_count(db: Database):
    assert await db.count_email_entries(patient_id="erika") == 0
    await db.upsert_email_entry(_make_email())
    assert await db.count_email_entries(patient_id="erika") == 1


async def test_get_nonexistent(db: Database):
    assert await db.get_email_entry(999) is None
    assert await db.get_email_entry_by_gmail_id("nonexistent", patient_id="erika") is None
