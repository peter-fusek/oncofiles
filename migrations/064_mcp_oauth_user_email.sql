-- Migration 064: bind Google account email to each MCP OAuth token (#478).
--
-- Context: post-#478 the MCP OAuth auth path returned the no-access sentinel
-- in multi-patient deployments because the token had no caller identity —
-- FastMCP's InMemoryOAuthProvider issues tokens without knowing who the
-- end-user is. That closed the cross-patient leak Michal Gašparík reported
-- 2026-04-24 but also broke the default-patient UX for legit users.
--
-- Proper fix: capture the Google account email from the user's dashboard
-- session cookie at /authorize time, stash (code_challenge → email), then
-- persist it on the issued access + refresh token rows here. verify_token
-- then resolves patient via patients.caregiver_email match. Existing tokens
-- (NULL email) keep the sentinel behavior — no breaking change.
--
-- DDL-only. Zero-row UPDATE. Safe per the post-#476 "no bulk migrations"
-- rule. Existing tokens default to NULL and users re-auth naturally on
-- claude.ai's next token-refresh cycle.

ALTER TABLE mcp_oauth_tokens ADD COLUMN user_email TEXT;
CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_user_email
    ON mcp_oauth_tokens(user_email);
