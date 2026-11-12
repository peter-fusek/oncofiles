"""Settings loaded from environment variables."""

from __future__ import annotations

import importlib.metadata
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
# Cloud AI (Anthropic API) is OFF by default (#618/#699): documents are not sent
# to a cloud model unless the operator opts in explicitly. Having a key is not
# enough — both CLOUD_AI_ENABLED and ANTHROPIC_API_KEY are required.
# Read at call time by cloud_guard.require_cloud_ai().
CLOUD_AI_ENABLED: bool = os.environ.get("CLOUD_AI_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
DATABASE_PATH: Path = Path(os.environ.get("DATABASE_PATH", str(DATA_DIR / "oncofiles.db")))

# Google Drive (v0.2)
GOOGLE_DRIVE_FOLDER_ID: str = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_BASE64: str = os.environ.get("GOOGLE_CREDENTIALS_BASE64", "")
GOOGLE_APPLICATION_CREDENTIALS: str = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")

# Master kill switch for ALL Google API access — Drive, Gmail, Calendar (#552).
# Set false when the credentials behind them are known-dead (deleted GCP
# project, revoked client) so the app stops hammering APIs that cannot answer.
#
# Prefer this over SYNC_ENABLED=false: SYNC_ENABLED kills the entire
# APScheduler, taking memory_check, the cleanup jobs and the
# sync watchdog down with it. This flag disables only the Google-touching
# paths, so the MCP server, dashboard, DB and local housekeeping keep running
# normally and the watchdog can still report the outage.
GOOGLE_INTEGRATION_ENABLED: bool = os.environ.get("GOOGLE_INTEGRATION_ENABLED", "true").lower() in (
    "true",
    "1",
    "yes",
)

# Gmail + Google Calendar are OFF by default (#618/#699). The privacy text says
# they are not used, and this flag is what makes that true — independently of
# SYNC_ENABLED and of GOOGLE_INTEGRATION_ENABLED (which must stay true so the
# curated pipeline can reach Drive). When false: the gmail/calendar scheduler
# jobs are not registered, no OAuth URL ever requests the gmail/calendar
# scopes, and the Gmail/Calendar MCP tools refuse. Runtime-only env; read at
# call time through ``gmail_calendar_enabled()`` so tests can patch it.
GMAIL_CALENDAR_ENABLED: bool = os.environ.get("GMAIL_CALENDAR_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)
GMAIL_CALENDAR_DISABLED_MESSAGE = "Gmail/Calendar integration is not enabled on this server."


def gmail_calendar_enabled() -> bool:
    """Return the current GMAIL_CALENDAR_ENABLED value (module attr, patchable)."""
    return GMAIL_CALENDAR_ENABLED


# Google OAuth 2.0 (v1.0)
# Local LLM (#565): OpenAI-compatible endpoint on the self-hosted server (LM Studio).
# Used by the curated-folder pipeline for OCR (vision) and classification, so
# document content never leaves the server. Runtime-only env; never buildtime.
LOCAL_LLM_BASE_URL: str = os.environ.get("LOCAL_LLM_BASE_URL", "").rstrip("/")
LOCAL_LLM_API_KEY: str = os.environ.get("LOCAL_LLM_API_KEY", "")
LOCAL_LLM_MODEL: str = os.environ.get("LOCAL_LLM_MODEL", "")
LOCAL_LLM_TIMEOUT_S: float = float(os.environ.get("LOCAL_LLM_TIMEOUT_S", "900"))
# Re-OCR self-limit (#667): no new document is started while RSS is above this,
# well under HARD_RSS_CEILING_MB, which makes /health exit the process.
REOCR_RSS_LIMIT_MB: float = float(os.environ.get("REOCR_RSS_LIMIT_MB", "450"))
# Concurrent vision passes per page in a re-read (#593). 1 = sequential (default);
# 2 needs at least 2 parallel slots in LM Studio (infra setting).
REOCR_PARALLEL_PASSES: int = int(os.environ.get("REOCR_PARALLEL_PASSES", "1"))
# Render resolution of the two vision passes (#593), split by path. Defaults are
# the values used since #660: pass 1 at 150 dpi, pass 2 at 220 dpi; every page is
# still capped at extract.MAX_PAGE_PIXELS. The curation pipeline's first OCR keeps
# extract.RENDER_DPI. Runtime env; the re-OCR child inherits REOCR_* and REREAD_*.
# Re-OCR rewrite path (curated_reocr: its transcript is written to Drive). DO NOT
# LOWER without a separate measurement of that path's output quality.
REOCR_DPI: int = int(os.environ.get("REOCR_DPI", "150"))
REOCR_SECOND_PASS_DPI: int = int(os.environ.get("REOCR_SECOND_PASS_DPI", "220"))
# Verify-only re-read (#678: nothing written, it only confirms Cowork-era values).
# The A/B knob for #593 T3.
REREAD_DPI: int = int(os.environ.get("REREAD_DPI", "150"))
REREAD_SECOND_PASS_DPI: int = int(os.environ.get("REREAD_SECOND_PASS_DPI", "220"))
# Second-pass policy (#593): "fixed2" (default) reads every page twice; "adaptive"
# skips the second pass only where it provably cannot change what is confirmed
# (see reocr._vision_passes); verification semantics never loosen.
REOCR_PASSES_MODE: str = os.environ.get("REOCR_PASSES_MODE", "fixed2").strip().lower()
# Model reasoning for re-OCR / re-read transcription (perf): "on" (default, the
# model's own behaviour) or "off" (chat_template_kwargs.enable_thinking=false).
# Transcription only; classification and extraction are not affected.
REOCR_THINKING: str = os.environ.get("REOCR_THINKING", "on").strip().lower()
# Qwen soft switch (#689): LM Studio ignores chat_template_kwargs.enable_thinking
# for the Qwen3.x MLX model, so with thinking=False llm.chat also puts "/no_think"
# into the last user message. "false" sends only the kwarg.
LLM_NO_THINK_SOFT_SWITCH: bool = os.environ.get("LLM_NO_THINK_SOFT_SWITCH", "true").lower() in (
    "true",
    "1",
    "yes",
)
# Context length LM Studio loads the model with (central macmini setting).
# Change it together with LM Studio, or prompts + budgets overflow (#601).
LOCAL_LLM_CONTEXT_WINDOW: int = int(os.environ.get("LOCAL_LLM_CONTEXT_WINDOW", "32768"))
# LLM-first fact extraction (#714), phase 2a: SHADOW mode only. The MCP tool
# curated_llm_extract_preview runs the local-model extractor + the generic
# verifier on stored page text and reports agreement with the stored rule
# facts (keys and counts only). It writes nothing, so it is enabled by
# default; LLM_EXTRACT_SHADOW_DISABLED=true is the off-switch.
LLM_EXTRACT_SHADOW_DISABLED: bool = (
    os.environ.get("LLM_EXTRACT_SHADOW_DISABLED", "false").strip().lower() == "true"
)
# Model reasoning for the text extractor: "off" (default; the page text is
# short and the answer is a copy job) or "on".
LLM_EXTRACT_THINKING: str = os.environ.get("LLM_EXTRACT_THINKING", "off").strip().lower()
# Sampling of both LLM extractors (#767): "greedy" (default, temperature 0,
# the previous behaviour) or "qwen" (the Qwen3 model-card profiles; greedy
# decoding makes Qwen3 models loop, especially with thinking off). Any other
# value is treated as "greedy". Re-OCR and classification are not affected.
LLM_EXTRACT_SAMPLING: str = os.environ.get("LLM_EXTRACT_SAMPLING", "greedy").strip().lower()
# Answer budget (max_tokens) per page with thinking OFF (#767). A long lab
# page (~40 items) needs well under 4000 tokens. Re-derive it from measured
# data: p95 completion_tokens of the preview reports x 1.5. With thinking ON
# the extractors keep llm.REASONING_BUDGET (reasoning counts into max_tokens).
LLM_EXTRACT_ANSWER_BUDGET: int = int(os.environ.get("LLM_EXTRACT_ANSWER_BUDGET", "4000"))
# Per-request timeout of an extractor call (#767), so a runaway generation
# costs minutes, not the 900 s OCR timeout (LOCAL_LLM_TIMEOUT_S, unchanged).
LLM_EXTRACT_TIMEOUT_S: float = float(os.environ.get("LLM_EXTRACT_TIMEOUT_S", "300"))
# Prefilled assistant turn for both LLM extractors (#770): with thinking OFF,
# end the request with an assistant message holding an already-closed empty
# think block ("<think>\n\n</think>\n\n"), because LM Studio / MLX ignores
# enable_thinking=false and /no_think for Qwen3.x on pages with content and
# the model never closes </think>. Default OFF until the dry-run A/B confirms
# it; never used with thinking ON. Re-OCR and classification are not affected.
LLM_EXTRACT_PREFILL: bool = os.environ.get("LLM_EXTRACT_PREFILL", "false").strip().lower() == "true"
# Structured output (json_schema response_format) for the lab shadow
# extractor (#714). Default OFF: with a json_schema grammar Qwen cannot emit
# its empty <think></think> first, so LM Studio files the whole answer under
# reasoning_content, which is never machine-trusted. Without it the answer
# comes back in content and is parsed and validated in Python (as re-OCR, #739).
LLM_EXTRACT_STRUCTURED_OUTPUT: bool = (
    os.environ.get("LLM_EXTRACT_STRUCTURED_OUTPUT", "false").strip().lower() == "true"
)
# Structured output (json_schema response_format) for the clinical extractor
# (#726). Default OFF: with a json_schema grammar Qwen cannot emit its empty
# <think></think> first, so LM Studio files the whole answer under
# reasoning_content, which is never machine-trusted (2026-11-11 prod preview).
# Without it the answer comes back in content and is parsed and validated in
# Python (same as re-OCR, #739).
LLM_CLINICAL_STRUCTURED_OUTPUT: bool = (
    os.environ.get("LLM_CLINICAL_STRUCTURED_OUTPUT", "false").strip().lower() == "true"
)
# LLM-first extraction of the non-lab kinds (#726 item 4): diagnoses, biomarkers,
# treatments, procedures, lesion sizes, planned dates. MCP tools
# curated_clinical_extract_preview (dry run) and curated_clinical_extract
# (dry_run=True by default; writes only with dry_run=false and a known DOB).
# LLM_CLINICAL_EXTRACT_DISABLED=true is the off-switch for both.
LLM_CLINICAL_EXTRACT_DISABLED: bool = (
    os.environ.get("LLM_CLINICAL_EXTRACT_DISABLED", "false").strip().lower() == "true"
)

GOOGLE_OAUTH_CLIENT_ID: str = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET: str = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
GOOGLE_OAUTH_REDIRECT_URI: str = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT_URI", "https://oncofiles.com/oauth/callback"
)

# Cloud transport (v0.4)
MCP_TRANSPORT: str = os.environ.get("MCP_TRANSPORT", "stdio")
MCP_HOST: str = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT: int = int(os.environ.get("MCP_PORT", os.environ.get("PORT", "8000")))
MCP_BEARER_TOKEN: str = os.environ.get("MCP_BEARER_TOKEN", "")

# Fair Use Policy (FUP) — free tier document limit per patient. 0 disables it.
MAX_DOCUMENTS_PER_PATIENT: int = int(os.environ.get("MAX_DOCUMENTS_PER_PATIENT", "500"))

# Query ceiling for "every document for this patient" reads (#548).
#
# Deliberately NOT equal to the FUP cap. The cap governs *future inserts*; it
# does not bound rows that already exist — q1b sits at ~296 documents under a
# cap of 200 today. So if a read limit tracked the cap exactly, LOWERING the cap
# would silently truncate reads over existing data. In `_cleanup_orphan_ocr`
# (removed in #734) that meant trashing OCR sidecars for the documents that fell
# outside the window, because anything absent from `expected_ocr_names` was
# treated as an orphan. `max()` makes the ceiling monotonic: it can never drop below either
# the cap or the historical high-water mark, and raising the cap raises it too.
#
# 10_000 also matches the most generous pre-existing literal in the codebase, so
# adopting DOC_QUERY_LIMIT everywhere never *lowers* an existing limit.
# `LIMIT 0` would return no rows, so the `0 = unlimited` cap can't be reused here
# either. Truncation is no longer silent regardless: `list_documents` logs an
# error when a whole-corpus read fills its ceiling (see database/_documents.py).
DOC_QUERY_LIMIT: int = max(MAX_DOCUMENTS_PER_PATIENT, 10_000)

# Sync scheduler (v1.0)
SYNC_INTERVAL_MINUTES: int = int(os.environ.get("SYNC_INTERVAL_MINUTES", "5"))
SYNC_ENABLED: bool = os.environ.get("SYNC_ENABLED", "true").lower() in ("true", "1", "yes")

# Sync staleness watchdog (#552). If a patient with a connected Drive folder
# has had no *completed* sync in this many hours, the pipeline is broken —
# surfaced on /health + /readiness and emailed to the admin. 36h matches
# AI_REPROCESS_MAX_AGE_HOURS and tolerates exactly one missed nightly run
# (23:00 UTC daily) before alerting, so a single blip stays quiet.
SYNC_STALE_ALERT_HOURS: int = int(os.environ.get("SYNC_STALE_ALERT_HOURS", "36"))

# Nightly-only AI pipeline (v5.10.0, #433)
# When AI_NIGHTLY_ONLY=true (default), LLM-touching jobs collapse into one
# CronTrigger at AI_NIGHT_WINDOW_START_UTC and only process new records
# (ai_processed_at IS NULL AND created_at > now - AI_REPROCESS_MAX_AGE_HOURS).
# Setting AI_NIGHTLY_ONLY=false restores the legacy 5-min interval + 4 nightly
# sweep jobs — rollback knob.
AI_NIGHTLY_ONLY: bool = os.environ.get("AI_NIGHTLY_ONLY", "true").lower() in (
    "true",
    "1",
    "yes",
)
AI_NIGHT_WINDOW_START_UTC: int = int(os.environ.get("AI_NIGHT_WINDOW_START_UTC", "23"))
AI_NIGHT_WINDOW_END_UTC: int = int(os.environ.get("AI_NIGHT_WINDOW_END_UTC", "3"))
DAILY_AI_DOC_CAP: int = int(os.environ.get("DAILY_AI_DOC_CAP", "20"))
AI_REPROCESS_MAX_AGE_HOURS: int = int(os.environ.get("AI_REPROCESS_MAX_AGE_HOURS", "36"))
# Inline AI enhance timeout. 60s strands scanned multi-page PDFs mid-OCR (#422).
# 180s covers typical 2-4 page Vision OCR + Haiku classify/metadata/summary.
# Tune up if larger scanned files hit the wall; tune down if memory pressure
# dominates (longer timeout = more time per-doc for leaked buffers to accumulate).
ENHANCE_TIMEOUT_S: float = float(os.environ.get("ENHANCE_TIMEOUT_S", "180"))
ENABLE_INTEGRITY_CHECK: bool = os.environ.get("ENABLE_INTEGRITY_CHECK", "false").lower() in (
    "true",
    "1",
    "yes",
)
# One-off validation trigger: fire the full nightly AI pipeline ~2 min after boot.
# Use after cost-optimizer changes to validate end-to-end without waiting for the
# next 23:00 UTC slot. Turn off once validated. See #440.
AI_RUN_ON_STARTUP: bool = os.environ.get("AI_RUN_ON_STARTUP", "false").lower() in (
    "true",
    "1",
    "yes",
)

# Gmail + Calendar integration (#104)
GMAIL_ENABLED: bool = os.environ.get("GMAIL_ENABLED", "false").lower() in ("true", "1", "yes")
CALENDAR_ENABLED: bool = os.environ.get("CALENDAR_ENABLED", "false").lower() in (
    "true",
    "1",
    "yes",
)

# GitHub bug reporting
GITHUB_TOKEN: str = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO: str = os.environ.get("GITHUB_REPO", "peter-fusek/oncofiles")
# Dashboard bug reports go ONLY to this repo, and only if GitHub confirms it is
# private (the dashboard shows patient data). No default: unset = disabled (#621).
BUG_REPORT_REPO: str = os.environ.get("BUG_REPORT_REPO", "")

# Dashboard auth — comma-separated admin emails (see all patients)
# Non-admin users can still sign in but only see their own patients.
DASHBOARD_ADMIN_EMAILS: list[str] = [
    e.strip().lower()
    for e in os.environ.get(
        "DASHBOARD_ALLOWED_EMAILS",  # env var kept for backward compat
        "peterfusek1980@gmail.com,peter.fusek@instarea.sk",
    ).split(",")
    if e.strip()
]

# Localization
PREFERRED_LANG: str = os.environ.get("ONCOFILES_PREFERRED_LANG", "sk")

# Logging
LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

# Version (single source of truth from pyproject.toml)
try:
    VERSION: str = importlib.metadata.version("oncofiles")
except importlib.metadata.PackageNotFoundError:
    VERSION = "0.0.0-dev"
