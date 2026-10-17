"""Environment-driven configuration for the TraceForge backend.

Everything tunable lives here so Cloud Run can override it with ``--set-env-vars``
without touching code.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


# Models offered in the cockpit dropdown (all verified with the Gemini Developer API, structured output
# and thinking_level=LOW). The first entry is the default "best" model; any other ID can be typed in.
GEMINI_MODEL_CATALOG: tuple[dict[str, str], ...] = (
    {"id": "gemini-3.8-flash", "label": "Gemini 3.8 Flash", "tier": "Recommended - best quality"},
    {"id": "gemini-3.7-flash", "label": "Gemini 3.7 Flash", "tier": "Balanced"},
    {"id": "gemini-3.5-flash", "label": "Gemini 3.5 Flash", "tier": "Balanced"},
    {"id": "gemini-flash-latest", "label": "Gemini Flash (latest alias)", "tier": "Auto-updated"},
    {"id": "gemini-3.1-flash-lite", "label": "Gemini 3.1 Flash-Lite", "tier": "Fastest - lightweight"},
    {"id": "gemini-flash-lite-latest", "label": "Gemini Flash-Lite (latest alias)", "tier": "Fastest - auto-updated"},
)


@dataclass(frozen=True)
class Settings:
    # ------------------------------------------------------------------ paths
    data_dir: Path
    ledger_path: Path
    schema_path: Path
    sop_md_path: Path
    sop_pdf_filename: str
    logs_path: Path

    # ----------------------------------------------------------- GCP / Gemini
    gcp_project: str | None
    gcp_location: str
    gemini_api_key: str | None  # Optional: Gemini Developer API instead of Vertex AI
    gemini_model: str
    gemini_max_retries: int
    gemini_timeout_s: int
    # LOW/MEDIUM/HIGH; empty = model default. LOW keeps latency ~8s vs 20-60s+ (504s) at default.
    gemini_thinking_level: str
    # Explicit, clearly-labelled rules-engine mode for demos/tests without credentials.
    offline_mode: bool
    # On transient Gemini failure (429 / 5xx / network) fall back to the deterministic rules engine.
    allow_offline_fallback: bool

    # ---------------------------------------------------- Line 4B cost defaults
    run_rate_units_per_hr: float
    unit_scrap_cost_usd: float
    risk_horizon_hrs: float
    downtime_cost_per_hr_usd: float
    seizure_rebuild_hours: float
    planned_pause_cost_per_hr_usd: float  # planned stop; 15 min -> $750 at the $3,000/hr default
    immediate_batch_window_hrs: float  # in-process batch: 240 units/hr * 0.5 h = 120 units

    # ------------------------------------------------------------- Safety gate
    min_confidence: float  # 0.90 == "vector confidence >= 90%"
    delta_e_threshold: float  # SOP-SMT-4402 §4.2.1
    drift_warning_pct: float  # SOP-SMT-4402 §2.1 / Operational Warning
    rms_trip_limit_mm_s: float  # SOP-SMT-4402 Table 2.1
    min_quote_chars: int

    # ------------------------------------------------------- integrity / ledger
    signing_key: str  # HMAC-SHA256 key for dossier signatures
    signing_key_ephemeral: bool  # True when no TRACEFORGE_SIGNING_KEY was provided
    ledger_bucket_name: str | None  # GCS bucket for durable ledger entries (None = local only)
    ledger_remote_prefix: str

    # ------------------------------------------------------------------- misc
    cors_origins: tuple[str, ...]

    @property
    def use_vertex(self) -> bool:
        """Vertex AI is used unless a Gemini Developer API key is supplied."""
        return not self.gemini_api_key

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key or self.gcp_project)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.getenv("TRACEFORGE_DATA_DIR", REPO_ROOT / "data")).resolve()
    return Settings(
        data_dir=data_dir,
        ledger_path=Path(os.getenv("TRACEFORGE_LEDGER_PATH", data_dir / "audit_ledger.jsonl")),
        schema_path=data_dir / "schema.json",
        sop_md_path=data_dir / "SOP-SMT-4402_HighSpeed_Feeder.md",
        sop_pdf_filename="SOP-SMT-4402_HighSpeed_Feeder.pdf",
        logs_path=data_dir / "sample_shift_logs.json",
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or None,
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", os.getenv("GCP_LOCATION", "us-central1")),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or None,
        # Gemini 1.5 models are retired on Vertex AI; GEMINI_MODEL can still be set to
        # "gemini-1.5-flash" / "gemini-1.5-pro" where they remain available.
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash"),
        gemini_max_retries=_env_int("GEMINI_MAX_RETRIES", 2),
        gemini_timeout_s=_env_int("GEMINI_TIMEOUT_S", 90),
        gemini_thinking_level=os.getenv("GEMINI_THINKING_LEVEL", "LOW").strip().upper(),
        offline_mode=_env_bool("TRACEFORGE_OFFLINE_MODE", False),
        allow_offline_fallback=_env_bool("GEMINI_FALLBACK_ENABLED", True),
        run_rate_units_per_hr=_env_float("RUN_RATE_UNITS_PER_HR", 240.0),
        unit_scrap_cost_usd=_env_float("UNIT_SCRAP_COST_USD", 30.0),
        risk_horizon_hrs=_env_float("RISK_HORIZON_HRS", 48.0),
        downtime_cost_per_hr_usd=_env_float("DOWNTIME_COST_PER_HR_USD", 5000.0),
        seizure_rebuild_hours=_env_float("SEIZURE_REBUILD_HOURS", 8.5),
        planned_pause_cost_per_hr_usd=_env_float("PLANNED_PAUSE_COST_PER_HR_USD", 3000.0),
        immediate_batch_window_hrs=_env_float("IMMEDIATE_BATCH_WINDOW_HRS", 0.5),
        min_confidence=_env_float("MIN_CONFIDENCE", 0.90),
        delta_e_threshold=_env_float("DELTA_E_THRESHOLD", 3.0),
        drift_warning_pct=_env_float("DRIFT_WARNING_PCT", 15.0),
        rms_trip_limit_mm_s=_env_float("RMS_TRIP_LIMIT_MM_S", 4.5),
        min_quote_chars=_env_int("MIN_QUOTE_CHARS", 40),
        # No hard-coded default secret: without TRACEFORGE_SIGNING_KEY a random per-process key is
        # used, so signatures cannot be forged but are invalidated by a restart.
        signing_key=os.getenv("TRACEFORGE_SIGNING_KEY") or secrets.token_hex(32),
        signing_key_ephemeral=not os.getenv("TRACEFORGE_SIGNING_KEY"),
        ledger_bucket_name=os.getenv("LEDGER_BUCKET_NAME") or None,
        ledger_remote_prefix=os.getenv("LEDGER_REMOTE_PREFIX", "ledger").strip("/"),
        cors_origins=tuple(
            o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()
        ),
    )
