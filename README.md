# TraceForge — evidence compiler for ambiguous factory incidents
Team **Interrupt 0xFF** · Prince Raj & Maitri Shekhda

TraceForge fuses three isolated silos — SCADA telemetry, free-text shift logs and an AOI camera frame — into one
SOP-grounded incident dossier (`data/schema.json`), prices the cost of inaction deterministically, and requires a
badge-authenticated human to approve before a SHA-256 hash-chained audit record is written.

```
Streamlit cockpit ──HTTP──> FastAPI ──> evidence (SVG/logs) ─┐
 frontend/app.py            backend/    SOP grounding (§4.2)  ├─> Gemini (schema-constrained) ─> overrides ─> Safety Gate
                                         cost model           ┘                                               │
                                         ledger.jsonl  <── /incidents/signoff (re-runs the gate) <───────────┘
```

## Trust model (why the output can be relied on)
| Concern | Mechanism |
|---|---|
| Hallucinated instructions | Citation must be a **verbatim** substring of SOP §4.2 on the cited page (`sop_grounding.py`). Options A/B titles must be verbatim §4.2.2 text; pause/derate parameters (15 min, 25 %, 4 h) are parsed from the SOP, not hard-coded. |
| Malformed LLM output | `response_mime_type=application/json` + `response_schema` from `schema.json`, then jsonschema + Pydantic validation, retry with error feedback, clean `502 GEMINI_SYNTHESIS_FAILED` on exhaustion. |
| Wrong numbers | Incident id, Delta-E, drift, cost-of-inaction and option economics are **overwritten** with deterministic values; overrides are reported in `meta.overrides`. |
| Forged sign-off | `/incidents/investigate` returns `signature` = HMAC-SHA256 over the canonical dossier JSON (`TRACEFORGE_SIGNING_KEY`). `/incidents/signoff` recomputes it and answers **403** on any mismatch, so a hand-built dossier can never be sealed. Badge IDs must match `^(TECH|ENG|MGR)_[0-9]{4}_[A-Z]+$` (422 otherwise). |
| Self-approval | Model's own `safety_gate` is discarded. Gate = confidence ≥ 90 % ∧ verbatim citation ∧ Delta-E > 3.0 ∧ options trace to SOP ∧ PLC override blocked. `/incidents/signoff` **re-runs the gate server-side**. |
| PLC safety | No PLC/SCADA client exists in the codebase; `plc_command_issued` is always `false`. |
| Audit tampering | `data/audit_ledger.jsonl` is hash-chained (`prev_hash` → `record_hash`), `fcntl`-locked appends, `GET /ledger/verify` recomputes the chain. |
| Ephemeral disk | With `LEDGER_BUCKET_NAME` set, each record is written **first** to `gs://<bucket>/ledger/<record_sha256>.json` (create-only), then to the local file; a bucket failure returns 503 and records nothing (fail closed). An empty local file is rebuilt from the bucket at startup and the chain is re-verified before it is trusted. |

Cost model — three different numbers, deliberately never merged:

| Metric | Value | Formula |
|---|---|---|
| Immediate batch at risk | **$3,600** (120 units) | `RunRate 240 × 0.5 h window × $30` — what a prompt Option A pause saves right now |
| 48 h inaction exposure | **$62,899.20** | `ScrapRisk = RunRate × DefectRateDrift × UnitScrapCost × TimeToNextShift` = `240 × 0.182 × $30 × 48` |
| Unplanned seizure downtime | **$42,500** | `8.5 h × $5,000/h` |

Option A costs **$750** (15 min at the *planned-stop* rate of $3,000/h; an unplanned seizure is the pricier $5,000/h).
`DefectRateDrift` = the +18.2 % rolling-2h drift in the shift log. These are **assumptions, not plant data**:
`DOWNTIME_COST_PER_HR_USD` (5000), `SEIZURE_REBUILD_HOURS` (8.5), `PLANNED_PAUSE_COST_PER_HR_USD` (3000),
`IMMEDIATE_BATCH_WINDOW_HRS` (0.5). Replace them with real figures. The `$3,600`/`120 units` window is a modelling choice:
it is the schema example's value, reproduced as `RunRate × window`.

## Run locally
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt          # CairoSVG needs libcairo: `brew install cairo` (macOS)
export TRACEFORGE_SIGNING_KEY=$(openssl rand -hex 32)   # else a random per-process key is used (signatures die on restart)

# Option 1 — real Gemini on Vertex AI (uses Application Default Credentials)
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT=<your-project> GOOGLE_CLOUD_LOCATION=us-central1
# Option 2 — Gemini Developer API key instead of Vertex:  export GEMINI_API_KEY=...
# Option 3 — no credentials, clearly-labelled rules engine (NO LLM):  export TRACEFORGE_OFFLINE_MODE=true
# Provider = whichever is set: GEMINI_API_KEY (AI Studio) wins over GOOGLE_CLOUD_PROJECT (Vertex AI, ADC).
# If Gemini is unreachable (429 / 5xx / network) the response is produced by the deterministic fallback and tagged
# engine = "offline_deterministic_fallback" with a visible warning; auth / bad-model / bad-output errors are NOT masked.
# Durable ledger (optional): export LEDGER_BUCKET_NAME=<bucket>   (needs roles/storage.objectCreator + objectViewer)

uvicorn backend.main:app --port 8000 --reload          # terminal 1  (docs: http://localhost:8000/docs)
streamlit run frontend/app.py                          # terminal 2  (http://localhost:8501)
pytest -q                                              # 115 tests, no network needed
```
**Model and API key from the UI:** the cockpit sidebar (*Gemini engine*) has a model dropdown (default: Gemini 3.8 Flash; lighter options such as Gemini 3.1 Flash-Lite, or any custom model ID) and a masked *Gemini API key* field. Both are sent with each investigation request, applied to that request only, and never stored, logged or echoed. Leave the key blank to use the server's `GEMINI_API_KEY` / Vertex AI credentials. The dropdown list comes from `GET /models`.

Server default model: `GEMINI_MODEL` (default `gemini-3.8-flash`). `gemini-1.5-*` has been retired on Vertex AI; set it only if your project still has access.

## Run in Docker (same image Cloud Run uses)
```bash
docker build -t traceforge .
docker run --rm -p 8080:8080 -e TRACEFORGE_OFFLINE_MODE=true traceforge     # cockpit on http://localhost:8080
# with Vertex AI: mount ADC and pass GOOGLE_CLOUD_PROJECT
#   -v $HOME/.config/gcloud:/home/traceforge/.config/gcloud:ro -e GOOGLE_CLOUD_PROJECT=<id>
```
One container: Streamlit on `$PORT`, FastAPI on internal `127.0.0.1:8000` (`deploy/entrypoint.sh`; `TRACEFORGE_MODE=api|ui|all`).

## Deploy to Cloud Run (Artifact Registry)
```bash
PROJECT_ID=<id> LEDGER_BUCKET=<globally-unique-bucket> ./deploy.sh
```
`deploy.sh` is idempotent. It enables missing APIs (`run`, `aiplatform`, `artifactregistry`, `cloudbuild`, `secretmanager`, `storage`),
creates the `traceforge-repo` Artifact Registry repo, generates the HMAC key into Secret Manager (`traceforge-signing-key`),
grants the runtime service account Vertex AI / secret / bucket access, builds with `cloudbuild.yaml`
(`<region>-docker.pkg.dev/<project>/traceforge-repo/traceforge:<sha>`), then runs `gcloud run deploy traceforge-engine --allow-unauthenticated`.
Omit `LEDGER_BUCKET` and the ledger is local-only (lost on restart) — the script says so.
`--max-instances 1` keeps the hash chain single-writer. **Not yet exercised against a real GCP project.**

## API
`GET /health` · `GET /incidents/demo-assets` · `POST /incidents/investigate` → `{dossier, signature, gate_checks, meta}` · `POST /incidents/signoff` (needs `signature`; 403 forged, 409 gate-rejected, 422 bad badge, 503 ledger-store down) · `GET /ledger/verify`

## Known data issue surfaced, not hidden
`sample_shift_logs.json` raw text says **"1.8 Hz"** while the normalised field and SOP Table 2.1 say **1.8 kHz**. TraceForge flags this
as a data-quality warning in the dossier instead of silently correcting it.
## Known page-number discrepancy
Citations use the SOP's own logical numbering (`<!-- PAGE BREAK: Page 4 of 4 -->` in the `.md`, which places §4.2 on **page 4**, as the
brief requires). The rendered `SOP-SMT-4402_HighSpeed_Feeder.pdf` has 5 physical pages and §4.2 text actually falls on physical page 3
(pages 4–5 are blank). Regenerate the PDF with explicit page breaks if auditors will check page numbers against the PDF.
The gate verifies against the `.md` markers, so a quote cited on any other page is rejected.
