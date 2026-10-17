"""Evidence loading: AOI images, shift logs, SCADA facts.

Everything here is deterministic. Numbers that matter (Delta-E, drift, RMS) are extracted from the
source artefacts directly and later *override* whatever the LLM reports.
"""

from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import Settings

_SAFE_ASSET = re.compile(r"^[A-Za-z0-9_.\-]+\.svg$")


class EvidenceError(ValueError):
    """Bad or missing evidence input (maps to HTTP 4xx)."""


# --------------------------------------------------------------------------- #
# AOI
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AoiFacts:
    filename: str
    svg_markup: str
    delta_e_max: float | None
    status_text: str | None
    camera_id: str | None
    roi: dict[str, float] | None  # x, y, width, height in viewBox units
    roi_label: str | None
    view_box: tuple[float, float, float, float]
    overlay_text: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.status_text and "AMBIGUOUS" in self.status_text.upper())


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_aoi_svg(filename: str, svg_markup: str) -> AoiFacts:
    try:
        root = ET.fromstring(svg_markup)
    except ET.ParseError as exc:
        raise EvidenceError(f"AOI asset '{filename}' is not valid SVG: {exc}") from exc

    vb = [float(v) for v in re.split(r"[\s,]+", root.get("viewBox", "0 0 600 600").strip())]
    view_box = (vb[0], vb[1], vb[2], vb[3]) if len(vb) == 4 else (0.0, 0.0, 600.0, 600.0)

    texts: list[str] = []
    rects: list[dict[str, str]] = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "text":
            t = "".join(el.itertext()).strip()
            if t:
                texts.append(t)
        elif name == "rect":
            rects.append(dict(el.attrib))

    delta_e = None
    for t in texts:
        m = re.search(r"ΔE_MAX:\s*([\d.]+)", t)
        if m:
            delta_e = float(m.group(1))
            break
    if delta_e is None:
        for t in texts:
            m = re.search(r"ΔE\s*=\s*([\d.]+)", t)
            if m:
                delta_e = float(m.group(1))
                break

    status = next((t for t in texts if t.upper().startswith("STATUS:")), None)
    camera = next((t for t in texts if t.upper().startswith("CAM_")), None)
    roi_label = next((t for t in texts if t.upper().startswith("ROI_")), None)

    roi = None
    if roi_label:
        # The ROI rectangle is the cyan, unfilled dashed box (stroke #00f3ff, fill none).
        for r in rects:
            if r.get("stroke", "").lower() == "#00f3ff" and r.get("fill", "").lower() == "none":
                try:
                    roi = {k: float(r[k]) for k in ("x", "y", "width", "height")}
                except (KeyError, ValueError):
                    roi = None
                break

    return AoiFacts(
        filename=filename,
        svg_markup=svg_markup,
        delta_e_max=delta_e,
        status_text=status,
        camera_id=camera,
        roi=roi,
        roi_label=roi_label,
        view_box=view_box,
        overlay_text=tuple(texts),
    )


def list_aoi_filenames(settings: Settings) -> list[str]:
    return sorted(p.name for p in settings.data_dir.glob("aoi_*.svg") if p.is_file())


def load_aoi(settings: Settings, filename: str) -> AoiFacts:
    # Allow-list lookup (never build a path from user input directly -> no traversal).
    available = list_aoi_filenames(settings)
    if not _SAFE_ASSET.match(filename or "") or filename not in available:
        raise EvidenceError(
            f"Unknown AOI asset '{filename}'. Available: {', '.join(available) or 'none'}"
        )
    path: Path = settings.data_dir / filename
    return parse_aoi_svg(filename, path.read_text(encoding="utf-8"))


def rasterize_svg(svg_markup: str, size: int = 768) -> bytes | None:
    """SVG -> PNG for multimodal input. Returns None when libcairo is unavailable.

    Gemini does not accept image/svg+xml, so when rasterisation is impossible the agent falls
    back to sending the SVG markup as text (it embeds the ROI, status and Delta-E annotations).
    """
    try:
        import cairosvg  # imported lazily: needs the native libcairo
    except (ImportError, OSError):
        return None
    try:
        return cairosvg.svg2png(
            bytestring=svg_markup.encode("utf-8"), output_width=size, output_height=size
        )
    except Exception:  # noqa: BLE001 - rendering must never take the investigation down
        return None


# --------------------------------------------------------------------------- #
# Shift logs / SCADA
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ShiftLogFacts:
    entry: dict[str, Any]
    log_id: str
    timestamp: str
    line_id: str
    component_id: str
    operator_id: str
    drift_pct_str: str
    drift_fraction: float
    rms_mm_s: float | None
    temp_c: float | None
    freq_khz: float | None
    scada_status: str | None
    alarm_tripped: bool | None
    baseline_median_rms: float | None
    data_quality_warnings: tuple[str, ...]


def load_logs(settings: Settings) -> list[dict[str, Any]]:
    try:
        data = json.loads(settings.logs_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EvidenceError(f"Shift log file not found: {settings.logs_path.name}") from exc
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"Shift log file is not valid JSON: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise EvidenceError("Shift log file must contain a non-empty JSON array.")
    return data


def extract_log_facts(settings: Settings, log_id: str | None) -> ShiftLogFacts:
    logs = load_logs(settings)
    if log_id:
        entry = next((e for e in logs if e.get("log_id") == log_id), None)
        if entry is None:
            raise EvidenceError(
                f"Unknown log_id '{log_id}'. Available: {', '.join(str(e.get('log_id')) for e in logs)}"
            )
    else:
        entry = max(logs, key=lambda e: e.get("timestamp", ""))

    obs = entry.get("normalized_observations", {}) or {}
    corr = entry.get("correlation_metadata", {}) or {}
    drift_str = str(corr.get("rolling_2h_drift_percentage", ""))
    m = re.search(r"[-+]?\d+(?:\.\d+)?", drift_str)
    if not m:
        raise EvidenceError("Shift log has no parsable rolling_2h_drift_percentage.")

    warnings: list[str] = []
    raw = str(entry.get("raw_text", ""))
    freq = obs.get("indicated_frequency_band_khz")
    # Detect unit conflict between free text ("1.8 Hz") and normalised field (1.8 kHz).
    m_hz = re.search(r"(\d+(?:\.\d+)?)\s*(k?Hz)\b", raw, re.I)
    if m_hz and freq is not None:
        raw_unit = m_hz.group(2).lower()
        if raw_unit == "hz" and float(m_hz.group(1)) == float(freq):
            warnings.append(
                f"DATA-QUALITY: shift-log raw_text says '{m_hz.group(0)}' but the normalised field and "
                f"SOP-SMT-4402 Table 2.1 specify {freq} kHz. Treated as a transcription error; "
                "operator should confirm."
            )

    return ShiftLogFacts(
        entry=entry,
        log_id=str(entry.get("log_id", "UNKNOWN")),
        timestamp=str(entry.get("timestamp", "")),
        line_id=str(entry.get("line_id", "UNKNOWN")),
        component_id=str(entry.get("station_id", "UNKNOWN")),
        operator_id=str(entry.get("operator_id", "UNKNOWN")),
        drift_pct_str=drift_str if drift_str.strip().startswith(("+", "-")) else f"+{m.group(0)}%",
        drift_fraction=float(m.group(0)) / 100.0,
        rms_mm_s=obs.get("rms_vibration_mm_s"),
        temp_c=obs.get("operating_temperature_c"),
        freq_khz=freq,
        scada_status=corr.get("scada_status"),
        alarm_tripped=corr.get("alarm_tripped"),
        baseline_median_rms=corr.get("baseline_median_rms"),
        data_quality_warnings=tuple(warnings),
    )


def make_incident_id(facts: ShiftLogFacts) -> str:
    """Deterministic: INC-YYYY-MM-DD-NNNN, NNNN derived from the log id."""
    date = facts.timestamp[:10] if re.match(r"\d{4}-\d{2}-\d{2}", facts.timestamp) else "0000-00-00"
    n = int(hashlib.sha256(facts.log_id.encode()).hexdigest()[:8], 16) % 10000
    return f"INC-{date}-{n:04d}"


def _label_for(filename: str) -> str:
    if "defect" in filename:
        return "Defective / Ambiguous AOI"
    if "healthy" in filename:
        return "Healthy / Nominal AOI"
    return filename


def demo_assets(settings: Settings) -> dict[str, Any]:
    images = []
    for fn in list_aoi_filenames(settings):
        facts = load_aoi(settings, fn)
        images.append(
            {
                "filename": fn,
                "label": _label_for(fn),
                "kind": "defective" if "defect" in fn else "healthy" if "healthy" in fn else "other",
                "delta_e_max": facts.delta_e_max,
                "status": facts.status_text,
                "camera": facts.camera_id,
                "roi": facts.roi,
                "roi_label": facts.roi_label,
                "view_box": list(facts.view_box),
                "svg": facts.svg_markup,
            }
        )
    return {"aoi_images": images, "shift_logs": load_logs(settings)}
