-- Migration 044: Backfill institution from structured_metadata.providers
-- Fix for #261: 45/51 e5g docs have NULL institution despite AI-extracted providers.
-- PROVIDER_TO_INSTITUTION mapping in enhance.py was expanded — this migration applies
-- the new mappings retroactively to existing docs.
-- Uses case-insensitive LIKE matching against the structured_metadata JSON column.
-- Only updates docs where institution IS NULL (safe — won't overwrite existing values).

-- SvMichal (Nemocnica svätého Michala)
UPDATE documents SET institution = 'SvMichal',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND structured_metadata LIKE '%Michala%';

-- Medifera
UPDATE documents SET institution = 'Medifera',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%MEDIFERA%' OR structured_metadata LIKE '%Medifera%' OR structured_metadata LIKE '%medifera%');

-- Unilabs
UPDATE documents SET institution = 'Unilabs',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%Unilabs%' OR structured_metadata LIKE '%unilabs%' OR structured_metadata LIKE '%UNILABS%');

-- VeselyKlinika (VESELY Očná Klinika, VESELA SÚKROMNÁ KLINIKA)
UPDATE documents SET institution = 'VeselyKlinika',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%VESELY%' OR structured_metadata LIKE '%Vesely%' OR structured_metadata LIKE '%VESELA%' OR structured_metadata LIKE '%Vesela%');

-- Urosanus
UPDATE documents SET institution = 'Urosanus',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%Urosanus%' OR structured_metadata LIKE '%urosanus%' OR structured_metadata LIKE '%UROSANUS%');

-- Sportmed
UPDATE documents SET institution = 'Sportmed',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%SPORTMED%' OR structured_metadata LIKE '%Sportmed%' OR structured_metadata LIKE '%sportmed%');

-- ProSanus
UPDATE documents SET institution = 'ProSanus',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%PRO SANUS%' OR structured_metadata LIKE '%Pro Sanus%' OR structured_metadata LIKE '%ProSanus%');

-- UNZBratislava (ÚNZ mesta Bratislavy)
UPDATE documents SET institution = 'UNZBratislava',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%NZ mesta%' OR structured_metadata LIKE '%zemn% poliklinika%');

-- Aseseta
UPDATE documents SET institution = 'Aseseta',
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE institution IS NULL AND (structured_metadata LIKE '%ASESETA%' OR structured_metadata LIKE '%Aseseta%' OR structured_metadata LIKE '%aseseta%');
