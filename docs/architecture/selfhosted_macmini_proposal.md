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

## Architecture

```
Peter's Mac mini (always on, from September)
├── Tiny onboarding web app (reachable via Cloudflare Tunnel, e.g. connect.oncofiles.com)
│   ├── "Sign in with Google" → OAuth (drive.file scope, Production status)
│   ├── Google Picker widget → user selects their Drive folder
│   └── on success: store {user, refresh_token, folder_id} in one local SQLite table
│
├── Background service (cron/launchd, runs every few minutes — no quotas, it's a real machine)
│   for each connected user:
│     1. poll their folder via Drive API (list files modified since last check)
│     2. for each new/changed file: fetch → base64 → call Anthropic API directly
│        (same extraction prompt shape as the current system: category, institution,
│        date, summary, suggested filename)
│     3. rename + move the file via Drive API (full authority — this is a real
│        backend, not the native connector, so there's no rename/move gap at all)
│     4. write/update one row in THAT USER'S OWN Register Google Sheet
│
└── One shared Anthropic API key, Peter's own billing (same €-per-month shape
    already validated in the current product, just for a smaller user count)

Claude.ai / Claude Code (unchanged)
  — user connects their own Google Drive, reads their own folder + register,
    answers "what's the latest development" by sorting the register by date.
```

What the Mac mini actually stores: OAuth tokens and folder IDs, in one small local table. **Not** the medical documents or their extracted content — those stay in each user's own Drive, in their own Register sheet, exactly as before. The Mac mini is a processing relay, not a database of anyone's medical records. That privacy framing survives this revision unchanged.

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
| Where documents live | User's own Drive + Sheet | Unchanged — user's own Drive + Sheet |
| What Peter operates | Nothing (that was the whole point) | A small local SQLite table of connections, on hardware he already owns |

The only thing given up relative to the fully-serverless version is "literally zero infrastructure for Peter to run." Given a real server is available from September anyway, that trade is clearly worth it — it buys the actual onboarding experience requested, which the serverless version could not deliver.

---

## Reaching the folder from Claude or ChatGPT — two separate connections

The onboarding flow above only authorizes the Mac mini backend (via `drive.file` + Picker) to organize the folder. It grants **no access to Claude or ChatGPT** — those are separate, user-initiated connections made inside each AI product's own settings, after onboarding, using the same Google account.

- **Claude**: Settings → Connectors → Google Drive, a one-time OAuth grant in the user's own Claude account. This is broad read access across their whole Drive, not scoped to a single folder — but since the backend keeps everything consistently named/dated, the user just asks in plain language and Claude's own search tools find the right folder by content, without needing to point at it explicitly.
- **ChatGPT — a real gap for these users, checked live (Aug 2026), not assumed**: a comparable Drive connector exists, but (a) requires a paid plan (Free tier has none at all), and (b) **is currently excluded in the EEA, Switzerland, and the UK** — meaning it's not usable at all for Slovakia-based users today, independent of any setup step. Even where available, its retrieval path reads a file's existing text layer rather than doing image-level OCR, making it unreliable for scanned PDFs and phone photos specifically — the dominant content type here.

**Practical conclusion**: Claude is the only "connect once, then just ask naturally" option for this project's actual users today. ChatGPT remains usable only via manually attaching individual files per conversation, and even then is weaker on scans than direct upload. Worth revisiting if the user base ever extends outside the EU/UK.

## Practical setup notes for September

- **Reachability**: Cloudflare Tunnel (`cloudflared`) run as a system service (auto-restarts on reboot/crash) gives the Mac mini a stable public HTTPS hostname with no port-forwarding and no public IP exposure — confirmed free-tier-viable for this scale. The ceiling on reliability becomes home internet/power uptime, not Cloudflare's side.
- **OAuth**: extend the existing `oncofiles-490809` GCP project's consent screen with the `drive.file` scope rather than standing up a second OAuth client — reuses the verification already done.
- **Storage**: a local SQLite file for the connection table is sufficient at this scale — no Turso, no replica, no single-connection concurrency concerns, because it's one process on one machine.
- **Scheduling**: launchd (macOS's native scheduler) running the poll/process job every few minutes, no Apps Script-style quota ceiling to design around.

## Open questions for Peter

- How many users, roughly, should this be sized for in September? ("A few" — is that 3, or 10? Changes nothing structurally, but worth pinning for the Anthropic cost estimate.)
- Does the shared Anthropic API key model (Peter pays, like today) match your intent, or do you want per-user billing preserved even in this simpler model?
- Same standing question as before: does this run alongside the existing oncofiles.com product for the other five patients, or does it replace it? Nothing here forecloses either answer.
