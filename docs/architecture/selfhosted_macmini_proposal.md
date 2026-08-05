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

## Correction: the chat side needs a real MCP server — built fresh, not migrated wholesale

An earlier revision of this doc had the AI-chat side simply connecting Claude's generic Google Drive connector, reading a Register sheet directly. That's wrong for what's actually wanted: the chat needs to query the **register and the AI-extracted metadata as first-class, structured data** — "what's the latest lab result," "summarize everything from NOU this year," with a **clickable link back to the original scan** — not re-derive that from raw file search and hoping Claude parses a spreadsheet correctly each time.

The next revision after that proposed simply redeploying the existing 96-tool oncofiles FastMCP server as-is. Per direction, that's the wrong instinct too — **most of that surface is SaaS baggage this deployment doesn't need**: a bilingual public dashboard, session-cookie auth + revocation, per-route CSP hardening, circuit breakers, cost-tier budgeting across many unrelated patients, admin/caregiver ACLs. None of that exists to serve *a document register* — it exists to serve *a multi-tenant web product with a public dashboard*, which this isn't. Dragging it over would be exactly the overengineering this whole exercise started by questioning.

**What to actually build fresh**: a small, purpose-built MCP server with maybe five tools total, designed around what a person actually asks:

| Tool | Purpose |
|---|---|
| `list_documents(category?, date_from?, date_to?)` | Browse by filter |
| `search_documents(query)` | Free-text search across summaries, tags, extracted text |
| `get_document(id)` | Full record: summary, category, institution, extracted text, **`drive_file_url`** |
| `get_latest(category?)` | Directly answers "what's the latest lab result" — pre-sorted server-side, no guessing |
| `get_timeline(topic?)` | Chronologically ordered documents relevant to a topic |

**What's genuinely worth reusing, not reinventing** — this is real infrastructure, not SaaS baggage, and rebuilding it from scratch would be pure waste:
- The GDrive OAuth + sync + rename-to-standard pipeline, and the AI extraction logic (`enhance.py`'s prompt design) — already AI-first (#365), already handles OCR/vision extraction well.
- The OAuth + Dynamic Client Registration plumbing that lets an MCP client authenticate a user — the mechanism is generic and correct regardless of how many tools sit behind it.
- The filename convention and category taxonomy — good design, independent of backend architecture.

**What's fresh**: the tool surface above, a much smaller per-user data model (one local SQLite table, not a multi-tenant schema built for five unrelated hosted patients), and no dashboard/session/CSP layer at all — MCP is the only interface.

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
│     4. fetch the file's `webViewLink` from the Drive API and the parent
│        folder's link, once, at processing time
│     5. write one row to local SQLite: filename, date, category, institution,
│        AI summary, tags, extracted text, drive_file_url, drive_folder_url
│
├── MCP server (new, purpose-built — five tools, not ninety-six)
│   ├── list_documents / search_documents / get_document / get_latest / get_timeline
│   │   — every response includes drive_file_url, so the answer can link
│   │     straight back to the original scan
│   └── OAuth + DCR (reused — this plumbing is generic, not SaaS-specific) —
│       a user adds it to an AI chat as a connector, same shape as any other
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
| What Peter operates | Nothing (that was the whole point) | A small, purpose-built MCP server (five tools) — new, not the old 96-tool surface |

The only thing given up relative to the fully-serverless version is "literally zero infrastructure for Peter to run." Given a real server is available from September anyway, that trade is clearly worth it — it buys the actual onboarding experience requested, which the serverless version could not deliver.

---

## Reaching the register from Claude, ChatGPT, or Gemini

The onboarding flow above only authorizes the Mac mini backend (via `drive.file` + Picker) to organize the folder. It grants **no access to any AI chat product** — connecting one to the register is a separate, user-initiated step. The confirmation page at the end of onboarding is where the user gets what they need for this: one server address —

```
https://mcp.oncofiles.com/mcp
```

— the same URL for every user; the OAuth handshake in each step below is what identifies *which* person is asking, so nothing per-user needs to be issued or copy-pasted beyond that URL.

Checked live (Aug 2026), not assumed, per product:

### Claude — fully self-serve today
1. claude.ai → **Settings → Connectors → Add custom connector**.
2. Paste the server URL above.
3. Click **Connect** → Google sign-in (the same account used for onboarding) → **Allow**.
4. Done. Ask questions directly — answers come with a link back to the original file.

### ChatGPT — self-serve, but with real friction to plan for
1. Requires **ChatGPT Plus or higher** — Free tier has no connector access at all. This is a real cost gate, not a setting to toggle around.
2. Settings → Connectors → **Advanced settings → Developer mode** (off by default, has to be found).
3. **Create** → paste the same server URL → choose OAuth → sign in → Allow.
4. Rough edges worth telling the user about up front rather than letting them discover: web only (no mobile app support), and the connector may need re-enabling per new chat.

### Gemini — not achievable without friction today, and worth saying so plainly
No consumer path exists. The Gemini web/mobile app has no setting to add a custom MCP server — Google's MCP investment today lives in developer tools (Gemini CLI, the new Antigravity IDE) and in Gemini Enterprise (a Google Cloud Console admin workflow), none of which a non-technical person should be pointed at. **Recommendation: don't build toward Gemini yet.** Ship Claude (and ChatGPT, with its caveats), and revisit Gemini once Google ships an actual consumer connector surface — trying to force it today would mean handing a caregiver a developer CLI, which fails the entire "no friction" goal this design exists for.

### The Drive-link requirement, and why it matters here specifically
Every tool response (`get_document`, `search_documents`, `list_documents`, `get_latest`) includes `drive_file_url` — fetched once from the Drive API at processing time and stored in the register row, not looked up live per question. This is what lets an answer like "your latest prescription was from Dr. Nováková on 2026-07-12" come with a direct, clickable link to the original scan — the actual document, not just the AI's summary of it, is one click away whenever it's needed quickly.

## Practical setup notes for September

- **Reachability**: Cloudflare Tunnel (`cloudflared`) run as a system service (auto-restarts on reboot/crash) gives the Mac mini a stable public HTTPS hostname with no port-forwarding and no public IP exposure — confirmed free-tier-viable for this scale. The ceiling on reliability becomes home internet/power uptime, not Cloudflare's side.
- **OAuth**: extend the existing `oncofiles-490809` GCP project's consent screen with the `drive.file` scope rather than standing up a second OAuth client — reuses the verification already done.
- **Storage**: a local SQLite file for the connection table is sufficient at this scale — no Turso, no replica, no single-connection concurrency concerns, because it's one process on one machine.
- **Scheduling**: launchd (macOS's native scheduler) running the poll/process job every few minutes, no Apps Script-style quota ceiling to design around.

## Open questions for Peter

- How many users, roughly, should this be sized for in September? ("A few" — is that 3, or 10? Changes nothing structurally, but worth pinning for the Anthropic cost estimate.)
- Does the shared Anthropic API key model (Peter pays, like today) match your intent, or do you want per-user billing preserved even in this simpler model?
- Same standing question as before: does this run alongside the existing oncofiles.com product for the other five patients, or does it replace it? Nothing here forecloses either answer.
- ChatGPT access requires the *user* to already pay for Plus or higher — is it worth documenting that as a real prerequisite for that path, or should ChatGPT support be deferred until it's clearer how many users would actually have that?
- Comfortable shipping without Gemini support for now, revisiting once Google ships a consumer-facing connector surface?
