"""Create conversation entries linking advocate notes to existing medical docs.

Usage:
    uv run python scripts/create_conversation_links.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oncofiles.config import TURSO_AUTH_TOKEN, TURSO_DATABASE_URL  # noqa: E402
from oncofiles.database import Database  # noqa: E402
from oncofiles.models import ConversationEntry  # noqa: E402

logger = logging.getLogger(__name__)

# Conversation entries linking advocate notes to existing docs
# Format: (date, type, title, content, tags, doc_ids)
ENTRIES: list[tuple[str, str, str, str, list[str], list[int]]] = [
    (
        "2025-01-22",
        "note",
        "Advocate: Bory discharge review",
        "Patient advocate review of Bory hospital discharge summary. "
        "Reference to discharge documentation from Bory Nemocnica.",
        ["advocate", "bory", "discharge"],
        [46, 28, 29],
    ),
    (
        "2025-01-27",
        "note",
        "Advocate: Post-op summary analysis",
        "Detailed patient advocate analysis of post-operative status. "
        "Covers staging (T4a N2 M1), pathology findings, lymph node involvement, "
        "and initial treatment plan considerations after sigmoid colon resection.",
        ["advocate", "post-op", "staging", "pathology"],
        [47, 25, 27, 30],
    ),
    (
        "2025-01-28",
        "question",
        "Advocate: Questions for Dr. Porsok (NOU)",
        "Pre-visit preparation with clinical questions for oncologist Dr. Porsok at NOU. "
        "Covers treatment options, biomarker implications, and prognosis discussion points.",
        ["advocate", "NOU", "porsok", "questions"],
        [48, 22],
    ),
    (
        "2025-01-28",
        "question",
        "Advocate: Follow-up questions for Bory",
        "22 clinical follow-up questions prepared for Bory hospital team. "
        "Covers post-surgical care, complications, and recovery milestones.",
        ["advocate", "bory", "questions", "follow-up"],
        [49],
    ),
    (
        "2025-02-06",
        "decision",
        "Advocate: FOLFOX vs FOLFIRI analysis",
        "Patient advocate analysis comparing FOLFOX and FOLFIRI chemotherapy regimens. "
        "Treatment decision documentation with rationale for selected protocol.",
        ["advocate", "FOLFOX", "FOLFIRI", "treatment-decision"],
        [50],
    ),
    (
        "2026-02-03",
        "decision",
        "Advocate: Treatment plan with Dr. Porsok",
        "Comprehensive treatment plan analysis covering biomarker profile, "
        "available options, and clinical trial landscape for mCRC.",
        ["advocate", "treatment-plan", "porsok", "biomarkers"],
        [51, 18, 19],
    ),
    (
        "2026-02-06",
        "note",
        "Advocate: Chemo admission + bio-therapy discussion",
        "Notes from chemotherapy admission. Discussion of biological therapy options "
        "and treatment strategy adjustments.",
        ["advocate", "chemo", "admission", "bio-therapy"],
        [52, 3, 4],
    ),
    (
        "2026-02-12",
        "note",
        "Advocate: Comprehensive disease summary",
        "Full disease summary with TNM classification, staging, treatment history, "
        "and current status. Key reference document for clinical context.",
        ["advocate", "disease-summary", "TNM", "staging"],
        [53, 61, 5, 6, 13],
    ),
    (
        "2026-02-13",
        "note",
        "Advocate: Clinical trial landscape for mCRC MSS",
        "Analysis of available clinical trials for metastatic CRC with MSS/pMMR status. "
        "Covers eligibility criteria and trial locations relevant to patient.",
        ["advocate", "clinical-trials", "mCRC", "MSS"],
        [54, 10, 11],
    ),
    (
        "2026-02-27",
        "note",
        "Advocate: Chemo admission notes",
        "Brief admission notes: [MEDICATION_REDACTED] 6000 dosing, NGS mutation discussion "
        "(POLE/POLD1, RET fusions, NTRK fusions), bevacizumab consideration.",
        ["advocate", "chemo", "admission", "NGS"],
        [58, 1, 2],
    ),
    (
        "2026-03-02",
        "note",
        "Advocate: Post-2nd chemo assessment",
        "Post-chemotherapy assessment after second cycle. Lab comparison table, "
        "tumor marker trends, and treatment response evaluation.",
        ["advocate", "chemo", "cycle-2", "assessment"],
        [59, 1],
    ),
    (
        "2026-03-13",
        "note",
        "Advocate: Pre-3rd chemo preparation",
        "Pre-chemotherapy preparation notes for third cycle. Lab trend analysis, "
        "SII calculation, questions for oncologist, bevacizumab risk assessment.",
        ["advocate", "chemo", "cycle-3", "preparation", "SII"],
        [60],
    ),
]


async def create_links(dry_run: bool = False) -> None:
    db = Database(":memory:", turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    await db.connect()

    created = 0
    for entry_date, entry_type, title, content, tags, doc_ids in ENTRIES:
        if dry_run:
            logger.info(
                "DRY RUN: %s [%s] %s (docs: %s)",
                entry_date,
                entry_type,
                title,
                doc_ids,
            )
            continue

        entry = ConversationEntry(
            entry_date=date.fromisoformat(entry_date),
            entry_type=entry_type,
            title=title,
            content=content,
            participant="claude-code",
            tags=tags,
            document_ids=doc_ids,
            source="import",
            source_ref="scripts/create_conversation_links.py",
        )
        entry = await db.insert_conversation_entry(entry)
        logger.info("Created entry %d: %s", entry.id, title)
        created += 1

    await db.close()
    logger.info("Done: %d conversation entries created", created)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(create_links(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
