-- MCP OAuth session persistence: survive Railway deploys
-- Stores FastMCP's InMemoryOAuthProvider state in the database

CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_info_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS mcp_oauth_tokens (
    token TEXT PRIMARY KEY,
    token_type TEXT NOT NULL CHECK(token_type IN ('access', 'refresh')),
    client_id TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    expires_at INTEGER,
    linked_token TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_type ON mcp_oauth_tokens(token_type);
CREATE INDEX IF NOT EXISTS idx_mcp_oauth_tokens_client ON mcp_oauth_tokens(client_id);
