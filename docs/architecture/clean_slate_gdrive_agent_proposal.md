# Clean-slate proposal: Drive-native document agent

**Status:** Design proposal, analysis only — no code written, nothing deployed. Supersedes the hedged framing in `native_first_rethink_memo.md` (which only compared "keep the Python backend" vs. "use Claude's native connector as-is"). This document proposes a third option that wasn't on the table there, and recommends it outright.
**Date:** 2026-08-05
**Premise, per direction:** clean slate — no migration constraints, Erika (re-)sets herself up fresh in the new system. This is a from-scratch design for the best available answer today, not a compromise anchored to the existing oncofiles backend.

---

## TL;DR

Neither of the two options in the prior memo was actually the best answer:

- **Keep the custom Python/Turso/Railway backend** — works, but is real infrastructure to run and pay for, for a problem that doesn't need a server.
- **Claude's native Drive/Gmail connector alone** — can't rename/move/trash files, and unattended scheduling (Routines) has an unresolved reliability question. Both are load-bearing gaps for this use case.

**A third option resolves both gaps using Google's own infrastructure, not a workaround:** a Google Apps Script bound to the user's own Drive/Sheet, doing the rename/organize/extract work by calling the Anthropic API directly, triggered by Drive's own push-notification events (with a time-driven fallback) — with Claude.ai/Claude Code used purely as the read-side conversational front end against the same folder and register. Verified live (Aug 2026), not assumed:

| Requirement | Apps Script verdict |
|---|---|
| Rename/move/trash files in place | **Native, first-class** (`DriveApp.setName/moveTo/setTrashed`) — the exact thing the connector can't do |
| Unattended scheduling | **Native, first-class** (Apps Script time-driven triggers are Google's own mature cron — no Claude auth involved at all) |
| React promptly to new uploads | **Native** — Drive `files.watch` push notifications, receivable by the Apps Script's own web-app URL (no server to host) |
| Register/index as a file, not a DB | A **Google Sheet** — human-readable, human-editable, sortable/filterable with zero custom UI |
| OCR / text extraction | Anthropic API (Files API + vision), called directly via `UrlFetchApp` — no custom OCR pipeline |
| Zero shared backend | Confirmed — everything runs inside the user's own Google account on Google's infrastructure, for free within generous quotas |
| Portability to a new user | **"Make a copy"** of the template Sheet+script in Google Drive, authorize under their own account, paste their own Anthropic API key |

This is not a smaller version of the same idea — it's a materially different (and better-fitting) architecture than either option previously considered.

---

## Architecture

```
Erika's Google Drive folder (the only place data ever lives)
├── Register.gsheet  ← the manifest/index; also hosts the bound Apps Script
├── Labs/
├── Reports/
├── Pathology/
├── ... (existing category folders, convention unchanged)
└── (any new file dropped anywhere under the root)
        │
        │  Drive push notification (file added/changed)
        ▼
Apps Script (bound to Register.gsheet, runs under Erika's own Google identity)
├── doPost(e)              — webhook receiver for Drive watch events
├── reprocessTrigger()     — time-driven backstop (catches missed pushes, re-subscribes watch channel before expiry)
├── processFile(fileId)    — the actual pipeline:
│     1. fetch file blob, base64-encode
│     2. call Anthropic Messages API (UrlFetchApp) — vision + structured extraction prompt
│     3. get back { category, institution, date, summary, tags, suggested_filename }
│     4. DriveApp: rename in place, move to category folder
│     5. write/update one row in the Register sheet
└── Script Properties: ANTHROPIC_API_KEY (Erika's own key, encrypted at rest by Google)
        │
        ▼
Claude.ai / Claude Code (native Google Drive connector, read-only against this same folder)
  — "what's the latest development?" → read Register sheet, sort by date, pull the top N
    matching documents, synthesize a chronological answer. No write access needed here;
    all writes happen inside the Apps Script.
```

Nothing above requires a server you operate, a database you run, or an OAuth app registered under your own identity. The entire system lives inside Erika's own Google account.

---

## Register schema (the Sheet, replacing the SQL database entirely)

One row per document:

| filename | drive_file_id | date | category | institution | summary | tags | status | last_processed |
|---|---|---|---|---|---|---|---|---|
| `20260315_Erika_NOU_Labs_CBC.pdf` | `1AbC...` | 2026-03-15 | Labs | NOU | "CBC panel, ANC 1.8, within range" | chemo-cycle-4 | processed | 2026-03-15T09:14:00Z |

- **Filename convention**: reuse the existing one verbatim (`YYYYMMDD_Patient_Institution_Category_DescriptionEN.ext`) — it's a good convention, independent of what runs underneath it.
- **Category taxonomy**: reuse the existing list (Labs, Report, Pathology, CT, USG, Genetics, Surgery, ChemoSheet, etc.) — same reasoning.
- **Timeline queries** work by having Claude read this sheet, sort by `date` descending, and reason over the `summary`/`tags` columns — this is the same conclusion the earlier research reached (don't trust Drive's own recency signal; maintain an explicit dated index), just now backed by a register that's actually reliably kept up to date because the same Apps Script that writes it also owns renaming.

---

## Why this beats both previously-considered options, concretely

**Vs. the current Python/Turso/Railway backend:**
- No server to host, no database to operate, no Railway bill, no migration files, no APScheduler edge cases, no Turso single-connection concurrency constraint, no circuit breakers, no session/auth layer to secure — because there's no shared service exposed to the internet at all. Every one of those exists in the current system to make a *hosted multi-tenant* service safe; a system where each user runs their own private copy inside their own Google account doesn't have that problem class to begin with.
- Cost model changes from "you operate infrastructure and absorb API cost, tiered against a monthly budget" to "Erika's own Anthropic API key, her own bill, her own dashboard." Rough estimate: Haiku-class vision+extraction on a typical scanned medical document runs low-single-digit cents; a few hundred documents' one-time backfill is plausibly single-digit dollars total, and ongoing incremental cost (a handful of new documents a month) is near-zero.
- **Genuine privacy upside worth naming**: the data never leaves Erika's own Google account except for the per-call Anthropic API request. There is no third-party database holding her medical records at all. That's a stronger trust story than "upload your files to our hosted service" — worth stating carefully rather than overclaiming (check Anthropic's current API data-retention terms for the relevant tier before using this in any external-facing pitch).

**Vs. Claude's native connector alone:**
- Solves the rename/move/trash gap outright — Apps Script's `DriveApp` has full authority the connector doesn't expose.
- Solves the scheduling-reliability question outright — Apps Script triggers don't involve Claude's connector auth at all, so the "re-prompts every fire" risk found in the earlier research simply doesn't apply here. The only auth Apps Script needs is the one-time Google authorization Erika grants when she first runs the script under her own identity — standard, mature, not experimental.
- Claude's native connector is still used, but only for its strongest capability (reading/searching/synthesizing across files during a chat), not its weakest (unattended file mutation).

---

## Setup flow — what "Erika sets herself up" actually looks like

1. Peter builds one template: a Google Sheet with the bound Apps Script pipeline code, plus a small setup script that creates the category subfolders.
2. Erika (or Peter, once, on her behalf) makes a copy of the template Sheet into her own Drive — Google's "Make a copy" duplicates the bound script along with it.
3. On first run, Google prompts her to authorize the script's requested scopes (Drive, external URL fetch) under her own account — a standard one-time consent screen, not a custom OAuth app.
4. She pastes her own Anthropic API key into a setup cell / Script Properties dialog.
5. She points the script at her existing Drive folder (or lets the setup script create a fresh one).
6. Done. The script starts watching that folder; dropping a new scan or photo into it triggers the pipeline automatically.
7. To onboard a second person later, repeat steps 2–6 with their own account and their own key — no code changes, no shared infrastructure, no involvement from Peter beyond handing them the template.

This is the literal answer to "trivially transferable to another user with their own GDrive/Gmail account."

---

## What this deliberately leaves out (relative to current oncofiles)

Being honest about the trade — this is a **narrower** tool than the current product, not a strict superset:

- No lab-trend analysis, biomarker safety gates, clinical-trial search, or cross-reference graph — those are "Oncoteam"-layer intelligence, not "Oncofiles"-layer storage/retrieval, and are out of scope for this rebuild unless deliberately added back later as a thin layer reading the same Register sheet.
- No bilingual dashboard, no multi-patient hosted product, no admin/caregiver access-control layer — this is single-user-per-copy by construction, not a SaaS. If oncofiles-the-product (serving q1b/e5g/nora/Mattias/Michal/Tibor as a hosted service) is meant to continue, that's a separate decision from whether Erika personally moves to this model — see the open questions below.
- No formal audit log or per-call cost telemetry out of the box — cheap to add (a second Sheet tab, appended to on every Apps Script call) if wanted, but not built by default.

---

## Known limitations to design around, not ignore

- **Consumer (plain gmail.com) Apps Script quota**: 90 minutes of total trigger runtime per day, 6 minutes per single execution. Fine for incremental "process what's new" runs; a large one-time backfill (hundreds of existing documents) needs to process in small batches with a persisted cursor, spread across a few days — or run under a Google Workspace account (6 hours/day) if backfill speed matters. Design the processing function to be idempotent and resumable from the start, not bolted on later.
- **Drive watch channels expire** (max 1 day for file-level watch, max 1 week for the coarser change-feed watch) and Google does not auto-renew them — the time-driven backstop trigger must both catch missed events and re-subscribe before expiry. This is a known, well-documented pattern, not a surprise risk, but it's a "gotcha" worth writing into the setup doc explicitly (the same way this repo's CLAUDE.md documents its own infra gotchas today).
- **Single-writer only** — fine, since exactly one Apps Script instance owns writes to the Register; Claude sessions only read it. Don't extend this design to multiple concurrent writers without adding real locking.
- **Large multi-page scans** may need chunking against the Anthropic API's page/size limits (600 pages/request, 100 if using a <1M-token-context model) — realistically irrelevant for typical single-visit medical documents, but worth a size check before sending.

---

## Recommended next step

Prototype this for real before committing further: build the template Sheet + Apps Script against a fresh test folder (not Erika's live one), run it through a real upload → rename → register-update → Claude-chat-query cycle, and confirm the Drive watch + re-subscribe mechanics behave as documented over a few days of actual unattended operation. That validates the one part of this design that's genuinely novel (event-driven Apps Script automation of this specific shape) before Erika migrates her real folder onto it.

## Open questions for Peter

- Does this replace oncofiles-the-hosted-product for the other five patients too, or is Erika moving to a personal instance of a new, separate tool while oncofiles.com keeps serving q1b(old)/e5g/nora/Mattias/Michal/Tibor as-is? (Note: if Erika fully migrates off the hosted product, q1b's production row/history in the current system becomes historical-only — worth deciding what happens to it, e.g. leave it in place read-only vs. explicitly retire it.)
- Do you want any of the Oncoteam-style intelligence (lab trends, biomarker gates) rebuilt on top of the Register sheet, or is this rebuild scoped to storage/organize/retrieve only for now?
- Should I proceed to build the template (Sheet + Apps Script) against a test folder, so it's ready for Erika to adopt once you've reviewed this?
