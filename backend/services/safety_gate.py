"""Safety Gate.

Deterministic, model-independent checks applied (a) after synthesis in /incidents/investigate and
(b) again, server-side, on the dossier submitted to /incidents/signoff. A dossier passes only if

  1. vector confidence >= 90 %                                 (config.min_confidence)
  2. the SOP citation is a *verbatim* quote of Section 4.2 on the cited page
  3. optical Delta-E exceeds the SOP §4.2.1 threshold (3.0)
  4. every remediation option is verbatim SOP §4.2.2 text with matching action semantics
  5. no autonomous PLC override path exists (this service has no PLC client; flag must be True)

Any failure => status REJECTED_UNGROUNDED, and sign-off is refused.
"""

from __future__ import annotations

from backend.config import Settings
from backend.models.incident import GateCheck, IncidentDossier, SafetyGate
from backend.services.sop_grounding import SopDocument, SopError


def evaluate(dossier: IncidentDossier, sop: SopDocument, settings: Settings) -> list[GateCheck]:
    checks: list[GateCheck] = []

    # 1. Confidence ------------------------------------------------------------------------
    conf = dossier.anomaly_signature.confidence_score
    checks.append(
        GateCheck(
            name="vector_confidence",
            passed=conf >= settings.min_confidence,
            detail=f"confidence {conf:.1%} vs required >= {settings.min_confidence:.0%}",
        )
    )

    # 2. Verbatim citation -----------------------------------------------------------------
    cit = dossier.grounded_sop_citation
    result = sop.verify_citation(cit.sop_document_id, cit.section, cit.page_number, cit.verbatim_quote)
    checks.append(
        GateCheck(
            name="verbatim_sop_citation",
            passed=result.valid,
            detail=f"{cit.sop_document_id} {cit.section} p.{cit.page_number}: {result.reason}",
        )
    )

    # 3. Delta-E threshold -------------------------------------------------------------------
    de = dossier.anomaly_signature.optical_delta_e
    checks.append(
        GateCheck(
            name="delta_e_exceeds_sop_threshold",
            passed=de > settings.delta_e_threshold,
            detail=f"Delta-E {de:.2f} vs SOP §4.2.1 threshold > {settings.delta_e_threshold:.1f}",
        )
    )

    # 4. Options trace to SOP §4.2.2 -------------------------------------------------------------
    try:
        sop_opts = sop.execution_options()
        problems: list[str] = []
        by_id = {o.option_id: o for o in dossier.decision_options}
        if set(by_id) != {"OPTION_A", "OPTION_B"}:
            problems.append("options must be exactly OPTION_A and OPTION_B")
        for oid, opt in by_id.items():
            if not sop.text_is_grounded(opt.title, "4.2"):
                problems.append(f"{oid} title is not verbatim SOP §4.2.2 text")
        a, b = by_id.get("OPTION_A"), by_id.get("OPTION_B")
        if a and (a.action_type != "IMMEDIATE_CALIBRATION_PAUSE" or a.line_pause_min != sop_opts["OPTION_A"].pause_min):
            problems.append(f"OPTION_A must be a {sop_opts['OPTION_A'].pause_min}-minute pause per SOP")
        if b and (b.action_type != "DERATED_SPEED_RUN" or b.line_pause_min != 0):
            problems.append("OPTION_B must be a derated run with no full pause per SOP")
        checks.append(
            GateCheck(
                name="remediation_traces_to_sop",
                passed=not problems,
                detail="; ".join(problems) if problems else "Options A/B are verbatim SOP §4.2.2 text.",
            )
        )
    except SopError as exc:
        checks.append(GateCheck(name="remediation_traces_to_sop", passed=False, detail=str(exc)))

    # 5. No autonomous PLC override -------------------------------------------------------------
    checks.append(
        GateCheck(
            name="autonomous_plc_override_blocked",
            passed=dossier.safety_gate.autonomous_plc_override_blocked is True,
            detail="TraceForge issues no PLC/SCADA write commands; execution requires a human at the line.",
        )
    )
    return checks


def seal_gate(checks: list[GateCheck]) -> SafetyGate:
    """Derive the schema's safety_gate object from the check results. PLC override is always blocked."""
    all_passed = all(c.passed for c in checks)
    return SafetyGate(
        requires_human_badge_auth=True,
        autonomous_plc_override_blocked=True,
        status="PASSED_CONFIRMATION_REQUIRED" if all_passed else "REJECTED_UNGROUNDED",
    )


def failed(checks: list[GateCheck]) -> list[GateCheck]:
    return [c for c in checks if not c.passed]
