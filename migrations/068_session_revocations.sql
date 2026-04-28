-- Migration 068: server-side dashboard session revocation (#510).
--
-- Pre-#510 the dashboard logout() flow only cleared client-side
-- sessionStorage. The HMAC-signed session token remained valid for its
-- full 24h lifetime — anyone holding a captured token (proxy log, browser
-- history dump, malicious extension) could continue making authenticated
-- requests until natural expiry.
--
-- Fix: each session token now carries a random `tid` (16 hex chars) in its
-- payload. POST /api/logout inserts that tid into this table; the
-- _verify_session_token path rejects any tid present here.
--
-- TTL: rows older than `expires_at` (= original session expiry) are stale
-- — once the natural HMAC-expiry rejection kicks in, the revocation row
-- is no longer needed. A background sweep purges rows past expires_at.
--
-- Indexed on expires_at so the sweep is cheap; tid is the natural primary
-- key (random + unique per issued session). Safe for Turso: plain CREATE.

CREATE TABLE IF NOT EXISTS session_revocations (
    tid          TEXT    PRIMARY KEY,
    revoked_at   INTEGER NOT NULL,
    expires_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_session_revocations_expires
    ON session_revocations(expires_at);
