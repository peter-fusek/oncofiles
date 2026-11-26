-- #856: which Google account granted the stored token, so a Drive disconnect
-- or an erasure revokes a grant at Google only when no other patient can be
-- using it. Additive only: two nullable columns, NO backfill (rows linked
-- before this stay unknown until the next link; unknown = never revoked).
--
-- google_account_hash:   keyed HMAC of the granting account's e-mail
--                        (oncofiles.google_link.email_hash, the #591 hash);
--                        never the e-mail itself. Written in the SAME upsert
--                        as the token by google_link.store_google_link.
-- google_account_hash_v: version of that keyed hash
--                        (google_link.GOOGLE_ACCOUNT_HASH_VERSION); another
--                        version counts as unknown.
ALTER TABLE oauth_tokens ADD COLUMN google_account_hash TEXT;
ALTER TABLE oauth_tokens ADD COLUMN google_account_hash_v INTEGER;
