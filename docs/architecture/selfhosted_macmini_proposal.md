# Self-hosted proposal: Mac mini backend, one-click onboarding

**Status:** Design proposal, analysis only. Supersedes `clean_slate_gdrive_agent_proposal.md`'s per-user Apps Script model — rejected as too much onboarding friction.
**Date:** 2026-08-05
**New input driving this revision:** a Mac mini M4 Pro (14-core CPU, 20-core GPU, 48GB RAM) becomes available in September as Peter's own infrastructure, sized to run this as a real server for a handful of users. That changes the right answer — the Apps Script design existed specifically to avoid needing a server; with a server actually available, its friction (per-user script copies, pasted API keys, an "unverified app" click-through on every login) is no longer a cost worth paying.

---

## The onboarding flow this is designed around

1. User receives a link via email or WhatsApp.
2. Opens it in Chrome, clicks **Sign in with Google**.
3. A Google Drive **folder picker** appears — they pick their existing folder (or the app offers to create one).
4. Confirmation: "Connected — drop a file in and check back in a couple of minutes."
5. Everything after that is automatic.

No API key to paste. No script to copy. No "Advanced → go to this app anyway" click-through. This is the actual product-style flow requested, and it's achievable — with one real constraint, checked live rather than assumed:

**Google's `drive.file` scope (the one built for exactly this "pick a folder via a picker" pattern) is non-sensitive and skips Google's heavy CASA security review.** But if the OAuth client stays in Google Cloud Console's "Testing" status, two things break the flow: every login shows the "Google hasn't verified this app" interstitial, and **background refresh tokens silently expire after 7 days** — fatal for an unattended sync job. Both go away once the OAuth client is in **Production** status with basic verification (privacy policy URL, verified domain, support email) — a one-time setup task, not something any individual user hits.

**This is already mostly done.** The existing oncofiles GCP project is already verified and in Production on `oncofiles.com`. Adding the `drive.file` scope to that already-verified app costs nothing further — no new domain verification, no new review cycle.

---

## Correction: the chat side needs a real MCP server, not the native Drive connector

An earlier revision of this doc had the AI-chat side simply connecting Claude's generic Google Drive connector, reading a Register sheet directly. That's wrong for what's actually wanted: the chat needs to query the **register and the AI-extracted metadata as first-class, structured data** — "what's the latest lab result," "summarize everything from NOU this year" — not re-derive that from raw file search and hoping Claude parses a spreadsheet correctly each time.

That's exactly what a purpose-built MCP server does, and — this is the important realization — **oncofiles already has one, fully built and tested.** `list_documents`, `search_documents`, `get_document` (returning AI summary + structured metadata), `get_journey_timeline` (chronological synthesis) already exist as MCP tools in the current codebase, backed by real structured rows rather than raw file content. The right move isn't to invent a second, thinner MCP server from scratch — it's to **redeploy the existing one**, trimmed to what a handful of self-hosted users need, rather than reinvent working, hardened code.

What changes for the Mac mini deployment, concretely:
- **Turso → local SQLite.** Turso's cloud-replica architecture exists to serve a distributed, always-scaling hosted product. A single always-on Mac mini has no need for a distributed replica — plain SQLite on local disk is simpler and sufficient at this scale.
- **Railway → Mac mini + Cloudflare Tunnel.** Same server code, different host.
- **GDrive OAuth, sync, rename-to-standard, and the AI extraction pipeline (`enhance.py`) carry over unchanged** — this is already AI-first (#365), already handles OCR/vision extraction, already writes structured metadata per document.
- **MCP auth carries over unchanged** — the existing OAuth + Dynamic Client Registration flow already lets a user connect Claude.ai to an oncofiles-shaped MCP server the same way they'd add any other connector: click connect, authenticate, done. No separate bespoke auth scheme needs building.
- **Multi-user isolation carries over unchanged** — the already-shipped Option A `patient_slug` + ACL-gate pattern (#429/#497/#498) is exactly "a few users, each seeing only their own data" — which is what this needs, just at a smaller scale than the current five-patient production system.

---

## Architecture

```
Peter's Mac mini (always on, from September)
├── Onboarding web app (reachable via Cloudflare Tunnel, e.g. connect.oncofiles.com)
│   ├── "Sign in with Google" → OAuth (drive.file scope, Production status)
│   ├── Google Picker widget → user selects their Drive folder
│   └── on success: create the user's row (refresh token, folder id)
│
├── Background service (cron/launchd, runs every few minutes — no quotas, it's a real machine)
│   for each connected user:
│     1. poll their folder via Drive API (list files modified since last check)
│     2. for each new/changed file: fetch → base64 → call Anthropic API directly
│        (same extraction prompt shape as the current system: category, institution,
│        date, summary, suggested filename)
│     3. rename + move the file via Drive API (full authority — a real backend,
│        so there's no rename/move gap the native connector has)
│     4. write one row to local SQLite: filename, date, category, institution,
│        AI summary, tags, extracted text — the same shape the current
│        `documents` table already stores
│
├── MCP server (the existing oncofiles FastMCP server, redeployed here)
│   ├── list_documents / search_documents / get_document / get_journey_timeline
│   │   — reading that same local SQLite index, not raw Drive files
│   └── OAuth + DCR (already built) — a user adds it to Claude.ai as a connector
│       the same way they'd add any other, no bespoke auth to invent
│
└── One shared Anthropic API key, Peter's own billing (same €-per-month shape
    already validated in the current product, just for a smaller user count)

Claude.ai / Claude Code
  — user adds the oncofiles MCP server as a connector (separate step from
    onboarding, see below) and asks questions answered from real structured
    tool calls — not raw file search over a spreadsheet.
```

**Correction to the earlier privacy framing**: this version of the Mac mini does hold more than connection metadata — it holds the extracted register (dates, categories, summaries, tags, and the extracted text used to answer questions), because that's what a real MCP server needs to query quickly and reliably. It does **not** hold the raw source files themselves — those stay only in the user's own Drive — but the AI-derived index of their contents now lives locally. Worth being explicit about, and worth a basic hygiene baseline given it's real medical metadata: disk encryption (FileVault, on by default on modern Macs), and a backup plan for that SQLite file (a home machine has no built-in redundancy the way Railway's Volume + Turso cloud replica did) — a simple nightly copy to Peter's own separate encrypted backup is enough at this scale.

---

## What changed from the Apps Script version, and why each change is a real improvement

| | Apps Script (rejected) | Mac mini backend (this proposal) |
|---|---|---|
| Onboarding | Copy a template, authorize a personal script copy, paste an API key | Click a link, sign in, pick a folder |
| "Unverified app" warning | Every user, every personal copy | None — one already-verified OAuth app |
| Background token lifetime | N/A (script runs under the user's own identity) | Long-lived, because the OAuth client is in Production |
| API key | Each user needs their own | One shared key, Peter's own billing |
| Processing schedule | Batched around 6-min/90-min Apps Script quotas | A real always-on machine — no quota math needed |
| New-file detection | Drive push notifications + expiring-channel renewal | Simple polling every few minutes — the machine is always on, so there's nothing to renew |
| Where source documents live | User's own Drive | Unchanged — user's own Drive, never copied wholesale to the Mac mini |
| Where the register/index lives | A Sheet in the user's own Drive | Local SQLite on the Mac mini, so the MCP server can query it directly |
| What Peter operates | Nothing (that was the whole point) | The existing oncofiles server code, redeployed — not new code, a new host |

The only thing given up relative to the fully-serverless version is "literally zero infrastructure for Peter to run." Given a real server is available from September anyway, that trade is clearly worth it — it buys the actual onboarding experience requested, which the serverless version could not deliver.

---

## Reaching the register from Claude or ChatGPT — two separate connections

The onboarding flow above only authorizes the Mac mini backend (via `drive.file` + Picker) to organize the folder. It grants **no access to Claude or ChatGPT** — connecting an AI chat to the register is a separate, user-initiated step, made inside each AI product's own settings, after onboarding.

- **Claude**: the user adds the oncofiles MCP server as a **custom connector** in their own Claude.ai account (the same OAuth + Dynamic Client Registration flow the current product already supports) — not the generic native Google Drive connector. This is the meaningful difference from the previous revision of this doc: instead of Claude guessing its way through a spreadsheet via raw file search, it gets real tools (`list_documents`, `search_documents`, `get_document`, `get_journey_timeline`) backed by the AI-extracted register directly. Same "click connect, authenticate, done" shape as any other connector, richer result.
- **ChatGPT — checked live (Aug 2026), not assumed**: OpenAI's *generic Drive connector* is paid-plan-only and currently excluded in the EEA/Switzerland/UK, ruling it out for Slovakia-based users regardless. Whether ChatGPT's consumer app supports adding an arbitrary **custom MCP connector** (which would sidestep the Drive-connector restriction entirely, since it wouldn't be Google-specific) is **not yet verified** — OpenAI has been shipping MCP support across its API/Agents SDK, but consumer-app custom-connector support is moving fast enough to need a direct, current check rather than an assumption either way.

**Practical conclusion**: Claude, via the oncofiles MCP connector, is the confirmed "connect once, ask naturally, get structured answers" option today. ChatGPT's viability depends on an unverified fact (custom MCP connector support in the consumer app) — worth checking directly before ruling it in or out.

## Practical setup notes for September

- **Reachability**: Cloudflare Tunnel (`cloudflared`) run as a system service (auto-restarts on reboot/crash) gives the Mac mini a stable public HTTPS hostname with no port-forwarding and no public IP exposure — confirmed free-tier-viable for this scale. The ceiling on reliability becomes home internet/power uptime, not Cloudflare's side.
- **OAuth**: extend the existing `oncofiles-490809` GCP project's consent screen with the `drive.file` scope rather than standing up a second OAuth client — reuses the verification already done.
- **Storage**: a local SQLite file for the connection table is sufficient at this scale — no Turso, no replica, no single-connection concurrency concerns, because it's one process on one machine.
- **Scheduling**: launchd (macOS's native scheduler) running the poll/process job every few minutes, no Apps Script-style quota ceiling to design around.

## Open questions for Peter

- How many users, roughly, should this be sized for in September? ("A few" — is that 3, or 10? Changes nothing structurally, but worth pinning for the Anthropic cost estimate.)
- Does the shared Anthropic API key model (Peter pays, like today) match your intent, or do you want per-user billing preserved even in this simpler model?
- Same standing question as before: does this run alongside the existing oncofiles.com product for the other five patients, or does it replace it? Nothing here forecloses either answer.
