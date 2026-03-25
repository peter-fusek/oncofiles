-- Migration 031: Merge duplicate categories (v4.6.0 taxonomy cleanup)
-- surgical_report → surgery
-- discharge_summary → discharge

UPDATE documents SET category = 'surgery'
WHERE category = 'surgical_report';

UPDATE documents SET category = 'discharge'
WHERE category = 'discharge_summary';
