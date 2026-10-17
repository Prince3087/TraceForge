"""Pydantic models mirroring ``data/schema.json`` plus API request/response envelopes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


# --------------------------------------------------------------------------- #
# Dossier (strict mirror of schema.json -> TraceForgeIncidentSynthesis)
# --------------------------------------------------------------------------- #
class AnomalySignature(BaseModel):
    classification: str
    confidence_score: float = Field(ge=0.0, le=1.0)
    optical_delta_e: float
    vibration_drift_pct: str


class CrossSiloCorrelation(BaseModel):
    scada_state: str
    shift_log_finding: str
    aoi_visual_finding: str
    synthesis_summary: str


class GroundedSopCitation(BaseModel):
    sop_document_id: str
    section: str
    page_number: int
    verbatim_quote: str
    citation_valid: bool


class CostOfInaction(BaseModel):
    risk_window_hours: int
    projected_scrap_cost_usd: float
    downtime_loss_usd: float
    total_financial_exposure_usd: float


class DecisionOption(BaseModel):
    option_id: Literal["OPTION_A", "OPTION_B"]
    title: str
    action_type: Literal["IMMEDIATE_CALIBRATION_PAUSE", "DERATED_SPEED_RUN"]
    line_pause_min: int
    projected_cost_usd: float
    scrap_avoided_units: int


class SafetyGate(BaseModel):
    requires_human_badge_auth: bool
    autonomous_plc_override_blocked: bool
    status: Literal["PASSED_CONFIRMATION_REQUIRED", "REJECTED_UNGROUNDED"]


class IncidentDossier(BaseModel):
    incident_id: str
    timestamp: str
    line_id: str
    component_id: str
    anomaly_signature: AnomalySignature
    cross_silo_correlation: CrossSiloCorrelation
    grounded_sop_citation: GroundedSopCitation
    cost_of_inaction: CostOfInaction
    decision_options: list[DecisionOption] = Field(min_length=2, max_length=2)
    safety_gate: SafetyGate


# Name used in the audit brief for the strict, server-side model.
IncidentSynthesis = IncidentDossier


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
class LineParams(BaseModel):
    """Cost-model inputs. Defaults are the Line 4B figures."""

    run_rate_units_per_hr: float = Field(default=240.0, gt=0)
    unit_scrap_cost_usd: float = Field(default=30.0, ge=0)
    risk_horizon_hrs: float = Field(default=48.0, gt=0)


class InvestigateRequest(BaseModel):
    aoi_asset: str = Field(
        default="aoi_defective_sample.svg",
        description="Filename of an AOI image under data/ (see GET /incidents/demo-assets).",
    )
    log_id: str | None = Field(
        default=None, description="Shift-log entry to use. Defaults to the most recent entry."
    )
    line_params: LineParams = Field(default_factory=LineParams)
    gemini_model: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,79}$",
        description="Per-request Gemini model override (see GET /models). Defaults to the server model.",
    )
    gemini_api_key: SecretStr | None = Field(
        default=None,
        max_length=256,
        repr=False,
        description="Per-request Gemini Developer API key. Used for this call only; never stored or logged.",
    )


class SignoffRequest(BaseModel):
    operator_badge_id: str = Field(min_length=1, max_length=64)
    selected_option_id: Literal["OPTION_A", "OPTION_B"]
    dossier: IncidentDossier
    signature: str = Field(min_length=64, max_length=64, description="Signature returned by /investigate.")
    comment: str | None = Field(default=None, max_length=500)


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #
class GateCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class InvestigationMeta(BaseModel):
    engine: Literal["gemini", "offline-rules", "offline_deterministic_fallback"]
    model: str | None = None
    attempts: int = 0
    aoi_asset: str
    log_id: str
    warnings: list[str] = Field(default_factory=list)
    overrides: list[str] = Field(
        default_factory=list,
        description="Model-supplied fields that were replaced by deterministic backend values.",
    )
    cost_formula: dict[str, Any] = Field(default_factory=dict)


class InvestigateResponse(BaseModel):
    dossier: IncidentDossier
    signature: str = Field(description="HMAC-SHA256 over the canonical dossier JSON; required by /signoff.")
    gate_checks: list[GateCheck]
    meta: InvestigationMeta


class LedgerRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    sequence: int
    recorded_at: str
    event_type: str
    incident_id: str
    operator_badge_id: str
    selected_option_id: str
    dossier_sha256: str
    payload: dict[str, Any]
    prev_hash: str
    record_hash: str


class SignoffResponse(BaseModel):
    status: Literal["SEALED"]
    ledger_record: LedgerRecord
    plc_command_issued: bool = False
    message: str


class DemoAssets(BaseModel):
    aoi_images: list[dict[str, Any]]
    shift_logs: list[dict[str, Any]]
    sop_document_id: str
    sop_files: list[str]
    line_defaults: LineParams


class ErrorResponse(BaseModel):
    error: str
    detail: str
    hint: str | None = None
