-- #491 E2E test prep: remove peter.fusek@instarea.sk from patients.caregiver_email.
--
-- Context: the v5.15 security sweep (#484) was verified at the MCP layer with
-- my OAuth-bound session, but that session is admin-bypass since my email is
-- in DASHBOARD_ADMIN_EMAILS. To reproduce Michal's non-admin caregiver
-- surface and empirically prove the per-caregiver scoping with Chrome end-to-
-- end, peter.fusek@instarea.sk must become a fresh non-admin non-caregiver
-- identity. This migration strips that email from every patient's
-- caregiver_email column, handling the three positions in a comma-separated list.
--
-- DASHBOARD_ADMIN_EMAILS env var change is handled separately via `railway
-- variables --set DASHBOARD_ALLOWED_EMAILS=peterfusek1980@gmail.com` — that
-- keeps @gmail.com as the single admin.
--
-- Reversibility: follow-up migration 066 (if needed) can re-add the email
-- with an inverse UPDATE — easy, the stripped text is a fixed literal.
--
-- No audit INSERT — per CLAUDE.md memory "Never INSERT patient-specific
-- data in migrations (runs in test DBs)". Migration trace lives in git
-- (commit 20d2161) and Railway deploy logs.

-- Case 1: sole caregiver — set to empty string
UPDATE patients
SET caregiver_email = ''
WHERE LOWER(TRIM(caregiver_email)) = 'peter.fusek@instarea.sk';

-- Case 2: first in comma-separated list
UPDATE patients
SET caregiver_email = SUBSTR(caregiver_email, LENGTH('peter.fusek@instarea.sk,') + 1)
WHERE LOWER(caregiver_email) LIKE 'peter.fusek@instarea.sk,%';

-- Case 3: middle or last in comma-separated list (handles both positions)
UPDATE patients
SET caregiver_email = REPLACE(caregiver_email, ',peter.fusek@instarea.sk', '')
WHERE LOWER(caregiver_email) LIKE '%,peter.fusek@instarea.sk%';
