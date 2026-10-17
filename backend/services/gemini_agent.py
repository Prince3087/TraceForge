"""Gemini / Vertex AI reasoning engine.

Pipeline for one investigation:

  1. Build a multimodal prompt: rasterised AOI frame (PNG) + shift-log JSON + SCADA facts +
     the *complete* SOP text with page markers.
  2. Call Gemini with ``response_mime_type="application/json"`` and ``response_schema`` derived from
     ``data/schema.json`` (constrained decoding), temperature 0.
  3. Validate the reply twice: JSON-Schema (jsonschema) and Pydantic. A reply that fails either,
     or whose SOP citation is not verbatim, is retried with the concrete validation errors fed back.
  4. After ``max_retries`` the caller gets a clean ``AgentFailure`` (HTTP 502) - never a half-parsed dossier.

The model only supplies the *narrative* (classification, confidence, cross-silo prose, citation).
All numbers and all remediation text are overridden deterministically by ``investigator.py``.

``offline_rules_engine`` is an explicit, clearly labelled non-LLM fallback for demos/tests without
credentials (TRACEFORGE_OFFLINE_MODE=true). It is never used silently.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import jsonschema
from pydantic import ValidationError

from backend.config import Settings
from backend.models.incident import IncidentDossier
from backend.services.evidence import AoiFacts, ShiftLogFacts, rasterize_svg
from backend.services.sop_grounding import REQUIRED_SECTION, SopDocument

log = logging.getLogger("traceforge.gemini")


class AgentFailure(RuntimeError):
    """Gemini could not produce a schema-valid dossier (maps to HTTP 502)."""

    def __init__(
        self, message: str, attempts: int, hint: str | None = None, transient: bool = False
    ):
        super().__init__(message)
        self.attempts = attempts
        self.hint = hint
        # True only when every attempt failed on rate-limit / 5xx / network errors, i.e. the model
        # never answered. Auth, model-not-found and schema/grounding failures are NOT transient and
        # must surface as errors instead of being masked by the offline fallback.
        self.transient = transient


class AgentNotConfigured(RuntimeError):
    """No credentials / project configured for Gemini (maps to HTTP 503)."""


@dataclass
class AgentResult:
    dossier: IncidentDossier
    engine: str  # "gemini" | "offline-rules" | "offline_deterministic_fallback"
    model: str | None
    attempts: int
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Schema handling
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _load_schema(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# Keywords Gemini's response_schema rejects with HTTP 400 InvalidArgument (plus annotation-only
# keys that carry no constraint). Server-side validation still uses the FULL schema + Pydantic, so
# stripping these never weakens what TraceForge accepts.
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {"$schema", "$id", "format", "minItems", "maxItems", "default", "example", "examples"}
)


def sanitize_schema_for_gemini(schema: Any) -> Any:
    """Recursively remove JSON-Schema keywords unsupported by Vertex AI / Gemini JSON mode.

    Children of ``properties`` are *property names*, not keywords: a field that happens to be
    called ``format`` or ``default`` is kept (only its sub-schema is sanitised).
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k in _UNSUPPORTED_SCHEMA_KEYS:
                continue
            if k == "properties" and isinstance(v, dict):
                out[k] = {name: sanitize_schema_for_gemini(sub) for name, sub in v.items()}
            else:
                out[k] = sanitize_schema_for_gemini(v)
        return out
    if isinstance(schema, list):
        return [sanitize_schema_for_gemini(item) for item in schema]
    return schema


def gemini_response_schema(settings: Settings) -> dict[str, Any]:
    return sanitize_schema_for_gemini(_load_schema(str(settings.schema_path)))


def validate_reply(settings: Settings, raw_text: str) -> tuple[IncidentDossier | None, list[str]]:
    """Return (dossier, []) on success or (None, [errors]) describing every problem."""
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        return None, [f"Reply is not valid JSON: {exc}"]
    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(_load_schema(str(settings.schema_path)))
    for err in sorted(validator.iter_errors(data), key=lambda e: list(e.absolute_path)):
        loc = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{loc}: {err.message}")
    if errors:
        return None, errors
    try:
        return IncidentDossier.model_validate(data), []
    except ValidationError as exc:
        return None, [f"{'/'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #
SYSTEM_INSTRUCTION = f"""You are TraceForge, an industrial evidence compiler for ambiguous factory incidents.
You fuse three isolated data silos - SCADA telemetry, free-text shift logs and an Automated Optical
Inspection (AOI) frame - into one incident dossier, grounded in the Standard Operating Procedure.

HARD RULES (violations cause the dossier to be rejected by a deterministic safety gate):
1. Use ONLY the evidence supplied in the user message. Never invent measurements, names, or procedures.
2. grounded_sop_citation.verbatim_quote MUST be copied character-for-character from the SOP text
   inside Section {REQUIRED_SECTION} (markdown symbols such as ** and > removed). Do not paraphrase,
   shorten with ellipses, or merge sentences from different sections. Prefer the sentence(s) of the
   CRITICAL COMPLIANCE DIRECTIVE (§4.2.1).
3. grounded_sop_citation.section must be "Section {REQUIRED_SECTION}"; page_number is the page on which
   Section {REQUIRED_SECTION} begins, determined from the "PAGE BREAK" markers in the SOP text.
   Set citation_valid to true only if you are certain the quote is verbatim.
4. decision_options must contain exactly OPTION_A then OPTION_B. Each `title` must be the verbatim SOP
   Section 4.2.2 text of that option. Do not propose any other remediation.
5. safety_gate.requires_human_badge_auth and autonomous_plc_override_blocked are always true. Set status
   to PASSED_CONFIRMATION_REQUIRED only if confidence_score >= 0.90 AND optical_delta_e exceeds the SOP
   threshold; otherwise REJECTED_UNGROUNDED.
6. confidence_score is your calibrated probability (0-1) that the classification is correct given ALL
   three silos. Lower it when the silos disagree or an input looks unreliable.
7. If an input contains an inconsistency (e.g. unit mismatch such as Hz vs kHz), say so inside
   shift_log_finding rather than silently correcting it.
8. Copy the AUTHORITATIVE VALUES block exactly into the matching fields (incident_id, timestamp,
   line_id, component_id, optical_delta_e, vibration_drift_pct, cost_of_inaction, decision_options
   numbers). Do not recompute them.
9. synthesis_summary must explain why the isolated systems look normal but the combined evidence does
   not, and must reference the SOP document id and the Delta-E threshold.
"""


def build_user_prompt(
    sop: SopDocument,
    aoi: AoiFacts,
    logf: ShiftLogFacts,
    authoritative: dict[str, Any],
    has_image: bool,
    trip_limit_mm_s: float,
) -> str:
    scada = {
        "scada_status": logf.scada_status,
        "alarm_tripped": logf.alarm_tripped,
        "rms_vibration_mm_s": logf.rms_mm_s,
        "emergency_trip_limit_mm_s": trip_limit_mm_s,
        "baseline_median_rms_mm_s": logf.baseline_median_rms,
        "rolling_2h_drift_percentage": logf.drift_pct_str,
        "operating_temperature_c": logf.temp_c,
        "indicated_frequency_band_khz": logf.freq_khz,
    }
    aoi_block = {
        "asset": aoi.filename,
        "camera": aoi.camera_id,
        "overlay_status": aoi.status_text,
        "delta_e_max_reported_by_camera": aoi.delta_e_max,
        "roi_label": aoi.roi_label,
        "roi_box_viewbox_units": aoi.roi,
    }
    parts = [
        "=== SILO 1: SCADA TELEMETRY ===",
        json.dumps(scada, indent=2),
        "=== SILO 2: SHIFT LOG (raw entry) ===",
        json.dumps(logf.entry, indent=2),
        "=== SILO 3: AOI FRAME METADATA ===",
        json.dumps(aoi_block, indent=2),
    ]
    if has_image:
        parts.append("The AOI frame itself is attached as an image; the cyan dashed box is the ROI.")
    else:
        parts.append(
            "The AOI frame could not be rasterised; its SVG markup follows (text overlays carry the "
            "camera annotations):\n" + aoi.svg_markup
        )
    if logf.data_quality_warnings:
        parts += ["=== KNOWN DATA-QUALITY ISSUES ===", *logf.data_quality_warnings]
    parts += [
        "=== AUTHORITATIVE VALUES (copy exactly) ===",
        json.dumps(authoritative, indent=2),
        f"=== SOP TEXT ({sop.document_id}, markdown with PAGE BREAK markers) ===",
        sop.raw_text,
        "Produce the incident dossier JSON now.",
    ]
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Gemini client
# --------------------------------------------------------------------------- #
def _is_daily_quota(message: str) -> bool:
    return "PerDay" in message or "per_day" in message.lower()


def _retry_hint(message: str) -> str:
    m = re.search(r"retry in ([0-9]+h)?([0-9]+m)?", message)
    return f" (resets in about {''.join(g for g in m.groups() if g)})" if m and any(m.groups()) else ""


def _make_client(settings: Settings):
    from google import genai
    from google.genai import types

    http = types.HttpOptions(timeout=settings.gemini_timeout_s * 1000)
    if settings.gemini_api_key:
        return genai.Client(api_key=settings.gemini_api_key, http_options=http)
    if not settings.gcp_project:
        raise AgentNotConfigured(
            "Gemini is not configured. Set GOOGLE_CLOUD_PROJECT (Vertex AI, uses Application Default "
            "Credentials) or GEMINI_API_KEY - or TRACEFORGE_OFFLINE_MODE=true for the labelled "
            "rules-engine fallback."
        )
    return genai.Client(
        vertexai=True, project=settings.gcp_project, location=settings.gcp_location, http_options=http
    )


def generate_dossier(
    settings: Settings,
    sop: SopDocument,
    aoi: AoiFacts,
    logf: ShiftLogFacts,
    authoritative: dict[str, Any],
    client: Any | None = None,
) -> AgentResult:
    """Call Gemini with structured output, validating and retrying until schema- and citation-valid.

    ``client`` may be injected (tests); otherwise a Vertex AI / Gemini API client is built from settings.
    """
    from google.genai import errors as genai_errors
    from google.genai import types

    client = client or _make_client(settings)
    png = rasterize_svg(aoi.svg_markup)
    warnings: list[str] = []
    if png is None:
        warnings.append("AOI SVG could not be rasterised (libcairo missing); SVG markup sent as text instead.")

    user_prompt = build_user_prompt(
        sop, aoi, logf, authoritative, has_image=png is not None,
        trip_limit_mm_s=settings.rms_trip_limit_mm_s,
    )
    base_parts = [types.Part.from_text(text=user_prompt)]
    if png is not None:
        base_parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))

    config_kwargs: dict[str, Any] = {}
    if settings.gemini_thinking_level:  # empty string = leave the model default (e.g. gemini-1.5-*)
        config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=settings.gemini_thinking_level)
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=gemini_response_schema(settings),
        **config_kwargs,
    )

    max_attempts = settings.gemini_max_retries + 1
    feedback: list[str] = []
    last_problem = "no attempt made"
    all_transient = True  # stays True only if the model never produced a (bad) answer

    for attempt in range(1, max_attempts + 1):
        parts = list(base_parts)
        if feedback:
            parts.append(
                types.Part.from_text(
                    text="Your previous reply was rejected for these reasons. Fix every one and "
                    "return the complete corrected JSON:\n- " + "\n- ".join(feedback)
                )
            )
        try:
            resp = client.models.generate_content(
                model=settings.gemini_model,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            )
            text = resp.text or ""
        except genai_errors.ClientError as exc:
            code = getattr(exc, "code", None)
            if code in (401, 403):
                raise AgentFailure(
                    f"Gemini authentication/permission error ({code}): {exc}",
                    attempt,
                    hint="Check Application Default Credentials / API key and that the Vertex AI API is enabled.",
                ) from exc
            if code == 404:
                raise AgentFailure(
                    f"Model '{settings.gemini_model}' not found or not enabled in "
                    f"{settings.gcp_location}: {exc}",
                    attempt,
                    hint="Set GEMINI_MODEL to a model available to your project (e.g. gemini-3.8-flash).",
                ) from exc
            last_problem = f"Gemini client error ({code}): {exc}"
            log.warning("attempt %d/%d: %s", attempt, max_attempts, last_problem)
            if code == 429 and _is_daily_quota(str(exc)):
                # Retrying cannot help for hours, and a rules-engine result would hide that the
                # fix is simply to pick another model / key.
                raise AgentFailure(
                    f"Daily free-tier quota for model '{settings.gemini_model}' is exhausted"
                    f"{_retry_hint(str(exc))}.",
                    attempt,
                    hint="Choose a different model in the sidebar (each model has its own quota), "
                    "use another API key, or enable billing on the key's project.",
                ) from exc
            if code != 429:  # only rate-limits are worth retrying among 4xx
                raise AgentFailure(last_problem, attempt) from exc
            time.sleep(min(2**attempt, 8))
            continue
        except Exception as exc:  # noqa: BLE001 - 5xx, timeouts, DNS: transient, retry
            last_problem = f"Gemini transport/server error: {type(exc).__name__}: {exc}"
            log.warning("attempt %d/%d: %s", attempt, max_attempts, last_problem)
            time.sleep(min(2**attempt, 8))
            continue

        dossier, errors = validate_reply(settings, text)
        if dossier is None:
            all_transient = False  # the model answered, badly: a real defect, not an outage
            feedback = errors
            last_problem = "Schema validation failed: " + "; ".join(errors[:5])
            log.warning("attempt %d/%d: %s", attempt, max_attempts, last_problem)
            continue

        cit = dossier.grounded_sop_citation
        check = sop.verify_citation(cit.sop_document_id, cit.section, cit.page_number, cit.verbatim_quote)
        if not check.valid and attempt < max_attempts:
            all_transient = False
            feedback = [
                f"grounded_sop_citation is not verbatim: {check.reason} The quote must be copied "
                f"exactly from SOP Section {REQUIRED_SECTION}."
            ]
            last_problem = feedback[0]
            log.warning("attempt %d/%d: %s", attempt, max_attempts, last_problem)
            continue

        # Schema-valid. If the citation is still bad on the final attempt, the safety gate rejects it.
        return AgentResult(dossier, "gemini", settings.gemini_model, attempt, warnings)

    raise AgentFailure(
        f"Gemini did not return a schema-valid dossier after {max_attempts} attempt(s). Last problem: {last_problem}",
        max_attempts,
        hint="Retry the investigation; if it persists check the model name, quota and network egress.",
        transient=all_transient,
    )


# --------------------------------------------------------------------------- #
# Offline rules engine (explicitly NOT an LLM)
# --------------------------------------------------------------------------- #
def offline_rules_engine(
    settings: Settings,
    sop: SopDocument,
    aoi: AoiFacts,
    logf: ShiftLogFacts,
    authoritative: dict[str, Any],
) -> AgentResult:
    """Deterministic rule-based synthesis. Same output contract as Gemini, clearly labelled."""
    de = aoi.delta_e_max if aoi.delta_e_max is not None else 0.0
    signals = {
        "delta_e_over_sop_threshold": de > settings.delta_e_threshold,
        "vibration_drift_over_warning": logf.drift_fraction * 100 > settings.drift_warning_pct,
        "spectral_band_1_8_khz": logf.freq_khz == 1.8,
        "operating_temp_in_warning_band": logf.temp_c is not None and 52.0 <= logf.temp_c <= 58.0,
        "residue_reported_in_shift_log": "residue" in json.dumps(logf.entry).lower(),
        "aoi_flagged_ambiguous": aoi.is_ambiguous,
    }
    hits = sum(signals.values())
    confidence = round(min(0.99, 0.55 + 0.075 * hits), 3) if hits else 0.05
    degraded = signals["delta_e_over_sop_threshold"] and signals["vibration_drift_over_warning"]

    scada_state = (
        f"{(logf.scada_status or 'UNKNOWN').title()} ({logf.rms_mm_s} mm/s RMS under "
        f"{settings.rms_trip_limit_mm_s} mm/s trip limit, alarm_tripped={logf.alarm_tripped}, "
        f"but {logf.drift_pct_str} rolling-2h upward slope)"
    )
    obs = logf.entry.get("normalized_observations", {})
    shift_finding = (
        f"Operator {logf.operator_id} reported {str(obs.get('reported_anomaly', 'an anomaly')).lower()} "
        f"in the {logf.freq_khz} kHz band and '{obs.get('visual_comment', 'no visual comment')}' "
        f"at {logf.timestamp}; action taken: {obs.get('operator_action_taken', 'n/a')}."
    )
    if logf.data_quality_warnings:
        shift_finding += " " + " ".join(logf.data_quality_warnings)
    aoi_finding = (
        f"{aoi.status_text or 'AOI status unavailable'}; ROI {aoi.roi_label or 'n/a'}; "
        f"Delta-E = {de} against SOP threshold {settings.delta_e_threshold}."
    )
    if degraded:
        classification = "Thermal Grease Seal Degradation"
        summary = (
            f"Isolated systems look acceptable (SCADA {logf.scada_status}, RMS below trip limit, shift log "
            f"'non-critical'), but concurrent {logf.drift_pct_str} vibration drift, a {logf.freq_khz} kHz "
            f"harmonic, operating temperature {logf.temp_c} C and oxidised residue (Delta-E {de} > "
            f"{settings.delta_e_threshold}) match the seal-degradation signature of {sop.document_id} "
            f"Section {REQUIRED_SECTION}."
        )
    else:
        classification = "No Seal Degradation Detected (Nominal)"
        summary = (
            f"Delta-E {de} does not exceed the {sop.document_id} threshold of {settings.delta_e_threshold}; "
            "the AOI frame does not corroborate the telemetry drift, so no SOP remediation is warranted."
        )

    data = copy.deepcopy(authoritative["dossier_fields"])
    data.update(
        anomaly_signature={
            "classification": classification,
            "confidence_score": confidence,
            "optical_delta_e": de,
            "vibration_drift_pct": logf.drift_pct_str,
        },
        cross_silo_correlation={
            "scada_state": scada_state,
            "shift_log_finding": shift_finding,
            "aoi_visual_finding": aoi_finding,
            "synthesis_summary": summary,
        },
        grounded_sop_citation={
            "sop_document_id": f"{sop.document_id}.pdf",
            "section": f"Section {REQUIRED_SECTION}",
            "page_number": sop.page_of(REQUIRED_SECTION) or 0,
            "verbatim_quote": sop.directive_quote(),
            "citation_valid": True,
        },
        safety_gate={
            "requires_human_badge_auth": True,
            "autonomous_plc_override_blocked": True,
            "status": "PASSED_CONFIRMATION_REQUIRED",
        },
    )
    dossier = IncidentDossier.model_validate(data)
    return AgentResult(
        dossier,
        "offline-rules",
        None,
        0,
        [
            "OFFLINE RULES ENGINE: no LLM was called. Narrative and confidence were produced by "
            f"deterministic rules ({hits}/{len(signals)} corroborating signals), not Gemini inference."
        ],
    )
