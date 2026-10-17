"""Investigation orchestrator: evidence -> authoritative values -> agent -> override -> safety gate.

The LLM is treated as an untrusted narrator. After it replies, every identifier, number and
remediation string is replaced with the deterministic value computed here; any field the model got
wrong is reported in ``meta.overrides`` so reviewers can see model drift. Confidence and the SOP
citation are NOT overridden - they are verified by the safety gate and fail closed.
"""

from __future__ import annotations

import dataclasses
import hmac
import hashlib
import json
import logging
from typing import Any

from backend.config import Settings
from backend.models.incident import (
    IncidentDossier,
    InvestigateRequest,
    InvestigateResponse,
    InvestigationMeta,
)
from backend.services import cost_model, safety_gate
from backend.services.evidence import (
    extract_log_facts,
    load_aoi,
    make_incident_id,
)
from backend.services.gemini_agent import (
    AgentFailure,
    AgentNotConfigured,
    AgentResult,
    generate_dossier,
    offline_rules_engine,
)
from backend.services.sop_grounding import SopDocument

log = logging.getLogger("traceforge.investigator")


# --------------------------------------------------------------------------- #
# Dossier integrity (HMAC-SHA256)
# --------------------------------------------------------------------------- #
def _canonical(dossier_dict: dict[str, Any]) -> bytes:
    return json.dumps(
        dossier_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sign_dossier(dossier_dict: dict[str, Any], secret_key: str) -> str:
    """HMAC-SHA256 hex digest over the canonical (sorted-key, compact) JSON of the dossier."""
    return hmac.new(secret_key.encode("utf-8"), _canonical(dossier_dict), hashlib.sha256).hexdigest()


def verify_dossier_signature(dossier_dict: dict[str, Any], signature: str, secret_key: str) -> bool:
    """Constant-time check that the dossier is byte-for-byte what /investigate issued."""
    if not isinstance(signature, str) or not signature:
        return False
    return hmac.compare_digest(sign_dossier(dossier_dict, secret_key), signature)


def _authoritative(settings, sop, aoi, logf, req) -> tuple[dict[str, Any], cost_model.CostInputs, dict[str, Any]]:
    inputs = cost_model.CostInputs.from_params(
        req.line_params,
        defect_rate_drift=logf.drift_fraction,
        downtime_cost_per_hr_usd=settings.downtime_cost_per_hr_usd,
        seizure_rebuild_hours=settings.seizure_rebuild_hours,
        planned_pause_cost_per_hr_usd=settings.planned_pause_cost_per_hr_usd,
        immediate_batch_window_hrs=settings.immediate_batch_window_hrs,
    )
    sop_options = sop.execution_options()
    options, option_detail = cost_model.compute_decision_options(inputs, sop_options)
    coi = cost_model.compute_cost_of_inaction(inputs)
    fields = {
        "incident_id": make_incident_id(logf),
        "timestamp": logf.timestamp,
        "line_id": logf.line_id,
        "component_id": logf.component_id,
        "cost_of_inaction": coi.model_dump(),
        "decision_options": [o.model_dump() for o in options],
    }
    authoritative = {
        "dossier_fields": fields,
        "optical_delta_e": aoi.delta_e_max,
        "vibration_drift_pct": logf.drift_pct_str,
        "sop_threshold_delta_e": settings.delta_e_threshold,
        "sop_section_4_2_page": sop.page_of("4.2"),
    }
    metrics = cost_model.compute_cost_metrics(inputs, sop_options["OPTION_A"].pause_min)
    return authoritative, inputs, cost_model.formula_breakdown(inputs, option_detail, metrics)


def _apply_overrides(
    dossier: IncidentDossier, authoritative: dict[str, Any], aoi_delta_e: float | None, drift: str
) -> tuple[IncidentDossier, list[str]]:
    data = dossier.model_dump()
    overrides: list[str] = []

    for key, value in authoritative["dossier_fields"].items():
        if data[key] != value:
            overrides.append(key)
            data[key] = value

    if aoi_delta_e is not None and data["anomaly_signature"]["optical_delta_e"] != aoi_delta_e:
        overrides.append("anomaly_signature.optical_delta_e")
        data["anomaly_signature"]["optical_delta_e"] = aoi_delta_e
    if data["anomaly_signature"]["vibration_drift_pct"] != drift:
        overrides.append("anomaly_signature.vibration_drift_pct")
        data["anomaly_signature"]["vibration_drift_pct"] = drift

    return IncidentDossier.model_validate(data), overrides


def _synthesize(settings, sop, aoi, logf, authoritative, client) -> AgentResult:
    """Gemini first; deterministic fallback ONLY when Gemini was unreachable (429/5xx/network).

    Auth errors, unknown models and schema/grounding failures are real defects and still raise -
    masking them with the fallback would hide a broken deployment.
    """
    if settings.offline_mode and client is None:
        return offline_rules_engine(settings, sop, aoi, logf, authoritative)
    try:
        return generate_dossier(settings, sop, aoi, logf, authoritative, client=client)
    except AgentFailure as exc:
        if not (exc.transient and settings.allow_offline_fallback):
            raise
        log.error("Gemini unavailable after %d attempt(s); using deterministic fallback: %s", exc.attempts, exc)
        fb = offline_rules_engine(settings, sop, aoi, logf, authoritative)
        fb.engine = "offline_deterministic_fallback"
        fb.attempts = exc.attempts
        fb.warnings = [
            "GEMINI UNAVAILABLE - DETERMINISTIC FALLBACK ENGINE USED. No LLM reasoning was applied; "
            f"narrative and confidence come from fixed rules over verified evidence. Cause: {exc}"
        ]
        return fb


def _with_request_overrides(settings: Settings, req: InvestigateRequest) -> Settings:
    """Per-request model / API key chosen in the cockpit. Applied to a copy; the key is never stored."""
    changes: dict[str, Any] = {}
    if req.gemini_model:
        changes["gemini_model"] = req.gemini_model
    if req.gemini_api_key and req.gemini_api_key.get_secret_value().strip():
        changes["gemini_api_key"] = req.gemini_api_key.get_secret_value().strip()
        changes["offline_mode"] = False  # an explicit key means the user wants the real model
    return dataclasses.replace(settings, **changes) if changes else settings


def investigate(
    settings: Settings,
    sop: SopDocument,
    req: InvestigateRequest,
    *,
    client: Any | None = None,
) -> InvestigateResponse:
    settings = _with_request_overrides(settings, req)
    aoi = load_aoi(settings, req.aoi_asset)
    logf = extract_log_facts(settings, req.log_id)
    authoritative, inputs, breakdown = _authoritative(settings, sop, aoi, logf, req)

    result = _synthesize(settings, sop, aoi, logf, authoritative, client)

    dossier, overrides = _apply_overrides(
        result.dossier, authoritative, aoi.delta_e_max, logf.drift_pct_str
    )

    # Deterministic gate: evaluated on the verified dossier; its verdict replaces the model's claim.
    checks = safety_gate.evaluate(dossier, sop, settings)
    gate = safety_gate.seal_gate(checks)
    cit_check = next(c for c in checks if c.name == "verbatim_sop_citation")
    data = dossier.model_dump()
    data["safety_gate"] = gate.model_dump()
    data["grounded_sop_citation"]["citation_valid"] = cit_check.passed
    dossier = IncidentDossier.model_validate(data)

    warnings = list(result.warnings) + list(logf.data_quality_warnings)
    if gate.status == "REJECTED_UNGROUNDED":
        warnings.append(
            "SAFETY GATE REJECTED: "
            + "; ".join(f"{c.name} ({c.detail})" for c in safety_gate.failed(checks))
        )

    meta = InvestigationMeta(
        engine=result.engine,  # type: ignore[arg-type]
        model=result.model,
        attempts=result.attempts,
        aoi_asset=aoi.filename,
        log_id=logf.log_id,
        warnings=warnings,
        overrides=overrides,
        cost_formula=breakdown,
    )
    signature = sign_dossier(dossier.model_dump(), settings.signing_key)
    if settings.signing_key_ephemeral:
        meta.warnings.append(
            "TRACEFORGE_SIGNING_KEY is not set: using a random per-process signing key. Signatures "
            "become invalid on restart and across instances - set a persistent key in production."
        )
    return InvestigateResponse(dossier=dossier, signature=signature, gate_checks=checks, meta=meta)


__all__ = ["investigate", "sign_dossier", "verify_dossier_signature", "AgentNotConfigured"]
