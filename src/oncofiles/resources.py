"""MCP resource definitions for Oncofiles."""

from __future__ import annotations

from datetime import date

from fastmcp import Context

from oncofiles.tools._helpers import _get_db


def register(mcp):
    @mcp.resource("files://catalog", description="Full catalog of stored medical documents")
    async def catalog(ctx: Context) -> str:
        """Return the complete document catalog as a formatted list."""
        db = _get_db(ctx)
        docs = await db.list_documents(limit=200)
        lines = [f"# Document Catalog ({len(docs)} documents)\n"]
        for d in docs:
            date_str = d.document_date.isoformat() if d.document_date else "unknown"
            lines.append(
                f"- **{d.filename}** [{d.category.value}] "
                f"({date_str}, {d.institution or 'unknown'}) "
                f"file_id: `{d.file_id}`"
            )
        return "\n".join(lines)

    @mcp.resource("files://latest-labs", description="Most recent lab result documents")
    async def latest_labs(ctx: Context) -> str:
        """Return the 5 most recent lab result documents."""
        db = _get_db(ctx)
        labs = await db.get_latest_labs(limit=5)
        if not labs:
            return "No lab results found."
        lines = ["# Latest Lab Results\n"]
        for d in labs:
            date_str = d.document_date.isoformat() if d.document_date else "unknown"
            lines.append(
                f"- **{d.filename}** ({date_str}, {d.institution or 'unknown'}) "
                f"file_id: `{d.file_id}`"
            )
        return "\n".join(lines)

    @mcp.resource(
        "files://treatment-timeline",
        description="Chronological timeline of treatment documents and events",
    )
    async def treatment_timeline(ctx: Context) -> str:
        """Return a chronological markdown timeline merging documents and treatment events."""
        db = _get_db(ctx)
        docs = await db.get_treatment_timeline()
        events = await db.get_treatment_events_timeline()

        if not docs and not events:
            return "No treatment documents or events found."

        # Build unified timeline items
        items: list[tuple[str, str]] = []
        for d in docs:
            date_str = d.document_date.isoformat() if d.document_date else "unknown"
            line = (
                f"- [doc/{d.category.value}] **{d.filename}** "
                f"({d.institution or 'unknown'}) file_id: `{d.file_id}`"
            )
            items.append((date_str, line))
        for e in events:
            date_str = e.event_date.isoformat()
            if len(e.notes) > 100:
                notes_preview = f" — {e.notes[:100]}..."
            elif e.notes:
                notes_preview = f" — {e.notes}"
            else:
                notes_preview = ""
            line = f"- [event/{e.event_type}] **{e.title}**{notes_preview}"
            items.append((date_str, line))

        items.sort(key=lambda x: x[0])
        total = len(docs) + len(events)
        lines = [f"# Treatment Timeline ({total} items: {len(docs)} docs, {len(events)} events)\n"]
        current_date = None
        for date_str, line in items:
            if date_str != current_date:
                current_date = date_str
                lines.append(f"\n## {current_date}\n")
            lines.append(line)
        return "\n".join(lines)

    @mcp.resource(
        "files://conversation-archive",
        description="Last 30 days of conversation diary entries",
    )
    async def conversation_archive(ctx: Context) -> str:
        """Return the last 30 days of conversation entries as markdown."""
        from datetime import timedelta

        db = _get_db(ctx)
        since = date.today() - timedelta(days=30)
        entries = await db.get_conversation_timeline(date_from=since, limit=200)
        if not entries:
            return "No conversation entries in the last 30 days."

        lines = [f"# Conversation Archive (last 30 days, {len(entries)} entries)\n"]
        current_date = None
        for e in entries:
            date_str = e.entry_date.isoformat()
            if date_str != current_date:
                current_date = date_str
                lines.append(f"\n## {current_date}\n")
            tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
            lines.append(f"### [{e.entry_type}] {e.title}{tag_str}\n")
            lines.append(e.content)
            lines.append("")
        return "\n".join(lines)

    @mcp.resource(
        "files://activity-timeline",
        description="Last 24 hours of agent tool calls",
    )
    async def activity_timeline(ctx: Context) -> str:
        """Return the last 24 hours of agent activity as markdown."""
        db = _get_db(ctx)
        entries = await db.get_activity_timeline(hours=24)
        if not entries:
            return "No agent activity in the last 24 hours."

        lines = [f"# Activity Timeline (last 24h, {len(entries)} calls)\n"]
        for e in entries:
            ts = e.created_at.strftime("%H:%M:%S") if e.created_at else "?"
            status_icon = "x" if e.status != "ok" else "v"
            duration = f" ({e.duration_ms}ms)" if e.duration_ms else ""
            lines.append(f"- [{ts}] [{status_icon}] {e.agent_id}/{e.tool_name}{duration}")
            if e.input_summary:
                lines.append(f"  in: {e.input_summary[:100]}")
            if e.error_message:
                lines.append(f"  err: {e.error_message[:200]}")
        return "\n".join(lines)
