"""Import Claude Code JSONL session transcripts into conversation archive (#37).

Parses each JSONL file, extracts user prompts + assistant responses,
and creates a summary conversation entry per session.

Usage:
    uv run python -m oncofiles.import_transcripts --dry-run
    uv run python -m oncofiles.import_transcripts
    uv run python -m oncofiles.import_transcripts --path /custom/path
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
from datetime import date, datetime
from pathlib import Path

from oncofiles.config import (
    DATABASE_PATH,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from oncofiles.database import Database
from oncofiles.models import ConversationEntry

# Ensure all output is flushed immediately for progress visibility
print = functools.partial(print, flush=True)  # noqa: A001

DEFAULT_TRANSCRIPTS_PATH = (
    Path.home() / ".claude" / "projects" / "-Users-peterfusek1980gmail-com-Projects-Erika"
)


def _parse_jsonl(path: Path) -> list[dict]:
    """Parse a JSONL file into a list of message dicts."""
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return messages


def _extract_text(content: str | list | dict) -> str:
    """Extract plain text from Claude Code message content (string or content blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _extract_session_summary(messages: list[dict], filename: str) -> dict | None:
    """Extract a summary from a Claude Code JSONL session transcript.

    Claude Code JSONL format: each line is a dict with top-level `type` field
    ("user" or "assistant") and nested `message.role` + `message.content`.

    Returns dict with: title, content, entry_date, or None if empty.
    """
    user_messages = []
    assistant_messages = []

    for record in messages:
        record_type = record.get("type")
        inner_msg = record.get("message", {})
        role = inner_msg.get("role") or record_type
        raw_content = inner_msg.get("content", "")
        text = _extract_text(raw_content)

        if not text.strip():
            continue

        if role == "user":
            user_messages.append(text)
        elif role == "assistant":
            assistant_messages.append(text)

    if not user_messages and not assistant_messages:
        return None

    # Title from first substantial user message (skip interruptions/short noise)
    title = f"Session: {filename}"
    for msg in user_messages:
        line = msg.strip().split("\n")[0][:120].strip()
        if len(line) > 10 and not line.startswith("[Request interrupted"):
            title = line
            break

    # Build markdown content
    content_parts = []
    content_parts.append(f"**Session file:** `{filename}`\n")
    content_parts.append(f"**User messages:** {len(user_messages)}")
    content_parts.append(f"**Assistant messages:** {len(assistant_messages)}\n")

    # Include first few user prompts as highlights
    content_parts.append("## Key Topics\n")
    for i, msg in enumerate(user_messages[:5]):
        snippet = msg[:300].replace("\n", " ").strip()
        content_parts.append(f"{i + 1}. {snippet}")
        if len(msg) > 300:
            content_parts[-1] += "..."

    content = "\n".join(content_parts)

    # Try to extract date from filename (UUID-based names don't have dates)
    # Fall back to file modification time
    entry_date = date.today()

    return {
        "title": title,
        "content": content,
        "entry_date": entry_date,
    }


async def import_transcripts(
    dry_run: bool = False,
    path: Path | None = None,
) -> dict[str, int]:
    """Import JSONL session transcripts into conversation_entries.

    Returns summary dict with counts.
    """
    transcripts_dir = path or DEFAULT_TRANSCRIPTS_PATH

    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()

    jsonl_files = sorted(transcripts_dir.glob("*.jsonl"))
    print(f"Found {len(jsonl_files)} JSONL files in {transcripts_dir}\n")

    stats = {"total": len(jsonl_files), "imported": 0, "skipped": 0, "errors": 0}

    for jsonl_path in jsonl_files:
        source_ref = jsonl_path.name

        # Idempotency check
        existing = await db.get_entry_by_source_ref(source_ref)
        if existing:
            print(f"  SKIP  {source_ref} (already imported as entry #{existing.id})")
            stats["skipped"] += 1
            continue

        try:
            messages = _parse_jsonl(jsonl_path)
            summary = _extract_session_summary(messages, source_ref)

            if not summary:
                print(f"  SKIP  {source_ref} (empty or unparseable)")
                stats["skipped"] += 1
                continue

            # Use file mtime for entry_date
            mtime = jsonl_path.stat().st_mtime
            entry_date = datetime.fromtimestamp(mtime).date()

            if dry_run:
                print(f'  WOULD {source_ref} → "{summary["title"][:60]}" ({entry_date})')
                stats["imported"] += 1
                continue

            entry = ConversationEntry(
                entry_date=entry_date,
                entry_type="summary",
                title=summary["title"],
                content=summary["content"],
                participant="claude-code",
                tags=["import", "transcript"],
                source="import",
                source_ref=source_ref,
            )
            entry = await db.insert_conversation_entry(entry)
            print(f'  OK    {source_ref} → entry #{entry.id}: "{entry.title[:60]}"')
            stats["imported"] += 1

        except Exception as e:
            print(f"  ERROR {source_ref} — {e}")
            stats["errors"] += 1

    await db.close()

    print(f"\n{'Dry run — no changes made.' if dry_run else 'Import complete.'}")
    print(f"  Total:    {stats['total']}")
    print(f"  Imported: {stats['imported']}")
    print(f"  Skipped:  {stats['skipped']}")
    print(f"  Errors:   {stats['errors']}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import JSONL transcripts into conversation archive"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without importing")
    parser.add_argument("--path", type=Path, help="Path to transcripts directory")
    args = parser.parse_args()
    asyncio.run(import_transcripts(dry_run=args.dry_run, path=args.path))


if __name__ == "__main__":
    main()
