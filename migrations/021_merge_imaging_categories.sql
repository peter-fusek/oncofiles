-- Migration 021: Merge imaging_ct and imaging_us into imaging
-- These subcategories were always empty in practice. All imaging docs
-- use the parent "imaging" category with subtype in structured_metadata.

UPDATE documents SET category = 'imaging' WHERE category = 'imaging_ct';
UPDATE documents SET category = 'imaging' WHERE category = 'imaging_us';
