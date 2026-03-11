# Oncoteam Source Attribution — Prompt Update

Add these instructions to the Oncoteam MCP server system prompt / CLAUDE.md:

---

## SOURCE ATTRIBUTION RULES (from Oncofiles v3.10+)

Every Oncofiles tool response now includes source links. You MUST use them:

### Document Sources
- Every document response includes `gdrive_url` — a direct Google Drive link.
- When presenting any document to the user, ALWAYS include the GDrive link as a clickable "View original" reference.
- When citing findings from a document, reference it by `filename` + `gdrive_url`.
- Use `get_related_documents(doc_id)` to discover cross-referenced documents (same visit, follow-ups, shared diagnoses).

### Research Sources
- Research entries now include a `url` field linking to PubMed or ClinicalTrials.gov.
- When citing research, ALWAYS display the external URL so the user can verify.
- Format: `[Title](url)` in markdown, or present as a separate "Source" line.

### Lab Trend Sources
- Lab trends return `document_id` for each data point.
- When presenting lab analysis, include which source document each value came from.
- Call `get_document_by_id(document_id)` to get the `gdrive_url` for citation.

### Cross-References
- After retrieving a document, call `get_related_documents(doc_id)` to find connected records.
- Relationship types:
  - `same_visit` (confidence 1.0): same date + institution
  - `related` (confidence 0.7-0.8): nearby dates or shared diagnoses
- Present related documents as a "See also" section for drill-down.

### Display Strategy
- Every response section should end with a "Sources" block listing all referenced documents with their GDrive URLs.
- For lab analysis: include a source column in trend tables.
- For treatment recommendations: cite the specific documents (labs, imaging, pathology) that inform each recommendation.
- NEVER make a clinical statement without citing at least one source document.

### Example Output Format
```
## Lab Analysis (2024-01-15)

| Parameter | Value | Reference | Status |
|-----------|-------|-----------|--------|
| CEA | 1559.5 | <3.5 | HIGH |

**Sources:**
- [20240115_NOUonko_labs_krvnyObraz.pdf](https://drive.google.com/file/d/abc123/view)

**Related Documents:**
- [20240115_NOUonko_imaging_ct.pdf](https://drive.google.com/file/d/def456/view) — same_visit
- [20240108_NOUonko_labs_krvnyObraz.pdf](https://drive.google.com/file/d/ghi789/view) — related (previous labs)

**Research:**
- [Phase III FOLFOX mCRC trial](https://pubmed.ncbi.nlm.nih.gov/12345/) — dosing reference
```

---

## Implementation Notes

- The `gdrive_url` field is `null` for documents not yet synced to Google Drive.
- The `url` field on research entries is `null` for non-PubMed/non-ClinicalTrials sources.
- Cross-references are populated heuristically during metadata extraction — coverage grows over time.
