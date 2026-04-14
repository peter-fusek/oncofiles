-- Support for multi-document PDF splitting and multi-file consolidation.
-- group_id: shared UUID for documents belonging to the same logical group
-- part_number: 1-based position within a group
-- total_parts: total count of parts in the group
-- split_source_doc_id: original document ID this was split from

ALTER TABLE documents ADD COLUMN group_id TEXT;
ALTER TABLE documents ADD COLUMN part_number INTEGER;
ALTER TABLE documents ADD COLUMN total_parts INTEGER;
ALTER TABLE documents ADD COLUMN split_source_doc_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_documents_group_id ON documents(group_id) WHERE group_id IS NOT NULL;
