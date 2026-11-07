# README — Jana: folder standard (synthetic test fixture)

- **Patient / Pacientka:** Jana Nováková (`JanaNovakova`)
- **Date of birth / Dátum narodenia:** **03.02.1970** — never a document date.
- **Standard version:** 1.0 — 2026-11-07 (test)

## 1. Filename convention

```
YYYYMMDD-PatientName-Institution-DocumentType-DoctorName-HospitalName[-Subject][-nn].ext
```

## 2. Folder structure / Štruktúra priečinkov

| Folder | DocumentType | Contains |
|---|---|---|
| `00-inbox — nové skeny` | — | Drop zone |
| `report — lekárske správy` | `Report` | Reports |
| `labs — laboratórne výsledky` | `Labs` | Labs |
| `imaging — zobrazovanie` | `Imaging` | Imaging |
| `treatment — priebeh liečby` | `Treatment` | Treatment |
| `other — ostatné` | `Other` | Fallback |
| `98-logs — logy a archív` | — | Logs |
| `99-duplicates — duplikáty` | — | Quarantine |

## 3. Controlled vocabularies

**`Institution`**:

```
NOU                  Sample national institute
SampleHospital       Sample hospital
Unknown
```

**Known doctors:** `Kovacova`, `Hruskova`.

## 4. Text sidecars

The DOB is **03.02.1970** and appears on every letterhead.
