# Inventár pred presunom Oncofiles na domáci Mac mini

**Stav:** iba inventár a posúdenie. Nič sa nemigrovalo ani nemenilo.
**Dátum zberu:** 2026-11-05. Zbieralo sa len čítaním: repo, verejné endpointy `oncofiles.com`, DNS, lokálny beh testov na Apple Silicon.
**Repo je verejné.** Preto tu nie sú tajné hodnoty, mená pacientov, interné IP adresy (Tailscale), e-maily administrátorov ani interné ID Coolify aplikácie.

Značka **NEOVERENÉ** znamená, že sa to v tomto behu nedalo priamo overiť. Pri takej položke uvádzam, odkiaľ informácia pochádza.

---

## (a) Zhrnutie

1. **Oncofiles už nebeží na Railway.** Od 2026-06-08 beží na serveri Forpsi/Aruba (VPS) pod **Coolify** (Railpack build, Traefik + Let's Encrypt). Overené: DNS A záznam, TLS certifikát a `server: uvicorn` za proxy. Reálny presun je teda **Forpsi/Coolify → Mac mini**. V repe z Railway zostali len pozostatky: `railway.toml`, `DISASTER_RECOVERY.md`, komentáre v kóde.
2. **Railway CLI je odhlásené** („Unauthorized“). Neprihlasoval som sa. Či Railway projekt ešte existuje a či sa zaň platí: **NEOVERENÉ**.
3. Prod je jeden proces `uv run oncofiles-mcp` (Python 3.12, FastMCP 3.1.1). Transport je streamable HTTP na `/mcp` a beží **bezstavovo** (`stateless_http=True`), takže sa nedrží žiadna relácia v pamäti. Spotreba je ~118 MB RSS. Výkonovo je to pre Mac mini triviálne.
4. **Dáta sú jeden súbor libSQL/SQLite** (`file:/data/oncofiles.db` na bind mounte Coolify). Turso Cloud je zrušené. Konzistentný export znamená zastaviť app a skopírovať súbor, prípadne `VACUUM INTO`.
5. **Na Apple Silicon to funguje.** Na M5 (arm64, macOS 27.2) prešlo **1360/1360 testov**. Natívne závislosti majú arm64 wheel: pymupdf 1.27.2, libsql 0.1.11, pydantic-core; cryptography má universal2.
6. **Google integrácia je mŕtva** od 2026-07-06 (GCP projekty sú zmazané a nedajú sa obnoviť, #552). Prod beží s `GOOGLE_INTEGRATION_ENABLED=false`. Presun teda nemusí riešiť Google OAuth redirecty, kým sa nerozhodne #539.
7. **Kľúčová vec pre bezvýpadkový prechod:** všetky verejné URL (MCP konektor, OAuth issuer, redirect URI) sú napevno `https://oncofiles.com`. **Ak zostane doména a skopírujú sa DB a tajomstvá 1:1, konektory v claude.ai a ChatGPT u všetkých používateľov fungujú ďalej bez zásahu.**
8. Na Mac mini som sa v tomto behu **nedostal**. Body 9–13 sú preto odporúčania s otvorenými otázkami, nie meranie.
9. **Najväčšie riziká:** (1) momentálne neexistuje overená záloha prod DB mimo servera, lebo GCS záloha zanikla spolu s GCP; (2) FileVault a výpadok prúdu; (3) zdravotné údaje na domácom stroji (GDPR); (4) zverejnenie do internetu, keďže DNS je na websupport.sk, nie na Cloudflare.
10. **Odporúčaná cesta:** nechať Forpsi zatiaľ ako **reverzný proxy a TLS front** a do Mac mini posielať prevádzku cez Tailscale. DNS, certifikát ani IP sa nemenia a návrat je len prepnutie cieľa v proxy (minúty). Úplné odstavenie Forpsi až po overení.

---

## (b) Inventár podľa bodov 1–13

### 1. Hosting: Railway (historicky) a Forpsi/Coolify (súčasnosť)

| Položka | Zistenie | Overenie |
|---|---|---|
| Súčasný hosting | Forpsi/Aruba VPS (rDNS `*.serverdedicati.aruba.it`), Coolify | DNS + TLS overené dnes |
| Coolify | projekt `oncofiles`, 1 aplikácia z vetvy `main`, build pack **Railpack (Beta)**, install `uv sync`, start `uv run oncofiles-mcp` | z poznámok z 2026-08-06/07, dnes **NEOVERENÉ** (Coolify UI je za prihlásením) |
| Prostredia | len **prod**, staging neexistuje | repo a poznámky |
| Inštancie | 1 (single-process, scheduler v procese) | kód: `_sync_semaphore = Semaphore(1)` |
| Uptime | `uptime_s` 1 394 947 ≈ **16,1 dňa** (štart ~2026-10-20) | `/health` dnes |
| RAM | 118 MB RSS, peak 123 MB, rast 0 MB/h | `/health` dnes |
| CPU | **NEOVERENÉ**: bez prístupu na box nie je telemetria. Typicky nečinné, nočný AI pipeline volá Anthropic API, nepočíta lokálne | |
| Región | VPS v EÚ (Aruba/Forpsi). Presné DC **NEOVERENÉ** | |
| Mesačná cena Forpsi | **NEOVERENÉ** | |
| Railway | CLI odhlásené. Posledné GitHub deployments od `railway-app[bot]` sú z 2026-06-08 (zvyšky po teardowne). Existencia projektu a fakturácia **NEOVERENÉ** | `railway whoami` → Unauthorized |
| Oncoteam | **Oncoteam volá Oncofiles** cez MCP na `https://oncofiles.com/mcp` (repo oncoteam). Oncofiles volá oncoteam voliteľne cez webhook (`ONCOTEAM_WEBHOOK_URL` / `_TOKEN`). Či je webhook v prode nastavený: **NEOVERENÉ**. Connector Oncoteam v claude.ai dnes vracia 404, takže oncoteam je zrejme sám nefunkčný (GCP cleanup, #552) | grep v oboch repách |

### 2. Build a beh

| Položka | Hodnota |
|---|---|
| Jazyk | Python **≥3.12** (lokálne 3.12.12), správca `uv`, lockfile `uv.lock` |
| Hlavné závislosti | fastmcp ≥3.1 (beží 3.1.1), anthropic, pydantic 2, aiosqlite, **pymupdf** (natívna, PDF/OCR), **libsql** (natívna), apscheduler 3.x, google-api-python-client, sqlglot, httpx |
| Build | **Bez Dockerfile.** Coolify používa Railpack. `docker-compose.yml` má `build: .`, ktorý nikam nevedie, čiže bez Dockerfile nefunguje. `railway.toml` je pozostatok |
| Štart | `uv run oncofiles-mcp` |
| Port | `MCP_PORT` → `PORT` → default **8000**, host `0.0.0.0` |
| Healthcheck | `/health`: len liveness, **nesmie volať DB**, inak ho Railway/Coolify po 120 s zabije. `/readiness`: kontrola DB (5 s timeout) |
| MCP transport | **streamable HTTP** na `/mcp` (POST/DELETE, GET → 405), **stateless** → reštart neruší relácie konektorov |
| Stav relácie | žiadny v pamäti. OAuth klienti a tokeny MCP sú **v DB** (`mcp_oauth_clients`, `mcp_oauth_tokens`). Odvolané dashboard relácie sú v `session_revocations` a pri štarte sa načítajú do pamäte |
| Apple Silicon | **Overené:** `uv run --frozen` na M5/arm64, import pymupdf/libsql/fastmcp OK, **1360 testov prešlo** (79 s). Natívny beh cez launchd nepotrebuje Docker. Docker image by bolo treba napísať (neexistuje) |

### 3. Dáta

| Položka | Zistenie | Overenie |
|---|---|---|
| Databáza | **libSQL/SQLite súbor**, `TURSO_DATABASE_URL=file:/data/oncofiles.db`. `TURSO_AUTH_TOKEN` a `TURSO_REPLICA_PATH` sú prázdne. Turso Cloud skupina je zmazaná (404) | poznámky z 2026-08-06, dnes **NEOVERENÉ** |
| Fyzicky | bind mount Coolify na VPS (`/data/coolify/applications/<id>/data` → `/data` v kontajneri), prežije rebuild | poznámky |
| Veľkosť | **NEOVERENÉ.** Historicky ~170 MB pri Turso replike. Lokálna stará kópia z marca 2026 má ~9 MB vrátane WAL, ale je zastaraná | |
| Riadky | verejné `/api/stats`: **344 dokumentov**, 106 nástrojov, 18 kategórií. Počty v ostatných tabuľkách **NEOVERENÉ** (vyžaduje operátorský bearer alebo SSH; obsah som zámerne nečítal) | `/api/stats` dnes |
| Schéma | 67 SQL migrácií v `migrations/` (posledná `070_onboarding_events.sql`), sledované v `schema_migrations`. Hlavné tabuľky: `patients`, `patient_context`, `patient_tokens`, `documents`, `document_pages`, `document_cross_references`, `document_references`, `lab_values`, `treatment_events`, `clinical_records` (+ audit, notes), `clinical_analyses`, `conversation_entries`, `research_entries`, `email_entries`, `calendar_entries`, `oauth_tokens` (Google), `mcp_oauth_clients`, `mcp_oauth_tokens`, `session_revocations`, `prompt_log`, `sync_history`, `activity_log`, `agent_state`, `onboarding_events`, `newsletter_subscribers` | repo |
| Migrácie | aplikujú sa **pri štarte** automaticky. Hromadné opravy nad 500 riadkov sa nesmú robiť v migráciách (#476) | CLAUDE.md |
| Dokumenty | samotné súbory sú v **Google Drive** opatrovateľov, nie na serveri. Server drží metadáta, OCR text a AI výstupy. Pozor: tie sú tiež zdravotné údaje | architektúra |
| Zálohy | **Žiadna overená záloha mimo boxu.** `scripts/backup_to_gcs.py` cielil GCS, ale GCP projekty sú zmazané (#552) a #425 je otvorené. Či Coolify/Forpsi robí snapshoty VPS: **NEOVERENÉ** | |
| Konzistentný export | (1) zastaviť app v Coolify, (2) skopírovať `oncofiles.db` **spolu s `-wal`/`-shm`**, alebo najprv urobiť `PRAGMA wal_checkpoint(TRUNCATE)`, (3) `PRAGMA integrity_check` na kópii, (4) porovnať `SELECT count(*)` po tabuľkách na zdroji a cieli. Za behu: `sqlite3 oncofiles.db "VACUUM INTO '/tmp/snap.db'"` dá konzistentný snapshot bez zastavenia | návrh |

### 4. Integrácie

| Integrácia | Stav | Závisí od URL? |
|---|---|---|
| Google Drive / Gmail / Calendar (OAuth klient + service account) | **mŕtve**: GCP projekty zmazané 2026-07-06, nedajú sa obnoviť. Kill switch `GOOGLE_INTEGRATION_ENABLED=false` | redirect `GOOGLE_OAUTH_REDIRECT_URI` (default `https://oncofiles.com/oauth/callback`). Pri prípadnej obnove v novom GCP projekte: zaregistrovať redirect na `oncofiles.com` a všetci opatrovatelia sa musia znovu prihlásiť |
| Google Drive cez lokálny mount (#539, návrh) | Drive for Desktop na Macu má zdieľané priečinky pripojené a zapisovateľné. Pre Mac mini to je **zaujímavá alternatíva** k Drive API (premenovanie = `os.rename`). Nič nie je postavené | nie |
| Anthropic API | `ANTHROPIC_API_KEY`, volania priamo na api.anthropic.com, nočný pipeline 23:00 UTC | nie |
| E-mail (Resend) | `RESEND_API_KEY`, `NOTIFY_FROM_EMAIL`, `NOTIFY_TO_EMAIL`. Upozornenia od `sync_watchdog`. Doména odosielateľa na Resend **NEOVERENÉ** | nie (odchádzajúce) |
| GitHub (bug reporty z dashboardu) | `GITHUB_TOKEN`, `GITHUB_REPO`. Token je podľa poznámok **expirovaný** a čaká na rotáciu (#550) | nie |
| Oncoteam webhook | `ONCOTEAM_WEBHOOK_URL`/`_TOKEN` (odchádzajúci; odstránený v #721) | áno, ak sa zmení URL oncoteamu |
| Prichádzajúce webhooky | žiadne. GitHub repo hooks: 0. Coolify deploy webhook nie je registrovaný (#549) | |
| MCP OAuth (Claude.ai, ChatGPT: DCR) | issuer `https://oncofiles.com/`, `/authorize`, `/token`, `/register` | **áno**: `base_url="https://oncofiles.com"` je **napevno** v `server.py` |

### 5. Domény a prístup

| Položka | Zistenie |
|---|---|
| Domény | `oncofiles.com` a `www.oncofiles.com`, A → Forpsi VPS, **TTL 600 s**. `*.up.railway.app` sa už nepoužíva (Railway URL oncoteamu vracia 404) |
| DNS | **websupport.sk** (`ns1–3.websupport.sk`). Na tej istej zóne je **e-mail** (MX `mx10/mx20.websupport.sk`, SPF `include:_spf.m1.websupport.sk`). Pri presune NS na Cloudflare treba MX/SPF/DKIM preniesť presne |
| TLS | Let's Encrypt (YR2), platí do **2027-01-05**, obnovuje ho Traefik v Coolify. HSTS `max-age=31536000; includeSubDomains` → doména **musí** stále bežať na platnom HTTPS (vrátane subdomén) |
| Konektor claude.ai/ChatGPT | URL `https://oncofiles.com/mcp`, rovnaká pre všetkých. Koľko používateľov ho má pridaný: **NEOVERENÉ** (počet riadkov `mcp_oauth_clients`; podľa incident poznámky bolo 33 opatrovateľov s Google tokenmi) |
| Autentifikácia | (1) **statický bearer** `MCP_BEARER_TOKEN` → default pacient a operátor; (2) **pacientske tokeny** `onco_*` (DB `patient_tokens`); (3) **MCP OAuth + DCR**, identita z Google prihlásenia pri `/authorize` (pri mŕtvom Google OAuth je **nový** login cez OAuth **NEOVERENE funkčný**, pravdepodobne nefunguje); (4) **dashboard session cookie** (HMAC kľúč odvodený z bearera alebo `DASHBOARD_SESSION_SECRET`) |
| Admin a opatrovateľ | `DASHBOARD_ADMIN_EMAILS` (alias `DASHBOARD_ALLOWED_EMAILS`) vidí všetkých pacientov. Ostatní len pacientov s `caregiver_email` = ich e-mail. ACL brána `_resolve_patient_id` |
| Kritické pre kontinuitu | `MCP_BEARER_TOKEN`, `DASHBOARD_SESSION_SECRET`, `OAUTH_STATE_SECRET` musia na Mac mini mať **identické hodnoty**. Inak prestanú platiť všetky session cookies a podpísané stavy a konektory sa musia znovu autorizovať |

### 6. Plánované úlohy

Všetko beží **v procese** cez APScheduler. Zapína ho `SYNC_ENABLED`. Úlohy, ktoré používajú Google, zastavuje `GOOGLE_INTEGRATION_ENABLED`. Všetky úlohy, ktoré pracujú s DB, sú serializované `Semaphore(1)`. Scheduler je **multi-patient**: iteruje cez všetkých pacientov.

ID úloh v kóde: `memory_check`, `sync_watchdog`, `nightly_ai_pipeline` (23:00 UTC, `misfire_grace_time=1800`), `gmail_sync`, `calendar_sync`, `gdrive_sync`, `startup_catchup`, `gmail_startup_sync`, `calendar_startup_sync`, `enhancement_sweep`, `metadata_extraction`, `category_validation`, `document_groups_backfill`, `pipeline_integrity`, `empty_folder_cleanup`, `trash_cleanup`, `oauth_token_cleanup`, `prompt_log_cleanup`, `tier_transition`, `weekly_analytics`, `weekly_discovery_pipeline`, `nightly_ai_oneoff`. Niektoré sa registrujú len v legacy režime `AI_NIGHTLY_ONLY=false`. Podľa poznámok z augusta je v prode aktívnych 14.

Mimo procesu: bývalý Railway cron `oncofiles-backup` (02:00 UTC, GCS) je **zaniknutý**. Lokálne `scripts/com.oncofiles.memory-backup.plist` je launchd záloha Claude memory, nie prod DB. Externé „triggery“ (denný health 06:00 UTC, týždenný ops audit) sú Claude remote triggery a podľa #524 sú slepé.

**Dve inštancie naraz (počas prechodu):** nebezpečné. Nie je zámok medzi inštanciami a každá má **vlastnú kópiu** DB súboru. Hrozí:
- **split-brain**: zápisy (nové OAuth tokeny konektorov, dokumenty, poznámky) skončia v DB, ktorú potom zahodíme;
- **dvojité AI volania a náklady**: obe spustia `nightly_ai_pipeline`;
- **dvojité e-maily**: `sync_watchdog` cez Resend;
- po obnove Google: **dvojité premenovania a presuny** v Drive a `_cleanup_orphan_ocr` môže presúvať do koša.

→ Pravidlo: **v každom okamihu beží scheduler len na jednom stroji.** Na novom stroji štartovať najprv so `SYNC_ENABLED=false` (read-only smoke test proti kópii DB), alebo zdroj úplne zastaviť pred skopírovaním DB.

### 7. CI/CD a prevádzka

| Položka | Zistenie |
|---|---|
| GitHub Actions | `ci.yml`: ruff lint + format + pytest na push/PR do `main`. **Nedeployuje** |
| Deploy | **ručne**, tlačidlom Redeploy v Coolify. Push do `main` nedeployuje (#549), webhook neexistuje |
| Logy | v Coolify UI (stdout kontajnera). Centralizované logy ani retenčná politika **NEOVERENÉ** / neexistujú |
| Monitoring | `/health` (liveness + `stale_sync_count`, `google_integration`), `/readiness`, `sync_watchdog` → e-mail cez Resend. Externý uptime monitor **NEOVERENÉ** (pravdepodobne žiadny) |
| Pamäťový failsafe | `memory_check` každých 5 min, reštart pri 420 MB (`MEMORY_RESTART_THRESHOLD_MB`) cez SIGTERM. **Spolieha sa na to, že supervisor proces znova spustí** (Coolify `restart: always`, na Macu `launchd KeepAlive`) |

### 8. Čo dáva platforma (Railway/Coolify) a doma chýba

| Funkcia platformy | Doma nahradiť |
|---|---|
| `RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_DEPLOYMENT_ID` (kód ich číta len do logu) | nastaviť napr. `RAILWAY_GIT_COMMIT_SHA=$(git rev-parse HEAD)` v spúšťacom skripte, alebo neskôr premenovať na neutrálne `ONCOFILES_GIT_SHA` |
| Persistentný volume / bind mount | adresár na šifrovanom APFS (napr. `/Users/<svc>/oncofiles/data`) |
| Automatické TLS + verejná URL | Traefik na Forpsi ako front (odporúčané na štart), Cloudflare Tunnel alebo Tailscale Funnel (bod 11) |
| Reštart po páde / po SIGTERM | `launchd` s `KeepAlive=true`, `RunAtLoad=true`, `ThrottleInterval` |
| Healthcheck s automatickým zabitím | voliteľne malý launchd watchdog, ktorý pri zlyhaní `/health` urobí `kickstart -k` |
| Build pipeline | `git pull && uv sync --frozen` + `launchctl kickstart`. Bez Dockeru |
| Privátna sieť | Tailscale (už nainštalovaný) |
| Správa env premenných | súbor `.env` s právami `600`, alebo macOS Keychain. Nikdy v repe |

### Tabuľka premenných prostredia

Zdroj „Coolify env“ znamená, že hodnota je dnes nastavená v Coolify UI (runtime, **nie** „Available at Buildtime“). Ktoré premenné sú v prode skutočne nastavené, som nevidel. Zoznam je **odvodený z kódu**. Ako „nastavené“ sú označené len tie, ktoré potvrdzujú poznámky z augusta.

| Názov | Účel | Tajná? | Zdroj / stav |
|---|---|---|---|
| `MCP_TRANSPORT` | `streamable-http` v prode | nie | Coolify env (predpoklad) |
| `MCP_HOST` / `MCP_PORT` / `PORT` / `HOST` | bind adresa a port (default 0.0.0.0:8000) | nie | Coolify/Railpack |
| `MCP_BEARER_TOKEN` | operátorský bearer + **koreň HMAC kľúčov** (session, OAuth state) | **áno** | Coolify env, **kopírovať 1:1** |
| `DASHBOARD_SESSION_SECRET` | voliteľný override kľúča session cookies | **áno** | Coolify env (ak je), kopírovať 1:1 |
| `OAUTH_STATE_SECRET` | voliteľný override kľúča OAuth state | **áno** | Coolify env (ak je), kopírovať 1:1 |
| `DASHBOARD_ADMIN_EMAILS` / `DASHBOARD_ALLOWED_EMAILS` | kto je admin | nie (osobný údaj) | Coolify env |
| `TURSO_DATABASE_URL` | `file:/data/oncofiles.db` | nie | Coolify env, na Macu zmeniť cestu |
| `TURSO_AUTH_TOKEN` | Turso Cloud, dnes nepoužité | áno | prázdne |
| `TURSO_REPLICA_PATH` | embedded replica, dnes nepoužité | nie | prázdne |
| `DATABASE_PATH` / `LOCAL_DB_PATH` | lokálna SQLite cesta (dev, skripty) | nie | default |
| `ANTHROPIC_API_KEY` | AI klasifikácia/extrakcia | **áno** | Coolify env |
| `GOOGLE_INTEGRATION_ENABLED` | kill switch Google (`false`) | nie | Coolify env, potvrdené `/health` |
| `SYNC_ENABLED` | zapína celý scheduler (`true`) | nie | Coolify env |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | Google OAuth klient (mŕtvy GCP projekt) | ID nie / secret **áno** | Coolify env, bezcenné |
| `GOOGLE_OAUTH_REDIRECT_URI` | redirect po Google OAuth | nie | default `https://oncofiles.com/oauth/callback` |
| `GOOGLE_CREDENTIALS_BASE64` / `GOOGLE_APPLICATION_CREDENTIALS(_JSON)` | service account (zmazaný projekt) | **áno** | Coolify env (ak je), bezcenné |
| `GOOGLE_DRIVE_FOLDER_ID` | default Drive priečinok | nie | Coolify env / DB |
| `GMAIL_ENABLED` / `CALENDAR_ENABLED` | feature flagy | nie | default |
| `SYNC_INTERVAL_MINUTES`, `SYNC_STALE_ALERT_HOURS` | ladenie syncu/watchdogu | nie | default |
| `AI_NIGHTLY_ONLY`, `AI_NIGHT_WINDOW_START_UTC`/`_END_UTC`, `AI_REPROCESS_MAX_AGE_HOURS`, `AI_RUN_ON_STARTUP`, `DAILY_AI_DOC_CAP`, `ENHANCE_TIMEOUT_S`, `ENABLE_INTEGRITY_CHECK` | riadenie AI pipeline a nákladov | nie | default / Coolify env |
| `MAX_DOCUMENTS_PER_PATIENT` | FUP limit (500) | nie | default |
| `MEMORY_RESTART_THRESHOLD_MB`, `HARD_RSS_CEILING_MB`, `MEMORY_TRACEMALLOC_ENABLED` | pamäťové poistky | nie | default |
| `DB_QUERY_SLOTS`, `DB_EXPRESS_SLOTS`, `DB_BACKGROUND_SLOTS` | semafory DB | nie | default (5/5/2 podľa `/health`) |
| `RESEND_API_KEY` | odosielanie e-mailov | **áno** | Coolify env |
| `NOTIFY_FROM_EMAIL` / `NOTIFY_TO_EMAIL` | odosielateľ a príjemca upozornení | nie | Coolify env |
| `GITHUB_TOKEN` | bug reporty → GitHub issues (**expirovaný**, #550) | **áno** | Coolify env, iba runtime |
| `GITHUB_REPO` | cieľové repo bug reportov | nie | default |
| `ONCOTEAM_WEBHOOK_URL` / `ONCOTEAM_WEBHOOK_TOKEN` | notifikácia oncoteamu (odstránené v #721, Oncoteam ukončený) | URL nie / token **áno** | Coolify env (ak je) |
| `ONCOFILES_PREFERRED_LANG`, `LOG_LEVEL` | lokalizácia, logy | nie | default |
| `PATIENT_NAME`, `PATIENT_CONTEXT_PATH` | od #733 sa nečítajú (žiadny legacy pacient) — nenastavovať | — | — |
| `IMPORT_LOCAL_SOURCE`, `IMPORT_TRANSCRIPTS_PATH` | jednorazové importy | nie | nepoužité v prode |
| `BACKUP_BUCKET`, `BACKUP_KMS_KEY`, `BACKUP_GITHUB_TOKEN` | GCS záloha (zaniknutá) | token **áno** | bývalý Railway cron |
| `RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_DEPLOYMENT_ID` | len do logu pri štarte | nie | vkladá platforma, doma nastaviť ručne |

---

### Mac mini (body 9–13)

> **Pozor:** tento beh prebehol na vývojovom MacBooku, nie na Mac mini. **Nič z bodov 9–12 nie je na Mac mini zmerané.**

### 9. Hardvér a systém
- Podľa `docs/architecture/selfhosted_macmini_proposal.md`: **M4 Pro, 14 CPU / 20 GPU jadier, 48 GB RAM**. Model, voľný disk a verzia macOS: **NEOVERENÉ**.
- Na overenie spustiť priamo na Mac mini: `system_profiler SPHardwareDataType; sw_vers; df -h /System/Volumes/Data`.
- Nároky: ~120 MB RAM, DB pod 1 GB, CPU zanedbateľné. Akýkoľvek M4 stačí s veľkou rezervou.

### 10. Prostredie
- Docker/OrbStack/Colima a čo počúva na portoch: **NEOVERENÉ** (`lsof -iTCP -sTCP:LISTEN -n -P`, `which docker orbstack colima`).
- **Odporúčanie: natívne cez `launchd` + `uv`**, nie Docker. Testy natívne na arm64 prechádzajú, odpadá vrstva VM, Drive for Desktop mount (#539) je priamo prístupný a Coolify/Railpack netreba. Docker by dával zmysel len pri zámere zostať prenositeľný späť na Linux VPS. V tom prípade najprv napísať Dockerfile, lebo dnes neexistuje.
- Bežať pod **samostatným štandardným používateľom** (napr. `oncofiles`) ako LaunchDaemon s `UserName`, nie pod Peterovým účtom.

### 11. Dostupnosť z internetu

Konektory claude.ai a ChatGPT potrebujú **verejnú HTTPS adresu** dostupnú zo serverov Anthropic/OpenAI. Samotný tailnet nestačí. Doména **musí zostať `oncofiles.com`**, inak sa rozbijú konektory, OAuth issuer (napevno v kóde) aj HSTS.

| Možnosť | Vlastná doména `oncofiles.com` | TLS | Limity / riziká | Vhodnosť |
|---|---|---|---|---|
| **A. Forpsi ako front** (Traefik/Caddy na VPS → Tailscale → Mac mini:8000) | **áno, bez zmeny DNS** | ostáva LE na VPS ako dnes | VPS stále platený; závislosť na Tailscale spojení; latencia +ms; prevádzka sa dešifruje na VPS ako dnes (žiadna nová tretia strana) | **najlepšie na prechod**, návrat za minúty |
| **B. Cloudflare Tunnel** (`cloudflared` ako služba) | áno, ale **zóna musí byť na Cloudflare** (presun NS z websupport.sk; CNAME/partial setup je len v platených plánoch). Pri presune pozor na MX/SPF | Cloudflare edge cert | **Cloudflare ukončuje TLS a vidí nešifrovaný obsah (zdravotné údaje)**, takže je to ďalší sprostredkovateľ podľa GDPR (DPA, záznam v privacy policy); limity veľkosti requestu (100 MB free) sú pre PDF upload OK | dobré dlhodobo, ak sa NS presunie a Cloudflare sa prijme ako sprostredkovateľ |
| **C. Tailscale Funnel** | **nie**: len `<stroj>.<tailnet>.ts.net`. `oncofiles.com` by sa nedala použiť bez ďalšieho proxy | TLS končí na zariadení (Tailscale nedešifruje) | len porty 443/8443/10000, obmedzená priepustnosť, beta-ish SLA | **nevhodné** ako hlavná cesta (zmena URL konektora pre všetkých); OK na test |
| D. Port forward na routeri + DDNS | áno | LE priamo na Macu (Caddy) | verejná IP domova, CGNAT?, exponovaný domáci router | neodporúčané |

Stav na Mac mini (`tailscale status`, `cloudflared`): **NEOVERENÉ**. Na MacBooku je Tailscale nainštalovaný, `cloudflared` nie je.

### 12. Prevádzka doma
- `pmset -g` a `fdesetup status` na Mac mini: **NEOVERENÉ**. Odporúčané nastavenia: `sudo pmset -a sleep 0 disksleep 0 autorestart 1 womp 1 powernap 0`.
- **FileVault a výpadok prúdu:** so zapnutým FileVault sa po štarte čaká na heslo v pre-boot obrazovke a služby nebežia. Možnosti:
  1. **FileVault nechať zapnutý** (pre zdravotné údaje prakticky nutné) a pre **plánované** reštarty používať `sudo fdesetup authrestart` (jednorazovo obíde heslo). Na **neplánované** výpadky dať **UPS** s USB signalizáciou (macOS ju podporuje natívne cez *Energy → UPS*), aby sa Mac pri dlhom výpadku korektne vypol a nezostal tvrdo vypnutý v strede zápisu.
  2. Po neplánovanom výpadku je potrebný ručný odomyk. Na Apple Silicon nie je zabudovaný vzdialený pre-boot unlock (na rozdiel od niektorých serverov), takže treba fyzický prístup alebo sa zmieriť s výpadkom do príchodu domov. **Toto je hlavný argument nechať Forpsi ako záložný front s možnosťou rýchleho návratu.**
  3. Vypnúť FileVault a šifrovať len dátový APFS zväzok, ktorý sa odomyká z Keychainu: slabšie, neodporúčam pre zdravotné údaje.
- **Aktualizácie macOS:** vypnúť automatickú *inštaláciu* (nechať sťahovanie) a aktualizovať ručne v ohlásenom okne, s `fdesetup authrestart`.
- **Zálohy:** Time Machine na lokálny šifrovaný disk **+ kópia mimo domu** (napr. šifrovaný `restic`/`rclone` snapshot DB súboru do EÚ úložiska, alebo naďalej na Forpsi VPS). Dnes neexistuje žiadna záloha mimo boxu (bod 3), takže toto vyriešiť **pred** migráciou bez ohľadu na Mac mini.
- **Internet doma:** typicky bez SLA, dynamická IP, možný CGNAT. Pri možnosti A/B to nevadí (odchádzajúce spojenie), pri výpadku je ale celá služba dole.
- **UPS** pre Mac mini a router/ONT.

### 13. Bezpečnosť a GDPR
- **Kategória údajov:** zdravotné údaje (čl. 9 GDPR) pacientov zo SK/EÚ. Ide aj o tretie osoby (pacienti iných opatrovateľov), nielen o Petrovu rodinu, takže „domáca výnimka“ (čl. 2 ods. 2 písm. c) **neplatí**.
- **Šifrovanie disku:** FileVault zapnutý (povinné). Zálohy tiež šifrované.
- **Fyzický prístup:** kto ďalší býva v domácnosti alebo má prístup k Macu; zamknutá obrazovka; samostatný servisný používateľ; žiadne automatické prihlásenie (to je s FileVault aj tak nemožné).
- **Prístup na Mac:** kto má admin účet, SSH (iba kľúče, iba cez Tailscale, zakázať heslá), Screen Sharing. Zoznam: **NEOVERENÉ**.
- **Logy:** aplikácia loguje na stdout a môžu obsahovať názvy súborov s menami pacientov. Na Macu presmerovať do súboru s rotáciou (`newsyslog`) a obmedzenými právami. Nesynchronizovať logy do iCloud.
- **iCloud/Spotlight:** dátový adresár vylúčiť zo Spotlight (`.metadata_never_index`) a nesmie byť v iCloud Drive ani v synchronizovanom priečinku.
- **Dokumentácia a súhlasy:** privacy policy (`/privacy` v `server.py`) dnes popisuje hosting. Treba ju aktualizovať: **kde** sa údaje spracúvajú (domáci server v SR, prevádzkovateľ fyzická/právnická osoba?), nových **sprostredkovateľov** (Cloudflare pri možnosti B; Tailscale len prenáša šifrované dáta), a vypustiť Railway/Turso/GCP. Zvážiť **informovanie existujúcich používateľov** o zmene miesta spracúvania. Kto je formálne prevádzkovateľ (Instarea s.r.o. vs. Peter súkromne): **otvorená otázka**, od nej závisí záznam o spracovateľských činnostiach a DPIA.
- **DPIA:** pri zdravotných údajoch vo väčšom rozsahu sa odporúča. Zmena infraštruktúry je vhodný moment na ňu.
- `DISASTER_RECOVERY.md` a `CLAUDE.md` stále opisujú Railway + Turso + GCS. Po migrácii ich treba prepísať.

---

## (c) Čo sa pri presune zmení alebo prestane fungovať

| Vec | Pri zachovaní `oncofiles.com` (odporúčané) | Pri zmene domény alebo URL |
|---|---|---|
| Konektory v claude.ai/ChatGPT (`/mcp`) | **bez zmeny**, ak sa skopíruje DB (`mcp_oauth_*`) a tajomstvá 1:1 | **všetci** používatelia musia konektor odstrániť a pridať znova |
| MCP OAuth issuer / DCR | bez zmeny (`base_url` napevno v kóde) | kód treba upraviť (`server.py`, `base_url`) |
| Google OAuth redirect | dnes irelevantné (Google mŕtvy). Pri obnove v novom GCP projekte zaregistrovať `https://oncofiles.com/oauth/callback` | iná redirect URI v GCP + env |
| Dashboard session cookies | platia ďalej pri rovnakých tajomstvách | odhlásenie všetkých |
| DNS | **možnosť A: žiadna zmena**; B: presun NS na Cloudflare (pozor na MX/SPF/DKIM e-mailu na websupport.sk) | |
| TLS | A: bez zmeny (LE na VPS); B: Cloudflare cert | |
| Oncoteam → Oncofiles | bez zmeny (`https://oncofiles.com/mcp`) | zmeniť URL v oncoteame |
| Oncofiles → Oncoteam webhook | bez zmeny | |
| Deploy | Coolify Redeploy → `git pull` + `uv sync --frozen` + `launchctl kickstart` na Macu | |
| Railway pozostatky | `railway.toml`, `RAILWAY_*` env (len log), docs. Nič nefunguje inak | |
| IP v privacy policy, DR dokumentácii | aktualizovať | |

---

## (d) Riziká a otvorené otázky

### Riziká (zoradené)
1. **Prod DB nemá overenú zálohu mimo servera.** Pred akoukoľvek manipuláciou urobiť a overiť snapshot (`VACUUM INTO` + `integrity_check` + počty riadkov) a uložiť ho šifrovane na dve miesta.
2. **Split-brain pri dvoch bežiacich inštanciách** (bod 6): strata zápisov, dvojité AI náklady a e-maily.
3. **FileVault + výpadok prúdu** znamená výpadok služby až do ručného odomknutia (bod 12).
4. **Domáce pripojenie a prúd** bez SLA; jediný stroj, žiadna redundancia.
5. **GDPR**: zdravotné údaje tretích osôb na domácom stroji. Treba aktualizovať privacy policy, ujasniť prevádzkovateľa, prípadne urobiť DPIA (bod 13).
6. **Chýbajúce tajomstvá na novom stroji** (najmä `MCP_BEARER_TOKEN`) by potichu zneplatnili sessions a HMAC podpisy. Hodnoty treba preniesť bezpečne (nie cez chat ani e-mail), napr. cez 1Password alebo priamo z Coolify do `.env` s právami 600.
7. **Neaktualizovaný runtime:** prod beží 5.18.1.dev0 z `f7a5a19`, `main` je `0abd270` (len docs). `uv sync` bez `--locked` na Coolify vs. `--frozen` doma môže dať iné verzie. Doma používať `--frozen` (overené: 1360 testov).
8. **Expirovaný `GITHUB_TOKEN`**: bug reporty nefungujú (nesúvisí s presunom, ale preniesť ho by nemalo zmysel).
9. Na pracovnom MacBooku leží v pracovnom adresári repa (gitignored) **stará kópia prod DB z marca 2026 a JSON kľúč service accountu** (z dnes zmazaného GCP projektu). Netýka sa presunu, ale ide o zdravotné údaje na ďalšom stroji. Odporúčam presunúť ich do šifrovaného archívu alebo do koša (nie trvalo mazať).

### Otvorené otázky
1. Existuje ešte Railway projekt, ktorý sa platí? (CLI odhlásené, treba skontrolovať v Railway dashboarde.)
2. Prečo je cieľ „Railway → Mac mini“? Platí, že sa vlastne odchádza z **Forpsi/Coolify**? Má Forpsi VPS po migrácii zostať ako front (možnosť A) alebo zaniknúť?
3. Mac mini: presný model, disk, macOS, FileVault, či už beží Linux VM a čo v nej.
4. Pobeží Mac mini natívne (launchd), alebo v Linux VM? Pri VM by šlo o Docker/systemd a FileVault chráni len hostiteľa.
5. Veľkosť prod DB a počty riadkov: potrebný operátorský bearer alebo SSH na VPS (`/readiness` s bearerom).
6. Koľko klientov konektorov (`mcp_oauth_clients`) a aktívnych tokenov existuje, teda koľkých ľudí by zasiahla zmena URL?
7. Ostáva rozhodnutie #539 (lokálny mount vs. obnova GCP)? Mac mini s Drive for Desktop je prirodzený domov pre variant „lokálny mount“. To mení, či treba na Mac mini Google účet a Drive for Desktop pod servisným používateľom.
8. Kto je prevádzkovateľ údajov podľa GDPR a či sú existujúci používatelia informovaní o zmene.
9. Je doma UPS, aké je pripojenie (verejná IP, CGNAT) a kto má fyzický prístup?
10. Kde sú tajné hodnoty uložené mimo Coolify (password manager)? Pri strate Forpsi boxu dnes neexistuje iná kópia: **NEOVERENÉ**.

---

## (e) Návrh poradia migrácie

**Fáza 0: príprava, bez výpadku**
1. Zistiť odpovede na otvorené otázky 1–5. Na Mac mini spustiť read-only príkazy z bodov 9–12.
2. **Zálohovať prod DB teraz**, nezávisle od migrácie (`VACUUM INTO` na VPS → šifrovaná kópia na 2 miesta).
3. Mac mini: FileVault ON, servisný používateľ, `pmset` nastavenia, UPS, Time Machine a offsite záloha, SSH iba kľúče cez Tailscale.
4. Na Mac mini: `git clone`, `uv sync --frozen`, `uv run pytest` (cieľ 1360 zelených), launchd plist (`KeepAlive`, logy do súboru s rotáciou).
5. Preniesť tajomstvá bezpečne do `.env` (600). Rovnaké hodnoty ako v Coolify, `TURSO_DATABASE_URL=file:/…/oncofiles.db`.
6. **Generálka:** skopírovať *včerajší* snapshot DB, spustiť so `SYNC_ENABLED=false` a otestovať cez Tailscale (`/health`, `/readiness`, MCP `tools/list` s bearerom, dashboard login). Žiadna verejná prevádzka.
7. Na Forpsi pripraviť (zatiaľ neaktívny) Traefik route alebo service, ktorý smeruje na Tailscale IP Mac mini (možnosť A). VPS sa musí pridať do tailnetu.
8. Aktualizovať privacy policy (nasadiť až s prepnutím).

**Fáza 1: prepnutie (odhad výpadku 10–20 min, mimo nočného okna 23:00 UTC)**
1. Ohlásiť krátku údržbu používateľom.
2. V Coolify **zastaviť** aplikáciu (nie zmazať). Kontajner, image aj dáta ostávajú.
3. Skopírovať `oncofiles.db` (+ checkpoint WAL) z VPS na Mac mini cez Tailscale (`scp`/`rsync`). Overiť `integrity_check` a počty riadkov na oboch stranách.
4. Spustiť app na Mac mini (`SYNC_ENABLED=true`, `GOOGLE_INTEGRATION_ENABLED=false` ako dnes).
5. Prepnúť Traefik na VPS na upstream Mac mini (možnosť A). DNS ostáva.
6. Smoke test zvonka: `/health`, `/readiness`, `/api/stats` (344 dokumentov), MCP konektor v claude.ai (existujúce pripojenie bez re-auth), dashboard login, prípadne oncoteam.

**Fáza 2: overovanie (2–4 týždne)**
- Sledovať `/health` (externý uptime monitor), nočný pipeline, `sync_watchdog` e-maily, pamäť, reštart po páde (`kill` test), reštart Macu s `fdesetup authrestart`, simulovaný výpadok internetu.
- Denný offsite snapshot DB z Mac mini.
- Až potom rozhodnúť o odchode z Forpsi a prípadne o presune DNS na Cloudflare (možnosť B). Inak môže VPS ostať ako lacný TLS front.

**Návrat (rollback)**
- Návrat smeruje na **Forpsi/Coolify**, nie na Railway: Railway prostredie od júna nebeží a v CLI nie je prihlásenie. Obnoviť Railway by bola nová migrácia (Turso je zrušené). Ak má Railway naozaj slúžiť ako záloha, treba ho najprv overiť v dashboarde (otvorená otázka 1).
- Postup: zastaviť app na Mac mini → skopírovať **aktuálnu** DB z Mac mini späť na VPS (aby sa nestratili zápisy) → v Coolify **Start** (env sa nemenil, takže build sa preskočí) → Traefik upstream späť na lokálny kontajner. Odhad: **5–15 min**.
- Ak Mac mini nie je dostupný (výpadok prúdu): spustiť Coolify app s poslednou DB, ktorá je na VPS. Zápisy od prepnutia sa stratia, preto je dôležitá denná (ideálne hodinová) replika DB z Mac mini späť na VPS počas fázy 2.
- **Nikdy nespúšťať obe inštancie so zapnutým schedulerom naraz** (bod 6).

---

## (f) Sprevádzkovanie na Mac mini: verejné zhrnutie

Konkrétna domáca konfigurácia (sieť, adresy, zariadenia, disky, názvy tajomstiev, prevádzkový log) je zámerne **mimo repa** v súkromnom prevádzkovom manuáli. Tu je len všeobecný postup, ktorý sa dá zverejniť.

### Cieľový stav
- `https://oncofiles.com` beží z domáceho Mac mini: Coolify v Linux VM (Colima), build Railpack, Traefik s Let's Encrypt. Bez Forpsi a bez Cloudflare.
- Z internetu sú dostupné len TCP 80 a 443. Panel a API Coolify, SSH a ostatné služby sú dostupné len cez Tailscale.
- Google OAuth v dashboarde (nový GCP projekt, consent screen External, In production, neoverený), MCP konektor na `/mcp`.

### Rozhodnutia (Peter, 2026-11-05/06)
- **R1. Dáta z Forpsi: začať načisto.** Prod DB z Forpsi sa nepresúva. Používatelia si konektor v claude.ai pridajú znova a Drive priečinok pripoja znova cez Google OAuth. Pred zastavením Forpsi sa prod DB **archivuje** (šifrovaná kópia, nie zmazanie). Prepnutie DNS preto nemá split-brain riziko.
- **R3. Google OAuth: External, In production, neoverené.** Varovanie „unverified app“ a strop 100 používateľov akceptujeme. Servisný účet sa nepoužíva, Drive ide len cez OAuth vo frontende.
- **R4. Zálohy:** šifrované `restic` snapshoty lokálne aj mimo domu. Odložené, kým na Mac mini nebežia reálni používatelia.

### DNS a TLS
- Pri prepnutí sa vo Websupporte menia len A záznamy `@` a `www`. MX, SPF, DMARC, `mail`, `autodiscover` a wildcard ostávajú. AAAA sa nepridáva.
- Deň vopred znížiť TTL na 300 s. Overiť `dig +short oncofiles.com @ns1.websupport.sk`.
- Certifikát Let's Encrypt vydá Traefik (HTTP-01) až po zmene DNS. Kvôli HSTS bude 1–2 minúty neplatný certifikát (bez výpadku by to šlo cez DNS-01).

### Google Cloud
- OAuth klient typu Web application, redirect `https://oncofiles.com/oauth/callback`, origin `https://oncofiles.com`. Consent screen a OAuth klient sa robia len v konzole (Google Auth Platform), nemajú verejné API.
- Scopes: `drive` a `gmail.readonly` (restricted), `calendar.readonly` (sensitive). V stave Testing by refresh tokeny expirovali po 7 dňoch, preto In production.
- `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI`, `GOOGLE_INTEGRATION_ENABLED=true` nastavené v Coolify ako runtime premenné (nikdy buildtime, pozri #550).

### Prepnutie (checklist)
1. DB na Mac mini ostáva čistá (R1). Prod DB z Forpsi sa pred zastavením archivuje.
2. Coolify: FQDN `https://oncofiles.com,https://www.oncofiles.com`, Redeploy.
3. Presmerovanie 80/443 overené zvonka cez verejnú IP s hlavičkou `Host: oncofiles.com`.
4. Websupport: A `@` a `www` na statickú IP.
5. Overiť certifikát (`curl -sI https://oncofiles.com/health`, issuer Let's Encrypt).
6. Testy: `/health`, `/readiness`, dashboard → Google prihlásenie → priečinok z Drive, claude.ai → konektor `https://oncofiles.com/mcp`.
7. Forpsi zastaviť až po Petrovom súhlase (Coolify Stop, nie Delete).

### CI/CD
- `.github/workflows/ci.yml` má job `deploy`: po zelených `lint` + `test`, len pri pushi do `main` a len keď je repo variable `DEPLOY_ENABLED=true`.
- Runner sa pripojí do tailnetu ako efemérny uzol `tag:ci` (`tailscale/github-action` pripnutý na SHA, OAuth client). Tailnet ACL povoľuje `tag:ci` len na port Coolify API a port 80 VM, nič iné (overené testami v politike).
- Job zavolá `POST /api/v1/deploy?uuid=…` (od Coolify 4.6 je to POST, GET vracia 405), počká na `finished` a urobí smoke test `/health`.
- Coolify token pre CI má len právo `deploy`.
- Repo je verejné, a teda aj logy Actions. Preto sú aj URL a UUID uložené ako secrets: GitHub ich v logoch maskuje, kým hodnoty z `vars` by sa vypísali čitateľne.
- Do `main` smie pushovať len vlastník repa (ruleset). Workflowy majú token len na čítanie.

### Neskôr
- Lokálny model (LM Studio, Anthropic-kompatibilné API) namiesto Anthropic API, až po spustení na `oncofiles.com`.
