-- #950: 6-character address tokens (maintainer decision 2026-11-22, easier
-- to dictate). Additive only: two nullable columns, no backfill.
--
-- token_length:  length of the token whose hash this row keeps. NULL = a row
--                written before this migration, i.e. a 10-character token.
--                New rows carry 6.
-- grace_ends_at: set only when a 10-character row is superseded by its
--                6-character successor (oncofiles.inbound_email.ensure_address):
--                the old row is inactive but still routes until this instant,
--                then lapses by a time check. Never deleted. Rotation clears it
--                (a rotated-away address stops at once).
ALTER TABLE inbound_addresses ADD COLUMN token_length INTEGER;
ALTER TABLE inbound_addresses ADD COLUMN grace_ends_at TEXT;
