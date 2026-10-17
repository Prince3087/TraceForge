"""TraceForge FastAPI application.

Endpoints
    GET  /health                  liveness + dependency status
    GET  /incidents/demo-assets   AOI images + raw shift logs for the cockpit
    POST /incidents/investigate   multi-signal evidence investigation (Gemini + deterministic gate)
    POST /incidents/signoff       human-in-the-loop sign-off -> SHA-256 hash-chained audit ledger
    GET  /ledger/verify           recompute and verify the whole hash chain
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import GEMINI_MODEL_CATALOG, get_settings
from backend.models.incident import (
    DemoAssets,
    InvestigateRequest,
    InvestigateResponse,
    LineParams,
    SignoffRequest,
    SignoffResponse,
)
from backend.services import investigator, ledger, safety_gate
from backend.services.investigator import verify_dossier_signature
from backend.services.evidence import EvidenceError, demo_assets
from backend.services.gemini_agent import AgentFailure, AgentNotConfigured
from backend.services.ledger import LedgerIntegrityError, LedgerPersistenceError
from backend.services.sop_grounding import SopError, load_sop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("traceforge.api")

BADGE_RE = re.compile(r"^(TECH|ENG|MGR)_[0-9]{4}_[A-Z]+$")

_remote_store = None


def get_remote_store():
    """Lazily-built GCS store (None when LEDGER_BUCKET_NAME is unset). Overridable in tests."""
    global _remote_store
    if _remote_store is None:
        _remote_store = ledger.make_remote_store(get_settings()) or False
    return _remote_store or None


def _restore_ledger() -> None:
    s = get_settings()
    try:
        n = ledger.hydrate_from_remote(s.ledger_path, get_remote_store())
        if n:
            log.info("ledger restored from gs://%s (%d records)", s.ledger_bucket_name, n)
    except (LedgerPersistenceError, LedgerIntegrityError) as exc:
        # Do not crash the service; sign-off will refuse (fail closed) until the bucket is readable.
        log.error("ledger restore failed at startup: %s", exc)
    if s.signing_key_ephemeral:
        log.warning("TRACEFORGE_SIGNING_KEY not set - using a random per-process signing key.")


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _restore_ledger()
    yield


app = FastAPI(
    title="TraceForge",
    version="1.0.0",
    description="Evidence compiler for ambiguous factory incidents (SOP-SMT-4402).",
    lifespan=_lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(get_settings().cors_origins),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _sop():
    settings = get_settings()
    try:
        return load_sop(str(settings.sop_md_path))
    except SopError as exc:
        raise HTTPException(
            status_code=500, detail={"error": "SOP_UNAVAILABLE", "detail": str(exc)}
        ) from exc


def _err(status: int, error: str, detail: str, hint: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": error, "detail": detail, "hint": hint})


# --------------------------------------------------------------------------- #
# Error handlers -> clean, uniform error bodies
# --------------------------------------------------------------------------- #
@app.exception_handler(HTTPException)
async def _http_exc(_: Request, exc: HTTPException):
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return _err(exc.status_code, "HTTP_ERROR", str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_exc(_: Request, exc: RequestValidationError):
    msgs = [f"{'.'.join(str(p) for p in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return _err(422, "INVALID_REQUEST", "; ".join(msgs))


@app.exception_handler(EvidenceError)
async def _evidence_exc(_: Request, exc: EvidenceError):
    return _err(400, "INVALID_EVIDENCE", str(exc))


@app.exception_handler(AgentNotConfigured)
async def _not_configured_exc(_: Request, exc: AgentNotConfigured):
    return _err(503, "GEMINI_NOT_CONFIGURED", str(exc))


@app.exception_handler(LedgerPersistenceError)
async def _ledger_persist_exc(_: Request, exc: LedgerPersistenceError):
    log.error("ledger persistence failure: %s", exc)
    return _err(503, "LEDGER_PERSISTENCE_FAILED", str(exc), "Nothing was recorded. Retry the sign-off.")


@app.exception_handler(LedgerIntegrityError)
async def _ledger_integrity_exc(_: Request, exc: LedgerIntegrityError):
    log.error("ledger integrity failure: %s", exc)
    return _err(500, "LEDGER_INTEGRITY_FAILURE", str(exc))


@app.exception_handler(AgentFailure)
async def _agent_exc(_: Request, exc: AgentFailure):
    log.error("agent failure after %d attempt(s): %s", exc.attempts, exc)
    return _err(502, "GEMINI_SYNTHESIS_FAILED", str(exc), exc.hint)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    s = get_settings()
    sop_ok, sop_detail = True, "loaded"
    try:
        load_sop(str(s.sop_md_path)).execution_options()
    except SopError as exc:
        sop_ok, sop_detail = False, str(exc)
    return {
        "status": "ok" if sop_ok else "degraded",
        "service": "traceforge",
        "version": app.version,
        "engine": "offline-rules" if s.offline_mode else "gemini",
        "gemini": {
            "configured": s.gemini_configured,
            "backend": "vertex-ai" if s.use_vertex else "gemini-api",
            "model": s.gemini_model,
            "location": s.gcp_location if s.use_vertex else None,
        },
        "sop": {"ok": sop_ok, "detail": sop_detail},
        "ledger": {
            **ledger.verify_chain(s.ledger_path),
            "remote_bucket": s.ledger_bucket_name,
            "durable": bool(s.ledger_bucket_name),
        },
        "signing_key_persistent": not s.signing_key_ephemeral,
    }


@app.get("/models")
def list_models() -> dict:
    """Model choices for the cockpit dropdown plus whether the server already holds credentials."""
    s = get_settings()
    return {
        "default_model": s.gemini_model,
        "models": list(GEMINI_MODEL_CATALOG),
        "server_key_configured": s.gemini_configured,
        "offline_mode": s.offline_mode,
    }


@app.get("/incidents/demo-assets", response_model=DemoAssets)
def get_demo_assets() -> DemoAssets:
    s = get_settings()
    sop = _sop()
    assets = demo_assets(s)
    return DemoAssets(
        **assets,
        sop_document_id=sop.document_id,
        sop_files=sorted(p.name for p in s.data_dir.glob("SOP-SMT-4402*") if p.is_file()),
        line_defaults=LineParams(
            run_rate_units_per_hr=s.run_rate_units_per_hr,
            unit_scrap_cost_usd=s.unit_scrap_cost_usd,
            risk_horizon_hrs=s.risk_horizon_hrs,
        ),
    )


@app.post("/incidents/investigate", response_model=InvestigateResponse)
def investigate(req: InvestigateRequest) -> InvestigateResponse:
    # Sync endpoint on purpose: FastAPI runs it in a worker thread, so the blocking Gemini call
    # never stalls the event loop.
    return investigator.investigate(get_settings(), _sop(), req)


@app.post("/incidents/signoff", response_model=SignoffResponse)
def signoff(req: SignoffRequest):
    s = get_settings()
    sop = _sop()

    badge = req.operator_badge_id.strip()
    if not BADGE_RE.match(badge):
        return _err(
            422,
            "INVALID_BADGE",
            "Operator badge ID must match (TECH|ENG|MGR)_NNNN_NAME, e.g. TECH_8412_DESHMUKH.",
        )

    # Integrity: only a dossier issued by /incidents/investigate (and untouched since) may be sealed.
    if not verify_dossier_signature(req.dossier.model_dump(), req.signature, s.signing_key):
        log.warning("signoff refused: bad dossier signature (badge=%s)", badge)
        return _err(
            403,
            "DOSSIER_SIGNATURE_INVALID",
            "Tampered or unverified dossier. Signature verification failed.",
            "Run /incidents/investigate and sign off the exact dossier and signature it returned.",
        )

    # Never trust the client: re-run the full safety gate server-side on the submitted dossier.
    checks = safety_gate.evaluate(req.dossier, sop, s)
    failed = safety_gate.failed(checks)
    if failed or req.dossier.safety_gate.status != "PASSED_CONFIRMATION_REQUIRED":
        reasons = (
            "; ".join(f"{c.name}: {c.detail}" for c in failed)
            or "dossier status is not PASSED_CONFIRMATION_REQUIRED"
        )
        return _err(
            409,
            "SAFETY_GATE_REJECTED",
            f"Sign-off refused. {reasons}",
            "Only dossiers that pass confidence >= 90%, verbatim SOP citation and PLC-override "
            "checks can be sealed.",
        )

    record = ledger.append_signoff(
        s.ledger_path,
        incident_id=req.dossier.incident_id,
        operator_badge_id=badge,
        selected_option_id=req.selected_option_id,
        dossier=req.dossier.model_dump(),
        gate_checks=[c.model_dump() for c in checks],
        comment=req.comment,
        dossier_signature=req.signature,
        remote=get_remote_store(),
    )
    log.info("sealed incident=%s seq=%s badge=%s", record["incident_id"], record["sequence"], badge)
    return SignoffResponse(
        status="SEALED",
        ledger_record=record,
        plc_command_issued=False,
        message=(
            f"Decision {req.selected_option_id} sealed in audit ledger (seq {record['sequence']}). "
            "No PLC command was issued - execute the action at the line per SOP-SMT-4402 §4.2.2."
        ),
    )


@app.get("/ledger/verify")
def verify_ledger() -> dict:
    return ledger.verify_chain(get_settings().ledger_path)
