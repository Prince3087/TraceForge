"""Deterministic Cost-of-Inaction engine. No LLM involvement in any number produced here.

    ScrapRisk = RunRate (units/hr) * DefectRateDrift * UnitScrapCost ($) * TimeToNextShift (hrs)

Line 4B defaults: 240 units/hr, $30 scrap cost/unit, 48 h risk horizon.

DefectRateDrift is taken from the +18.2 % rolling-2h vibration drift reported in the shift
log (SOP-SMT-4402 §2.1 treats this as the leading indicator of the seal-degradation defect
rate) expressed as a fraction (0.182).

Option parameters (pause minutes, derate %, max derate hours) are NOT hard-coded: they are
read from SOP-SMT-4402 §4.2.2 by ``sop_grounding.SopDocument.execution_options``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.models.incident import CostOfInaction, DecisionOption, LineParams
from backend.services.sop_grounding import SopOption


class CostModelError(ValueError):
    pass


def parse_drift_fraction(drift: str | float) -> float:
    """'+18.2%' -> 0.182. Accepts floats already expressed as a fraction."""
    if isinstance(drift, (int, float)):
        return float(drift)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", drift or "")
    if not m:
        raise CostModelError(f"Cannot parse drift percentage from '{drift}'.")
    return float(m.group(0)) / 100.0


@dataclass(frozen=True)
class CostInputs:
    run_rate_units_per_hr: float
    unit_scrap_cost_usd: float
    risk_horizon_hrs: float
    defect_rate_drift: float  # fraction, e.g. 0.182
    downtime_cost_per_hr_usd: float  # unplanned seizure downtime benchmark ($5,000/hr)
    seizure_rebuild_hours: float
    planned_pause_cost_per_hr_usd: float = 3000.0  # planned stop is cheaper: 15 min -> $750
    immediate_batch_window_hrs: float = 0.5  # 240 units/hr * 0.5 h = 120-unit batch in process

    @classmethod
    def from_params(
        cls,
        params: LineParams,
        defect_rate_drift: float,
        downtime_cost_per_hr_usd: float,
        seizure_rebuild_hours: float,
        planned_pause_cost_per_hr_usd: float = 3000.0,
        immediate_batch_window_hrs: float = 0.5,
    ) -> "CostInputs":
        return cls(
            run_rate_units_per_hr=params.run_rate_units_per_hr,
            unit_scrap_cost_usd=params.unit_scrap_cost_usd,
            risk_horizon_hrs=params.risk_horizon_hrs,
            defect_rate_drift=defect_rate_drift,
            downtime_cost_per_hr_usd=downtime_cost_per_hr_usd,
            seizure_rebuild_hours=seizure_rebuild_hours,
            planned_pause_cost_per_hr_usd=planned_pause_cost_per_hr_usd,
            immediate_batch_window_hrs=immediate_batch_window_hrs,
        )


def scrap_units(inp: CostInputs) -> float:
    """RunRate * DefectRateDrift * TimeToNextShift  (units)."""
    return inp.run_rate_units_per_hr * inp.defect_rate_drift * inp.risk_horizon_hrs


def scrap_risk_usd(inp: CostInputs) -> float:
    """ScrapRisk = RunRate * DefectRateDrift * UnitScrapCost * TimeToNextShift."""
    return scrap_units(inp) * inp.unit_scrap_cost_usd


def compute_cost_of_inaction(inp: CostInputs) -> CostOfInaction:
    """Scrap + catastrophic-seizure downtime if the seal replacement is delayed past the SOP window."""
    scrap = scrap_risk_usd(inp)
    downtime = inp.seizure_rebuild_hours * inp.downtime_cost_per_hr_usd
    return CostOfInaction(
        risk_window_hours=int(round(inp.risk_horizon_hrs)),
        projected_scrap_cost_usd=round(scrap, 2),
        downtime_loss_usd=round(downtime, 2),
        total_financial_exposure_usd=round(scrap + downtime, 2),
    )


def compute_cost_metrics(inp: CostInputs, pause_min: int) -> dict[str, Any]:
    """The two exposures that must never be conflated, plus downtime and the Option A pause cost.

    immediate_batch_scrap_usd        - batch currently in process (RunRate * window) * unit scrap cost;
                                       what a prompt Option A pause saves right now. 240*0.5*30 = $3,600.
    catastrophic_exposure_48h_usd    - ScrapRisk over the full horizon. 240*0.182*30*48 = $62,899.20.
    downtime_interruption_loss_usd   - unplanned seizure downtime. 8.5 h * $5,000 = $42,500.
    """
    batch_units = inp.run_rate_units_per_hr * inp.immediate_batch_window_hrs
    batch_usd = batch_units * inp.unit_scrap_cost_usd
    cat = scrap_risk_usd(inp)
    down = inp.seizure_rebuild_hours * inp.downtime_cost_per_hr_usd
    return {
        "immediate_batch_units": int(round(batch_units)),
        "immediate_batch_scrap_usd": round(batch_usd, 2),
        "catastrophic_exposure_48h_usd": round(cat, 2),
        "downtime_interruption_loss_usd": round(down, 2),
        "total_financial_exposure_usd": round(cat + down, 2),
        "option_a_pause_cost_usd": round(pause_min / 60.0 * inp.planned_pause_cost_per_hr_usd, 2),
        "option_a_immediate_net_usd": round(
            batch_usd - pause_min / 60.0 * inp.planned_pause_cost_per_hr_usd, 2
        ),
    }


def compute_decision_options(
    inp: CostInputs, sop_options: dict[str, SopOption]
) -> tuple[list[DecisionOption], dict[str, Any]]:
    """Build Option A / Option B from the SOP's own parameters.

    Option A (Full Stop): pause for ``pause_min``; seal swap restores nominal defect rate, so
        all drift-driven scrap in the horizon is avoided. Cost = pause downtime.
    Option B (Derated Feed): throttle ``derate_pct`` % for ``derate_max_hours`` while staging
        the replacement. The line keeps running at (1-derate) speed and keeps producing
        drift-defects during that window (residual scrap); lost throughput is costed at the
        line's hourly downtime rate for the equivalent lost production hours.
    """
    a, b = sop_options["OPTION_A"], sop_options["OPTION_B"]
    total_units = scrap_units(inp)
    rate = inp.planned_pause_cost_per_hr_usd  # planned stop / derate, not unplanned seizure

    # Option A
    a_cost = (a.pause_min / 60.0) * rate
    a_avoided = total_units

    # Option B
    derate = b.derate_pct / 100.0
    window = min(b.derate_max_hours, inp.risk_horizon_hrs)
    residual_units = inp.run_rate_units_per_hr * (1.0 - derate) * inp.defect_rate_drift * window
    lost_throughput_cost = derate * window * rate
    b_cost = lost_throughput_cost + residual_units * inp.unit_scrap_cost_usd
    b_avoided = max(total_units - residual_units, 0.0)

    options = [
        DecisionOption(
            option_id="OPTION_A",
            title=a.text,
            action_type="IMMEDIATE_CALIBRATION_PAUSE",
            line_pause_min=int(a.pause_min),
            projected_cost_usd=round(a_cost, 2),
            scrap_avoided_units=int(round(a_avoided)),
        ),
        DecisionOption(
            option_id="OPTION_B",
            title=b.text,
            action_type="DERATED_SPEED_RUN",
            line_pause_min=0,
            projected_cost_usd=round(b_cost, 2),
            scrap_avoided_units=int(round(b_avoided)),
        ),
    ]
    detail = {
        "option_a": {
            "pause_min": a.pause_min,
            "pause_cost_usd": round(a_cost, 2),
            "scrap_units_avoided": round(a_avoided, 2),
            "scrap_usd_avoided": round(a_avoided * inp.unit_scrap_cost_usd, 2),
        },
        "option_b": {
            "derate_pct": b.derate_pct,
            "derate_window_hrs": window,
            "lost_throughput_cost_usd": round(lost_throughput_cost, 2),
            "residual_scrap_units": round(residual_units, 2),
            "residual_scrap_usd": round(residual_units * inp.unit_scrap_cost_usd, 2),
            "scrap_units_avoided": round(b_avoided, 2),
            "scrap_usd_avoided": round(b_avoided * inp.unit_scrap_cost_usd, 2),
        },
    }
    return options, detail


def formula_breakdown(
    inp: CostInputs, option_detail: dict[str, Any], metrics: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Human-auditable trace of every number, surfaced in the API response."""
    return {
        "formula": "ScrapRisk = RunRate(units/hr) * DefectRateDrift * UnitScrapCost($) * TimeToNextShift(hrs)",
        "run_rate_units_per_hr": inp.run_rate_units_per_hr,
        "defect_rate_drift": inp.defect_rate_drift,
        "unit_scrap_cost_usd": inp.unit_scrap_cost_usd,
        "time_to_next_shift_hrs": inp.risk_horizon_hrs,
        "scrap_units_at_risk": round(scrap_units(inp), 2),
        "scrap_risk_usd": round(scrap_risk_usd(inp), 2),
        "downtime_cost_per_hr_usd": inp.downtime_cost_per_hr_usd,
        "seizure_rebuild_hours": inp.seizure_rebuild_hours,
        "downtime_loss_formula": "seizure_rebuild_hours * downtime_cost_per_hr_usd",
        "planned_pause_cost_per_hr_usd": inp.planned_pause_cost_per_hr_usd,
        "immediate_batch_window_hrs": inp.immediate_batch_window_hrs,
        "metrics": metrics or {},
        "options": option_detail,
    }
