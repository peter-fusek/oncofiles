# #426 memory leak — nightly verification runbook

**Target event:** 2026-04-21 23:00 UTC nightly pipeline run.
**Fix shipped:** commit `e394431` (Vision OCR reference leaks) in v5.11.0-dev.
**Auto-verifier:** remote trigger `trig_01FBDuwLEdaXyyoynN2zaiaj` fires
2026-04-22 07:05 UTC and posts to #433.

## Pre-flight (2026-04-21 daytime, already done)

Two RSS samples 3 min apart showed peak unchanged and growth rate falling in
inverse-proportion to uptime — mathematical signature of fixed-startup cost
with zero ongoing leak. Daytime is clean. See commit `13f710f` for prod
verification data (174.4 MB stable at 20+ min uptime, growth ratio 0.848
matches expected 0.849 for startup-only).

## Definition of success

The nightly pipeline at 23:00 UTC processes up to `DAILY_AI_DOC_CAP=20` docs
per patient. For a typical batch:
- Old baseline (pre-fix, 2026-04-21 AM): **RSS 203 → 628 MB in 10 min** (~2550 MB/h)
- Target (post-fix): **growth rate ≤ 100 MB/h** averaged across the burst
- Hard failure: RSS crosses `MEMORY_RESTART_THRESHOLD_MB=420` and scheduler
  triggers graceful restart

## Morning-after checks (2026-04-22 ~07:05 UTC)

### 1. Read `trig_01FBDuwLEdaXyyoynN2zaiaj` result on #433

The auto-verifier posts a comment with:
- nightly pipeline start/end timestamps
- docs processed count
- peak RSS during + post burst
- final growth rate since startup

Pass criteria: **growth_rate_mb_per_hour ≤ 100** + peak RSS < 420 MB.

### 2. Spot-check `/health` manually

```bash
curl -s https://oncofiles.com/health | jq '.'
```

If growth rate is high (>200) AND uptime is >60 min, that's a real leak —
not startup amortization. Sample twice ~5 min apart and check if the ratio
matches expected startup-cost math (see pre-flight).

### 3. Grep Railway logs for the burst window

Look for any of these warning signals:

```
WARNING enhance: Vision OCR failed for doc ...   # would mean pdf_doc leak if no close log
CRITICAL Graceful restart: RSS ... exceeds 420 MB
CRITICAL Hard RSS ceiling: ... > 600 MB — forcing restart via health probe
```

A handful of "Vision OCR failed" warnings is acceptable (transient API errors
are expected). The key question is whether the failure path closed the fitz
doc — you can verify by searching for `reclaim_memory` log lines per doc and
confirming they're balanced with the incoming batch size.

### 4. Token-delta check against 2026-04-18 baseline

Anthropic Haiku token usage should be ≤10% of the legacy per-5-min interval
cadence (see memory `v5.10.1-dev` entry for pre-optimizer baseline). Peter
checks Anthropic console.

## Outcomes

### Pass (growth ≤ 100 MB/h)
- Close #426 on the auto-verifier comment thread
- Close #433 (cost optimizer production-verified)
- Update memory `v5.11.0-dev` entry: "nightly verification passed"
- Mark Task #1 complete in the task system

### Partial (100 < growth ≤ 200 MB/h)
- Keep #426 open with observation; the surgical fix helped but isn't
  sufficient for the target
- Schedule v5.12 scoping for multiprocessing OCR worker (plan's fallback)
- Do NOT rollback `AI_NIGHTLY_ONLY` — nightly-only still protects daytime

### Fail (growth > 200 MB/h OR hard ceiling hit)
- Investigate: is the leak in the code I patched (regression) or elsewhere
  (new source)? Compare prompt_log entries pre/post-fix for call counts +
  duration distribution.
- If regression: revert `e394431` via `git revert` → push → deploy
- If new source: look at gmail_sync + calendar_sync (they also run nightly
  now at 23:15 and 23:20 UTC; each could leak independently)
- File critical follow-up if multiprocessing OCR worker becomes P0

## Nightly pipeline components firing between 23:00 and 23:30 UTC

| Time | Job | Expected docs | Notes |
|---|---|---|---|
| 23:00 UTC | `_run_nightly_ai_pipeline` | up to 20/patient × 5 patients = 100 docs max | Vision OCR + enhance + metadata extraction |
| 23:15 UTC | `_run_gmail_sync` | whatever's new | No Vision OCR; Google API calls only |
| 23:20 UTC | `_run_calendar_sync` | whatever's new | Same profile as Gmail |

`_run_nightly_ai_pipeline` is the one the #426 fix protects. The Gmail +
Calendar syncs don't use Vision OCR so they shouldn't hit the patched path,
but if EITHER leaks during their 23:15 / 23:20 run, tonight's data will
attribute it to "nightly burst" even though the root cause is different.

## Contingency: if production flounders overnight

- `HARD_RSS_CEILING_MB=600` in `memory.py` will force a restart via
  `sys.exit(1)` from the `/health` probe. This is the last-line defense.
- Railway's healthcheck re-provisions automatically. Expect 30-60s outage.
- Peter gets UptimeRobot alert within a minute.

## References

- #426 — this issue
- #433 — cost optimizer verification (auto-verifier trigger target)
- commit `e394431` — the fix
- commit `ee62fee` — prior 180s timeout + per-doc reclaim (v5.10.1)
- `src/oncofiles/sync.py:1735` — `_enhance_document` (the patched path)
- `src/oncofiles/memory.py` — RSS tracking + graceful restart logic
