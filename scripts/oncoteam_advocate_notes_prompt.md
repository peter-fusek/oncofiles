# Oncoteam: Index Patient Advocate Notes

## What happened
16 patient advocate notes from Apple Notes have been imported into Oncofiles (prod Turso). These are Peter's lay medical analysis notes — preparation for oncologist visits, post-visit records, clinical decision analysis, and treatment strategy documents. All are in Slovak.

Also soft-deleted duplicate docs 33/34 (phone screenshots identical to 44/45).

## New documents (prod IDs 46-61)

| ID | Date | Filename | Text | Content |
|----|------|----------|------|---------|
| 46 | 2025-01-22 | BoryNemocnicaPrepustaciaSprava.md | 135c | Attachment-only: ref to Bory discharge PDF |
| 47 | 2025-01-27 | SumarPrepustacejSpravyPoOperacii.md | 6.0K | Post-op analysis: staging, pathology, lymph nodes, treatment plan |
| 48 | 2025-01-28 | NOU[PHYSICIAN_REDACTED]Otazky.md | 4.7K | Pre-visit questions for Dr. Porsok at NOU |
| 49 | 2025-01-28 | BoryOtazky.md | 3.3K | 22 clinical questions for Bory follow-up |
| 50 | 2025-02-06 | [PHYSICIAN_REDACTED]NOUAnalyza.md | 7.0K | FOLFOX vs FOLFIRI analysis, treatment decision |
| 51 | 2026-02-03 | [PHYSICIAN_REDACTED]LiecebnyPlan.md | 8.6K | Treatment plan analysis, biomarkers, clinical trials |
| 52 | 2026-02-06 | PrijemChemoABioliecba.md | 2.4K | Chemo admission notes, bio-therapy discussion |
| 53 | 2026-02-12 | SumarOchorenia.md | 10K | Comprehensive disease summary (TNM, staging, treatment) |
| 54 | 2026-02-13 | KlinickeStudie.md | 5.9K | Clinical trial landscape analysis for mCRC MSS |
| 55 | 2026-02-20 | PsychoterapeutMaslik.md | 116c | Psychotherapist contact note |
| 56 | 2026-02-20 | [PHYSICIAN_REDACTED]HER2.md | 117c | MOUNTAINEER 3, HER2 note (attachment-only) |
| 57 | 2026-02-22 | BioliecbaPreMSSpMMR.md | 140c | Bio-therapy for MSS/pMMR (attachment-only) |
| 58 | 2026-02-27 | PrijemChemo.md | 305c | Chemo admission: [MEDICATION_REDACTED] 6000, NGS mutations |
| 59 | 2026-03-02 | Po2ChemoPrepustenie.md | 3.6K | Post-2nd chemo assessment with lab comparison table |
| 60 | 2026-03-13 | Pred3Chemo.md | 3.7K | Pre-3rd chemo prep: lab trends, SII, questions |
| 61 | 2026-02-12 | SumarOchorenia.pdf | 33MB | PDF version of doc 53 (same content) |

## Cross-references to existing documents

These advocate notes discuss/reference existing oncofiles documents:

- **Doc 46** (2025-01-22 Bory discharge) → relates to docs 28, 29 (Bory discharge)
- **Doc 47** (2025-01-27 post-op summary) → relates to docs 25, 27, 30 (Bory post-op)
- **Doc 48** (2025-01-28 NOU questions) → relates to doc 22 (Porsok report)
- **Doc 50** (2025-02-06 FOLFOX vs FOLFIRI) → treatment decision analysis
- **Doc 51** (2026-02-03 treatment plan) → relates to docs 18, 19 (NOU pathology/referral)
- **Doc 52** (2026-02-06 chemo admission) → relates to docs 3, 4 (labs)
- **Doc 53** (2026-02-12 disease summary) → relates to docs 5, 6, 13 (genetics, pathology)
- **Doc 54** (2026-02-13 clinical trials) → relates to docs 10, 11 (chemo reports)
- **Doc 58** (2026-02-27 chemo admission) → relates to docs 1, 2 (labs, report)
- **Doc 59** (2026-03-02 post-2nd chemo) → relates to doc 1 (report)
- **Doc 60** (2026-03-13 pre-3rd chemo) → relates to latest lab docs

## Tasks for Oncoteam

1. **Read key notes** via `view_document()` — especially docs 47, 50, 51, 53, 54, 59, 60 (the content-rich ones)
2. **Create conversation entries** via `log_conversation()` linking advocate notes to existing medical docs by document_ids
3. **Cross-reference treatment events** — notes mention specific chemo cycles, verify alignment with existing treatment_events
4. **Extract any new clinical data** — the notes may contain observations not in formal medical documents
5. **Verify biomarker consistency** — check that advocate notes' understanding of [BIOMARKER_REDACTED], [BIOMARKER_REDACTED], HER2-neg matches the verified profile
6. **Flag any discrepancies** between advocate notes and formal medical records

## Important context

- Institution: `PacientAdvokat` = Peter (patient advocate/caregiver)
- Category: `other` (no dedicated advocate category)
- Language: Slovak (SK)
- These are SUPPLEMENTARY documents — advocate's analysis alongside formal medical records
- All 15 MD notes have AI summaries and structured metadata already generated
- Docs 55-57 are minimal (attachment references only — photos embedded in Apple Notes)
- Doc 61 (PDF) is the same content as doc 53 (MD) — different format
- Docs 33/34 were soft-deleted (duplicates of 44/45)
- Total active docs: 59
