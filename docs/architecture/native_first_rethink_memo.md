# Rethinking Oncofiles: native-connector, no-backend architecture

**Status:** Research memo, analysis only — no code proposed or written. For Peter to decide.
**Date:** 2026-08-05
**Trigger:** Session request to step back and reconsider whether the current custom backend (FastMCP + Turso + Railway + APScheduler) is more machinery than the problem needs, in favor of an "AI-first" design: Claude's native Google Drive/Gmail connectors doing rename/organize/extract, a register stored as a file inside Drive itself instead of a database, scheduled AI agents for upkeep, and zero-backend portability to a second user's own Drive/Gmail account.

---

## Grounding: what the project's own history already says

Before proposing anything, I read the founding BRD and the full GitHub issue history (533 issues, 57 open) rather than working from summary memory alone. Three things there change the framing:

1. **Erika is not a hypothetical — she's patient `q1b`, already live in production.** #404 ("Erika stuck chemo_sheet root cause") and the current sprint's operational follow-ups both refer to "Erika (q1b)" directly. This rethink isn't greenfield design for a new personal tool — it's reconsidering the architecture underneath a real patient's real document history, real filename-convention data, and a real GDrive folder that's already been synced, categorized, and partially AI-processed for months. Any lean rewrite needs a migration story for her existing data, not just a clean-slate design.
2. **Multi-patient was the plan from day one, not scope creep.** The original `docs/product-vision.md` (written at v3.16, closed out via 28 all-shipped `US-*` user stories) explicitly scoped "Phase 2 — Multi-User (v4.x): patient_id on all tables, per-patient GDrive folder isolation, multi-patient Oncoteam agents" as the second roadmap phase, before a single line of Turso/Railway code existed. The heavy multi-patient isolation machinery isn't accidental bloat layered onto a personal tool — it's the originally-planned second phase, executed as planned, then hardened further by real production incidents (cross-patient leaks #429/#476/#497, session bugs #495/#502/#510) that a single-patient tool would never have surfaced.
3. **There's already a closed, full competitive-landscape research issue (#444, 2026-04-21) that bears directly on this question**, and its conclusion cuts against a wholesale native/no-backend rewrite. Its closest identified peer is **Chartfold** (github.com/queelius/chartfold) — a solo cancer patient's MIT-licensed tool: SQLite + MCP tools over EHR exports, **single-patient, no OCR, no GDrive/Gmail ingest, no bilingual support**. #444's own feature-parity matrix scores Oncofiles a 28 against Chartfold's 8, with the gap being exactly the things a "just point Claude's native connector at a Drive folder" design would give up: OCR at scale, multi-patient isolation, bilingual SK/EN, biomarker safety gates, cross-references, lab trend analysis. A lean native-first Oncofiles would structurally converge toward Chartfold's shape, not stay ahead of it. #444 also explicitly recommended the next three priorities as **clinical validation, EU-native positioning, and FHIR ingest** — a backend rewrite doesn't appear in that list at all, and would consume the sprint capacity #444 earmarked for those instead.
4. **An "AI-first" pivot already happened once, at a narrower scope.** #365 ("AI-first document processing — replace all hardcoded heuristics", closed 2026-04-15) replaced six hardcoded keyword-matching systems (institution inference, category inference, duplicate detection, etc.) with AI-powered classification *inside* the existing backend. That's a meaningfully different move than what's being proposed now — eliminating the backend itself in favor of Claude's native connectors — but it means the "let AI do the judgment calls instead of brittle heuristics" instinct isn't new here; it already shipped once, successfully, without discarding the database/server.

(Side note, unrelated to architecture: `docs/product-vision.md` hasn't been touched since v3.16 — still describes 55 tools / 90 docs / v3.16.0 against a current reality of 96+ tools / 1308 tests / v5.19. Worth a refresh regardless of what this memo's recommendation leads to.)

## Decision point 0 — scope, before anything else

This is the fork every other recommendation hangs on, so it's first.

Oncofiles today is **not** a personal Erika-only tool, and — per the grounding above — was never meant to be one past Phase 1. Per current state it already serves 5 real patients in production (q1b/**Erika**, e5g, nora-antalova, mattias-cesnak, Michal Gašparík, Tibor Illovský) plus a sandbox, behind multi-patient isolation, session auth, and a public dashboard — 1308 tests exist largely *because* it's multi-tenant. A "rip out the backend" rethink and "build Erika a lean personal tool" are two different projects with two different answers, and the second one would mean either forking her data out of the shared system or running any experiment against a snapshot/copy of her Drive folder without touching her live production rows:

- **(a) Wholesale replacement** of oncofiles' backend for all existing patients — high blast radius, breaks real users' current setup, throws away ~18 months of hardening (auth, isolation, cost control, dedup, CSP, etc.) for capabilities the research below shows aren't fully there yet natively.
- **(b) A separate, parallel personal tool** for Erika (or for your own family use), independent of the oncofiles product, explicitly not trying to serve Michal/Tibor/q1b's families.
- **(c) A north-star simplification** — let the *architecture philosophy* below inform where oncofiles sheds complexity over time, without a big-bang cutover, and without breaking existing patients.

The rest of this memo assumes you want an honest comparison either way, but the recommendation section is written for **(c)** as the pragmatic default, with **(b)** called out as a cheap way to test the idea for real before deciding on (a) or (c). Flag if you actually meant (a) or (b) specifically — the trade-offs below shift materially.

---

## What "AI-first, no backend" would concretely mean

1. Drive folder per patient (already the shape today) holds the actual files.
2. A **register/manifest file** living in that same Drive folder (Sheet, or a JSON/YAML file) is the system of record — no Turso, no Railway, no SQL migrations. Each row/entry: filename, parsed date, category, institution, one-line AI summary, tags.
3. **Renaming, organizing, OCR/text extraction** happen via Claude's native Google Drive connector + its own PDF/vision reading — no custom Python `rename_to_standard`, no custom OCR pipeline.
4. **Scheduled agent jobs** (Claude Code Routines / scheduled tasks) periodically scan for new files, extract, update the register — replacing APScheduler + the nightly AI pipeline.
5. **Portability**: onboarding a second person means duplicating a Claude Project (instructions + register-format spec) and having them connect *their own* Drive/Gmail via Claude's own OAuth — no shared server, no per-user deploy, no oauth app of your own to run.
6. **Conversational timeline queries** ("what's the latest development") work by having Claude read the register, sort by date, and synthesize — instead of the custom cross-reference/document-group/timeline logic currently in the dashboard and DB layer.

This is a coherent, appealing model — it's also exactly the model Claude Projects + connectors were built to support for individuals. The question is whether the *current* state of Anthropic's tooling (as of Aug 2026, verified live, not from training-data memory) actually delivers each piece.

---

## Reality check per capability (researched live, not from memory — see sources in the research summary)

| Capability needed | Native-first reality today | Verdict |
|---|---|---|
| **Rename files in place** | The full Drive connector (the one with `search_files`/`read_file_content`/`create_file`/`copy_file`) can **create and copy** but — per multiple independent sources — **cannot rename, move, or trash an existing file**. Your entire filename-convention system (`YYYYMMDD_Patient_Institution_Category_Desc.ext`) is built on atomic in-place rename. | **Blocking gap.** Workaround (copy with new name, leave original) breaks the "no duplicates, never lose originals" invariant differently than it's broken today, and doubles Drive storage. |
| **OCR / text extraction from scans & photos** | Works well — Claude's vision reads PDFs/images natively when the connector fetches them. No custom OCR pipeline needed. | **Native wins outright.** This is genuinely simpler than a custom pipeline. |
| **Gmail: read/search/label/draft** | Full read + label + draft support. **No send tool** (by design). | Fine for "monitor / import attachments" use; irrelevant if you don't need to send from the agent. |
| **Unattended scheduled jobs with connector access** | Claude Code "Routines" (cloud, no machine required) is the closest fit, but user bug reports (not yet confirmed fixed) say connector auth can **re-prompt on every scheduled fire**, which would defeat unattended operation entirely for exactly the Drive/Gmail-touching job you need. Desktop scheduled tasks require the machine to stay on. `/loop` dies in 7 days and needs an open session. | **Unverified — the single highest-risk unknown.** Must be live-tested (create one real scheduled Routine touching Drive, let it fire twice unattended) before any architecture depends on it. |
| **Self-hosted MCP alternative to the native connector** | Anthropic's own reference `mcp-server-gdrive` is **archived/unmaintained**, with a known bug: OAuth tokens expire hourly and never auto-refresh — a dealbreaker for background jobs. Community forks exist but are unofficial. Google shipped its own first-party Workspace MCP in dev preview (May 2026) — more promising long-term, but still a preview. | No mature, maintained, official option today. This is not a place to build load-bearing infra yet. |
| **Files-as-database (register in Drive instead of SQL)** | Standard practical mitigation for low-write-volume, single-writer use: **append-only** updates (not in-place mutation) sidestep the classic race-condition failure mode. No real transactional guarantees, but you don't need them at this volume/concurrency. | **Viable**, with the append-only caveat — don't try to make the register support concurrent writers. |
| **Portable "bring your own Drive" multi-tenant pattern** | This is the strongest finding: Claude's connector OAuth already **is** the per-user tenancy boundary. A duplicated Claude Project pointing at a user's own connected Drive is the cleanest possible "transferable to a new user" design — genuinely zero shared infrastructure, because there's no backend to share. | **Native wins outright** — this is the one part of your ask that native tooling is unambiguously better at than a custom multi-tenant backend. |
| **Chronological timeline synthesis ("what's the latest development")** | Nothing found suggests Drive's own search/recency ranking is reliable for this. Converges with everything else above: you need an **explicit dated register that Claude reads and sorts**, not ambient Drive metadata. | Requires the register (item 2 above) regardless of which architecture you pick — this isn't really an either/or. |
| **HIPAA / medical-data framing** | Personal record-keeping by a patient or family member for themselves is generally not "covered entity" activity the way a clinician's use would be, but this is a real legal nuance, not something to wave off. Keep explicit "organizing/summarizing, not medical advice" framing in whatever project instructions you write. | Worth a real gut-check once, not a blocker to prototyping. |

---

## The core tension

The two capabilities the whole current oncofiles backend exists to guarantee — **reliable atomic rename** and **reliable unattended scheduling** — are exactly the two pieces where native tooling is weakest or unverified today. Meanwhile the two things a from-scratch backend is genuinely bad at — **OCR extraction** (native vision just handles it) and **multi-tenant portability** (native OAuth-per-user beats a shared server) — are exactly where native tooling wins outright.

That's not a coincidence: Anthropic's connectors are built for "one person, their own account, occasional interactive use," not "unattended nightly jobs mutating files across many other people's accounts on a schedule." Oncofiles was built for the second thing. That's not overengineering relative to the actual requirement — the current backend's complexity mostly traces back to things Google Drive's own API forces on any correct implementation (atomic rename semantics, 404/permission handling, folder validation, single-connection Turso concurrency limits) plus multi-patient isolation, which is only "overengineering" if requirement **(a)/(b)** above turns out not to need multiple patients.

---

## Recommendation

1. **Resolve Decision Point 0 first** (product-wide vs. personal vs. north-star) — it's a 2-minute conversation that changes everything else, and the grounding above tips it: q1b/Erika is production data, and #444's own competitive analysis already scored "Chartfold-shaped" (single-patient, no OCR, no bilingual, no backend) as the weakest peer in the landscape, not something to converge toward.
2. **Don't cut over the existing backend for q1b/e5g/nora/Mattias/Michal/Tibor.** The rename and scheduling gaps are real and unverified-fixed; a cutover today would trade a working (if heavy) system for one with two known holes in exactly the operations that matter most (data integrity on rename, unattended reliability) — and would also abandon the exact moat #444 identified against HealthEx/ChatGPT Health/Chartfold.
3. **Do prototype the lean model for real, cheaply, out-of-band**, against a copy of Erika's Drive folder (not her live production one), before deciding anything structural:
   - Duplicate a Claude Project pointed at Erika's Drive folder.
   - Write the project instructions to define the register file format and the filename convention (can literally reuse the existing convention from this repo's CLAUDE.md).
   - Manually run one "organize + extract + update register" session, see what the native connector's rename gap actually costs you in practice (how bad is copy-and-leave-original, really, for a personal-scale folder).
   - Separately, create one real scheduled Routine that touches the Drive connector, and let it fire unattended twice, to settle the auth-reprompt question empirically rather than from a bug report.
   - This validates or kills the idea in an afternoon, with zero risk to production.
4. **If the prototype holds up**, the highest-value thing to backport into oncofiles proper isn't "delete the backend" — it's specific simplifications the research surfaces as genuinely lower-complexity wins regardless of which path you pick:
   - Consider whether the register/timeline concept (explicit dated manifest, read-and-sort rather than ambient recency) simplifies any of the current cross-reference/document-group logic.
   - Track Google's first-party Workspace MCP (dev preview, May 2026) as a maintained alternative to hand-rolled Drive API code, once it's out of preview — not the archived Anthropic reference server.
5. **If Decision Point 0 turns out to be (b)** — a separate personal tool, not touching the oncofiles product — this whole plan is low-risk to build directly, since it doesn't touch existing patients at all. That's the fastest path to a real answer.

---

## Open questions for Peter

- Given q1b is Erika and already in production: is the actual pain point something specific about her setup right now (a bug, a friction point in daily use), rather than the architecture in the abstract? If so, naming that concretely might be a smaller, faster fix than a rearchitecture.
- Does #444's competitive framing (EU/oncology/multi-patient/bilingual as the moat, Chartfold as the cautionary "what we'd converge toward") still match your read of the market, seven-plus sprints later? If yes, that's a strong argument for treating this as decision-point (c) — informing simplification, not (a) — replacement.
- Is this about Erika specifically, or a rethink of oncofiles for everyone currently on it?
- If personal-only: is a single Claude Project + native connectors an acceptable long-term home, or do you want something that survives a Claude product change (i.e., is portability *away* from Anthropic's connectors also a goal)?
- How tolerant are you of the copy-instead-of-rename workaround if the native connector's rename gap isn't fixed by the time you'd want to commit?
- Worth spending 30 minutes confirming the Routines connector-auth-reprompt bug status directly (create one, let it fire twice) before this goes further?
