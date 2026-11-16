# README — ${display}: folder standard

- **Patient / Pacient:** ${display} (`${token}`)
- **Date of birth / Dátum narodenia:** **${dob}** — never a document date.
- **Patient type / Typ pacienta:** oncology
- **Standard version:** 1.0 — ${created} (template `oncology`, created by Oncofiles setup)

This file is the contract for this folder. The Oncofiles curated pipeline reads it before every
run and follows it to the letter. A run that cannot follow it stops and explains why instead of
guessing. Edit it only if you know what you are changing.

Tento súbor je pravidlo pre tento priečinok. Oncofiles ho prečíta pred každým spracovaním a
riadi sa ním. Ak sa ním nedá riadiť, spracovanie sa zastaví a vysvetlí prečo.

## 1. Filename convention

```
YYYYMMDD-PatientName-Institution-DocumentType-DoctorName-HospitalName[-Subject][-nn].ext
```

- `YYYYMMDD` is the clinical date printed in the document (examination, sampling, report).
  Never the birth date, the print date or the scan date. No printed date: the document stays in
  the inbox for review.
- Every field is CamelCase ASCII without diacritics, spaces or underscores. A missing value is
  `Unknown`.
- `PatientName` is always the patient token from the line above.

## 2. Folder structure / Štruktúra priečinkov

| Folder | DocumentType | Contains |
|---|---|---|
| `00-inbox — nové skeny` | — | Drop zone: put new scans and files here (files directly in this folder's root are picked up too) |
| `report — lekárske správy` | `Report` | Doctor's reports, outpatient visits, consultations, follow-ups |
| `labs — laboratórne výsledky` | `Labs` | Blood tests, tumour markers, urine and other laboratory results |
| `imaging — zobrazovanie` | `Imaging` | CT, MRI, PET, ultrasound, X-ray reports |
| `pathology — patológia` | `Pathology` | Histology, cytology, biopsy reports |
| `genetics — genetika` | `Genetics` | Molecular and genetic testing (tumour panels, germline tests) |
| `treatment — priebeh liečby` | `Treatment` | Chemotherapy and other treatment sheets, cycle records, radiotherapy plans |
| `prescription — recepty` | `Prescription` | Prescriptions and medication lists |
| `surgery — operácie` | `Surgery` | Operation reports and surgical protocols |
| `discharge — prepúšťacie správy` | `Discharge` | Hospital discharge summaries |
| `referral — odporúčania` | `Referral` | Referrals and request forms |
| `reference — odborné materiály` | `Reference` | Guidelines, leaflets, articles (not about one visit of the patient) |
| `advocate — poznámky opatrovateľa` | `Advocate` | The caregiver's own notes, questions and summaries |
| `admin — administratíva` | `Admin` | Insurance, invoices, appointments, certificates, other paperwork |
| `other — ostatné` | `Other` | Fallback when nothing else fits |
| `98-logs — logy a archív` | — | Run reports and logs written by Oncofiles |
| `99-duplicates — duplikáty` | — | Quarantine for exact duplicates (nothing is ever deleted) |

The folder set is fixed: Oncofiles never creates, renames or deletes folders after setup.
DocumentType folders are flat (no subfolders).

## 3. Controlled vocabularies

- empty at setup; the vocabularies grow with the documents.
- **Institution**: none yet. A new institution printed in a document is added below
  automatically; an unclear one waits in the inbox for review.
- **Known doctors**: none yet. A new doctor printed in a document is added below
  automatically; an unclear one waits in the inbox for review.
- **HospitalName / Subject**: free; a new value is accepted and reported.

## 4. Rules

- Nothing is ever deleted. Exact duplicates are moved to `99-duplicates`.
- Each document gets a text sidecar `<file>.md` next to it (extracted text and metadata); source
  and sidecar always move together.
- A document stays in `00-inbox` with a sidecar explaining why when its date, type, institution
  or doctor is uncertain, when it holds several documents, or when it is handwritten and unclear.
- Every change is logged in `_actions.jsonl` and `_changelog.md`; `_index.json` lists the folder.
- Text is read on the Oncofiles server with a local model; document content is not sent to a
  cloud AI service by this pipeline.
