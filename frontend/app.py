"""TraceForge - industrial command console (Streamlit).

Run:  streamlit run frontend/app.py
Backend URL is read from TRACEFORGE_API_URL (default http://127.0.0.1:8000).

Layout (split-pane evidence dossier)
    left   : AOI viewer (Healthy / Defective toggle) with ROI bounding-box overlay
    centre : SOP citation evidence box + cross-silo correlation + safety-gate checks
    right  : Option A vs Option B trade-off matrix + cost of inaction
    bottom : human approval checkpoint (badge ID + seal audit ledger)
"""

from __future__ import annotations

import base64
import html
import os
import re
from typing import Any

import requests
import streamlit as st

API_URL = os.getenv("TRACEFORGE_API_URL", "http://127.0.0.1:8000").rstrip("/")

# Must stay identical to BADGE_RE in backend/main.py (the server is the authority; this only saves a round trip).
BADGE_RE = re.compile(r"^(TECH|ENG|MGR)_[0-9]{4}_[A-Z]+$")

BLUE, CYAN, OBSIDIAN, AMBER = "#0f62fe", "#00f3ff", "#0b0f19", "#f59e0b"
GREEN, RED, PANEL, BORDER, MUTED = "#24d18f", "#fa4d56", "#121a2c", "#1f2b45", "#8d9bb5"

st.set_page_config(page_title="TraceForge | Interrupt 0xFF", page_icon="⬢", layout="wide")

CSS = f"""
<style>
.stApp {{ background: {OBSIDIAN}; }}
header[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding-top: 1.2rem; max-width: 1700px; }}
.tf-title {{ font-family: ui-monospace, Menlo, monospace; letter-spacing: .18em; color: {CYAN};
  font-size: 1.55rem; font-weight: 700; margin: 0; }}
.tf-sub {{ color: {MUTED}; font-size: .85rem; margin: 0 0 .6rem 0; }}
.tf-panel {{ background: {PANEL}; border: 1px solid {BORDER}; border-top: 2px solid {BLUE};
  border-radius: 4px; padding: .9rem 1rem; margin-bottom: .8rem; }}
.tf-panel h4 {{ margin: 0 0 .6rem 0; font-family: ui-monospace, Menlo, monospace; font-size: .8rem;
  letter-spacing: .16em; text-transform: uppercase; color: {CYAN}; }}
.tf-chip {{ display:inline-block; padding: .1rem .55rem; border-radius: 2px; font-size: .72rem;
  font-family: ui-monospace, Menlo, monospace; letter-spacing: .06em; font-weight: 700; }}
.tf-ok {{ background: rgba(36,209,143,.14); color: {GREEN}; border: 1px solid {GREEN}; }}
.tf-bad {{ background: rgba(250,77,86,.14); color: {RED}; border: 1px solid {RED}; }}
.tf-warn {{ background: rgba(245,158,11,.14); color: {AMBER}; border: 1px solid {AMBER}; }}
.tf-info {{ background: rgba(15,98,254,.18); color: #78a9ff; border: 1px solid {BLUE}; }}
.tf-kv {{ display:flex; justify-content: space-between; gap: .8rem; padding: .22rem 0;
  border-bottom: 1px dashed {BORDER}; font-size: .84rem; }}
.tf-kv span:first-child {{ color: {MUTED}; }}
.tf-kv span:last-child {{ font-family: ui-monospace, Menlo, monospace; text-align: right; }}
.tf-quote {{ background: #0a1020; border-left: 4px solid {AMBER}; padding: .8rem 1rem; margin: .6rem 0;
  font-family: Georgia, 'Times New Roman', serif; font-size: 1.02rem; line-height: 1.5; color: #fff3d6; }}
.tf-silo {{ font-size: .84rem; line-height: 1.45; padding: .45rem 0; border-bottom: 1px dashed {BORDER}; }}
.tf-silo b {{ color: {CYAN}; font-family: ui-monospace, Menlo, monospace; font-size: .72rem;
  letter-spacing: .1em; display:block; }}
.tf-aoi {{ position: relative; width: 100%; aspect-ratio: 1 / 1; background: #05070a;
  border: 1px solid {BORDER}; border-radius: 4px; overflow: hidden; }}
.tf-aoi img {{ position:absolute; inset:0; width:100%; height:100%; }}
.tf-roi {{ position:absolute; border: 2px solid {CYAN}; box-shadow: 0 0 14px {CYAN}, inset 0 0 10px rgba(0,243,255,.35);
  border-radius: 4px; pointer-events: none; }}
.tf-roi-tag {{ position:absolute; top:-1.35rem; left:-2px; background:{CYAN}; color:{OBSIDIAN};
  font-family: ui-monospace, Menlo, monospace; font-size:.66rem; font-weight:700; padding:0 .4rem; white-space:nowrap; }}
table.tf-matrix {{ width:100%; border-collapse: collapse; font-size:.84rem; }}
table.tf-matrix th, table.tf-matrix td {{ border: 1px solid {BORDER}; padding: .45rem .55rem; text-align:left;
  vertical-align: top; }}
table.tf-matrix th {{ background:#0a1020; color:{CYAN}; font-family: ui-monospace, Menlo, monospace;
  font-size:.72rem; letter-spacing:.08em; }}
table.tf-matrix td.metric {{ color:{MUTED}; width: 34%; }}
table.tf-matrix td.sel {{ background: rgba(15,98,254,.16); }}
.tf-big {{ font-family: ui-monospace, Menlo, monospace; font-size: 1.5rem; font-weight: 700; color: {AMBER}; }}
.tf-seal {{ background: rgba(36,209,143,.08); border: 1px solid {GREEN}; border-radius: 4px; padding: .8rem 1rem; }}
.tf-hash {{ font-family: ui-monospace, Menlo, monospace; font-size: .74rem; word-break: break-all; color: #b6c4e0; }}
div[data-testid="stButton"] button[kind="primary"] {{ background: {BLUE}; border: 1px solid {CYAN};
  font-weight: 700; letter-spacing: .04em; }}
div[data-testid="stButton"] button[kind="primary"]:hover {{ background:#0043ce; border-color:{CYAN}; }}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Backend client
# --------------------------------------------------------------------------- #
class ApiError(RuntimeError):
    def __init__(self, status: int | None, error: str, detail: str, hint: str | None = None):
        super().__init__(detail)
        self.status, self.error, self.detail, self.hint = status, error, detail, hint


def api(method: str, path: str, *, timeout: int = 30, **kwargs: Any) -> dict[str, Any]:
    try:
        r = requests.request(method, f"{API_URL}{path}", timeout=timeout, **kwargs)
    except requests.exceptions.ConnectionError as exc:
        raise ApiError(None, "BACKEND_UNREACHABLE", f"Cannot reach TraceForge API at {API_URL}.",
                       "Start it with: uvicorn backend.main:app --port 8000") from exc
    except requests.exceptions.Timeout as exc:
        raise ApiError(None, "BACKEND_TIMEOUT", f"API call to {path} timed out after {timeout}s.") from exc
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code >= 400:
        raise ApiError(r.status_code, body.get("error", "HTTP_ERROR"),
                       body.get("detail") or r.text[:300], body.get("hint"))
    return body


@st.cache_data(ttl=60, show_spinner=False)
def fetch_models(api_url: str) -> dict[str, Any]:  # api_url is part of the cache key
    return api("GET", "/models")


@st.cache_data(ttl=60, show_spinner=False)
def fetch_assets(api_url: str) -> dict[str, Any]:  # api_url is part of the cache key
    return api("GET", "/incidents/demo-assets")


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def usd(v: float) -> str:
    return f"${v:,.2f}"


def usd0(v: float) -> str:
    return f"${v:,.0f}"


def svg_has_embedded_roi(svg_markup: str) -> bool:
    """True when the AOI SVG already draws its own ROI box + label (e.g. ROI_01)."""
    return re.search(r">\s*ROI_\w+", svg_markup) is not None


def chip(text: str, kind: str) -> str:
    return f'<span class="tf-chip tf-{kind}">{esc(text)}</span>'


def kv(label: str, value: Any) -> str:
    return f'<div class="tf-kv"><span>{esc(label)}</span><span>{esc(value)}</span></div>'


def panel(title: str, body: str) -> None:
    st.markdown(f'<div class="tf-panel"><h4>{esc(title)}</h4>{body}</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Header + backend status
# --------------------------------------------------------------------------- #
head_l, head_r = st.columns([3, 2])
with head_l:
    st.markdown('<p class="tf-title">TRACEFORGE // EVIDENCE COMPILER</p>'
                '<p class="tf-sub">Interrupt 0xFF · Ambiguous-incident dossier for LINE-4B · '
                'SOP-grounded · human-in-the-loop</p>', unsafe_allow_html=True)

try:
    health = api("GET", "/health", timeout=8)
    assets = fetch_assets(API_URL)
except ApiError as exc:
    st.error(f"{exc.error}: {exc.detail}" + (f"  \n{exc.hint}" if exc.hint else ""))
    st.stop()

with head_r:
    engine = health["engine"]
    g = health["gemini"]
    chips = chip("API ONLINE", "ok")
    if engine == "offline-rules":
        chips += " " + chip("OFFLINE RULES ENGINE (NO LLM)", "warn")
    elif st.session_state.get("gemini_key_ui", "").strip():
        chips += " " + chip(f"GEMINI · {st.session_state.get('gemini_model_ui') or g['model']} · UI KEY", "info")
    elif g["configured"]:
        chips += " " + chip(f"GEMINI · {g['model']} · {g['backend']}", "info")
    else:
        chips += " " + chip("GEMINI NOT CONFIGURED", "bad")
    chips += " " + chip("LEDGER INTACT" if health["ledger"]["valid"] else "LEDGER TAMPERED",
                        "ok" if health["ledger"]["valid"] else "bad")
    chips += " " + (chip("LEDGER DURABLE · GCS", "ok") if health["ledger"].get("durable")
                    else chip("LEDGER LOCAL ONLY", "warn"))
    st.markdown(f'<div style="text-align:right;padding-top:.5rem">{chips}</div>', unsafe_allow_html=True)

images = {i["kind"]: i for i in assets["aoi_images"]}
defaults = assets["line_defaults"]
logs = assets["shift_logs"]

# --------------------------------------------------------------------------- #
# Sidebar: line parameters + log selection
# --------------------------------------------------------------------------- #
CUSTOM_MODEL = "Custom model ID…"
with st.sidebar:
    st.markdown("### Gemini engine")
    model_cfg = fetch_models(API_URL)
    catalog = {m["id"]: m for m in model_cfg["models"]}
    options = list(catalog) + [CUSTOM_MODEL]
    default_idx = options.index(model_cfg["default_model"]) if model_cfg["default_model"] in catalog else 0
    picked = st.selectbox(
        "Model", options, index=default_idx,
        format_func=lambda i: i if i == CUSTOM_MODEL else f'{catalog[i]["label"]}  ·  {catalog[i]["tier"]}',
        help="If a model times out (504) or is overloaded, switch to a lighter one such as Flash-Lite and re-run.",
    )
    model_id = (st.text_input("Custom model ID", placeholder="e.g. gemini-3.1-flash-lite").strip()
                if picked == CUSTOM_MODEL else picked)
    st.session_state["gemini_model_ui"] = model_id
    api_key_in = st.text_input(
        "Gemini API key", type="password", key="gemini_key_ui", placeholder="AIza… / AQ.…  (optional)",
        help="Used for this session's investigations only. Sent to the TraceForge backend over the request, "
             "never stored or logged. Leave blank to use the server's configured credentials.",
    ).strip()
    if api_key_in:
        st.caption("🔑 Using the key you entered.")
    elif model_cfg["server_key_configured"]:
        st.caption("🔑 Using the server's configured credentials.")
    elif model_cfg["offline_mode"]:
        st.caption("⚠ Server is in offline rules mode — enter a key to use Gemini.")
    else:
        st.warning("No API key configured on the server — enter one above.")
    st.markdown("---")
    st.markdown("### Line 4B parameters")
    run_rate = st.number_input("Run rate (units/hr)", min_value=1.0, value=float(defaults["run_rate_units_per_hr"]), step=10.0)
    scrap_cost = st.number_input("Unit scrap cost ($)", min_value=0.0, value=float(defaults["unit_scrap_cost_usd"]), step=1.0)
    horizon = st.number_input("Risk horizon / time to next shift (hrs)", min_value=1.0, value=float(defaults["risk_horizon_hrs"]), step=1.0)
    log_id = st.selectbox("Shift log entry", [l["log_id"] for l in logs])
    st.caption("ScrapRisk = RunRate × DefectRateDrift × UnitScrapCost × TimeToNextShift")
    st.markdown("---")
    st.caption(f"API: {API_URL}")
    if st.button("Verify audit ledger chain"):
        try:
            v = api("GET", "/ledger/verify")
            (st.success if v["valid"] else st.error)(f"{v['records']} record(s): {v['reason']}")
        except ApiError as exc:
            st.error(exc.detail)

# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
ss = st.session_state
# All keys are initialised BEFORE any widget is created so every widget and pane reads the same value
# in the same render pass.
ss.setdefault("aoi_kind", "defective")
ss.setdefault("selected_option", "OPTION_A")
ss.setdefault("investigation_result", None)  # last /incidents/investigate response (dossier+signature+meta)
ss.setdefault("last_image_mode", ss["aoi_kind"])
ss.setdefault("sealed", None)
ss.setdefault("investigate_error", None)
ss.setdefault("frame_changed", False)


def on_image_mode_change() -> None:
    """AOI toggle callback: a dossier compiled from the other frame must never stay on screen."""
    st.session_state.last_image_mode = st.session_state.aoi_kind
    st.session_state.investigation_result = None
    st.session_state.sealed = None
    st.session_state.investigate_error = None
    st.session_state.selected_option = "OPTION_A"
    st.session_state.frame_changed = True


kind_labels = {"defective": "Defective (ambiguous)", "healthy": "Healthy (nominal)"}

run_col, _ = st.columns([1, 3])
if run_col.button("▶ RUN INVESTIGATION", type="primary", use_container_width=True):
    ss.sealed = None
    ss.investigate_error = None
    ss.frame_changed = False
    asset = images[ss.aoi_kind]["filename"]
    with st.spinner("Fusing SCADA · shift log · AOI against SOP-SMT-4402 …"):
        try:
            ss.investigation_result = api(
                "POST", "/incidents/investigate", timeout=150,
                json={"aoi_asset": asset, "log_id": log_id,
                      "line_params": {"run_rate_units_per_hr": run_rate,
                                      "unit_scrap_cost_usd": scrap_cost,
                                      "risk_horizon_hrs": horizon},
                      "gemini_model": model_id or None,
                      "gemini_api_key": api_key_in or None},
            )
        except ApiError as exc:
            ss.investigation_result = None
            ss.investigate_error = exc

if ss.investigate_error:
    e = ss.investigate_error
    st.error(f"**{e.error}** — {e.detail}" + (f"  \n_{e.hint}_" if e.hint else ""))

if ss.frame_changed and ss.investigation_result is None:
    st.info(f"AOI frame switched to **{kind_labels[ss.aoi_kind]}** — previous dossier cleared. "
            "Run a new investigation for this frame.")

res = ss.investigation_result
dossier = res["dossier"] if res else None

# --------------------------------------------------------------------------- #
# Three panes
# --------------------------------------------------------------------------- #
left, centre, right = st.columns([1.05, 1.3, 1.3], gap="medium")

# ---- LEFT: AOI viewer ------------------------------------------------------
with left:
    st.markdown(f'<div class="tf-panel" style="margin-bottom:.4rem"><h4>AOI Inspection Feed</h4></div>',
                unsafe_allow_html=True)
    st.radio("Frame", list(kind_labels), format_func=kind_labels.get, key="aoi_kind", horizontal=True,
             label_visibility="collapsed", on_change=on_image_mode_change)
    img = images[ss.aoi_kind]
    b64 = base64.b64encode(img["svg"].encode("utf-8")).decode("ascii")
    roi_html = ""
    # The defective SVG already draws its own ROI_01 box and label: adding ours would double-draw it.
    if img["roi"] and not svg_has_embedded_roi(img["svg"]):
        vx, vy, vw, vh = img["view_box"]
        r = img["roi"]
        roi_html = (
            f'<div class="tf-roi" style="left:{(r["x"]-vx)/vw*100:.3f}%;top:{(r["y"]-vy)/vh*100:.3f}%;'
            f'width:{r["width"]/vw*100:.3f}%;height:{r["height"]/vh*100:.3f}%">'
            f'<span class="tf-roi-tag">ROI · ΔE {img["delta_e_max"]}</span></div>'
        )
    st.markdown(
        f'<div class="tf-aoi"><img alt="{esc(img["label"])}" src="data:image/svg+xml;base64,{b64}"/>{roi_html}</div>',
        unsafe_allow_html=True,
    )
    is_def = img["kind"] == "defective"
    panel("Frame telemetry",
          kv("Asset", img["filename"]) + kv("Camera", img["camera"]) + kv("Status", img["status"])
          + kv("ΔE max", img["delta_e_max"]) + kv("SOP §4.2.1 threshold", "> 3.0")
          + f'<div style="margin-top:.5rem">{chip("ROI FLAGGED" if is_def else "NO ROI · NOMINAL", "warn" if is_def else "ok")}</div>')

# ---- CENTRE: citation evidence --------------------------------------------
with centre:
    if not dossier:
        panel("Citation Evidence", '<div style="color:%s">Run an investigation to compile the evidence dossier. '
              'Every claim will be traced to a verbatim SOP quotation.</div>' % MUTED)
    else:
        cit = dossier["grounded_sop_citation"]
        sig = dossier["anomaly_signature"]
        gate = dossier["safety_gate"]
        ok = cit["citation_valid"]
        panel(
            "Citation Evidence · SOP Grounding",
            kv("SOP document", cit["sop_document_id"]) + kv("Section", cit["section"])
            + kv("Page", cit["page_number"])
            + f'<div class="tf-quote">“{esc(cit["verbatim_quote"])}”</div>'
            + chip("VERBATIM CITATION VERIFIED" if ok else "CITATION NOT VERIFIED", "ok" if ok else "bad"),
        )
        panel(
            "Anomaly Signature",
            kv("Classification", sig["classification"])
            + kv("Confidence", f'{sig["confidence_score"]:.1%} (gate ≥ 90%)')
            + kv("Optical ΔE", sig["optical_delta_e"]) + kv("1.8 kHz vibration drift", sig["vibration_drift_pct"]),
        )
        c = dossier["cross_silo_correlation"]
        panel(
            "Cross-silo Correlation",
            f'<div class="tf-silo"><b>SCADA</b>{esc(c["scada_state"])}</div>'
            f'<div class="tf-silo"><b>SHIFT LOG</b>{esc(c["shift_log_finding"])}</div>'
            f'<div class="tf-silo"><b>AOI</b>{esc(c["aoi_visual_finding"])}</div>'
            f'<div class="tf-silo" style="border-bottom:none"><b>SYNTHESIS</b>{esc(c["synthesis_summary"])}</div>',
        )
        rows = "".join(
            f'<div class="tf-kv"><span>{esc(g_["name"])}</span><span>'
            f'{chip("PASS" if g_["passed"] else "FAIL", "ok" if g_["passed"] else "bad")}</span></div>'
            f'<div style="color:{MUTED};font-size:.74rem;padding:.1rem 0 .3rem">{esc(g_["detail"])}</div>'
            for g_ in res["gate_checks"]
        )
        panel("Safety Gate",
              f'<div style="margin-bottom:.4rem">{chip(gate["status"], "ok" if gate["status"].startswith("PASSED") else "bad")} '
              f'{chip("PLC OVERRIDE BLOCKED", "info")} {chip("BADGE AUTH REQUIRED", "info")}</div>' + rows)

# ---- RIGHT: trade-off matrix ----------------------------------------------
with right:
    if not dossier:
        panel("Decision Matrix", f'<div style="color:{MUTED}">Option A vs Option B trade-offs appear here.</div>')
    else:
        coi = dossier["cost_of_inaction"]
        met = res["meta"]["cost_formula"]["metrics"]
        # Two DIFFERENT exposures - never summed or presented as one number.
        panel(
            "Cost Exposure",
            f'<div style="display:flex;gap:.7rem">'
            f'<div style="flex:1;border:1px solid {AMBER};border-radius:4px;padding:.6rem .7rem">'
            f'<div style="color:{MUTED};font-size:.7rem;letter-spacing:.08em">CARD A · IMMEDIATE BATCH AT RISK</div>'
            f'<div class="tf-big">{usd0(met["immediate_batch_scrap_usd"])}</div>'
            f'<div style="font-size:.8rem">({met["immediate_batch_units"]:,} units in process)</div></div>'
            f'<div style="flex:1;border:1px solid {RED};border-radius:4px;padding:.6rem .7rem">'
            f'<div style="color:{MUTED};font-size:.7rem;letter-spacing:.08em">CARD B · PROJECTED {coi["risk_window_hours"]}H INACTION EXPOSURE</div>'
            f'<div class="tf-big" style="color:{RED}">{usd0(met["catastrophic_exposure_48h_usd"])}</div>'
            f'<div style="font-size:.8rem">(Seizure Horizon · scrap only)</div></div></div>'
            + f'<div style="margin-top:.6rem">'
            + kv("Bearing-seizure downtime (unplanned)", usd(met["downtime_interruption_loss_usd"]))
            + kv("Total inaction exposure (B + downtime)", usd(coi["total_financial_exposure_usd"]))
            + '</div>',
        )
        opts = {o["option_id"]: o for o in dossier["decision_options"]}
        det = res["meta"]["cost_formula"]["options"]
        a, b = opts["OPTION_A"], opts["OPTION_B"]
        da, db = det["option_a"], det["option_b"]
        net_a = da["scrap_usd_avoided"] - a["projected_cost_usd"]
        net_b = db["scrap_usd_avoided"] - b["projected_cost_usd"]
        # The radio below owns this key; Streamlit has already applied the click before this script
        # run, so reading it here highlights the CURRENT choice in the same render pass.
        sel = ss.selected_option
        ca = ' class="sel"' if sel == "OPTION_A" else ""
        cb = ' class="sel"' if sel == "OPTION_B" else ""
        best = "OPTION_A" if net_a >= net_b else "OPTION_B"
        matrix = (
            '<table class="tf-matrix"><tr><th>METRIC</th><th>OPTION A · FULL STOP</th><th>OPTION B · DERATE 25%</th></tr>'
            f'<tr><td class="metric">SOP action (verbatim §4.2.2)</td><td{ca}>{esc(a["title"])}</td><td{cb}>{esc(b["title"])}</td></tr>'
            f'<tr><td class="metric">Line pause</td><td{ca}>{a["line_pause_min"]} min</td><td{cb}>None · {db["derate_pct"]:.0f}% slower for ≤ {db["derate_window_hrs"]:.0f} h</td></tr>'
            f'<tr><td class="metric">Immediate batch</td><td{ca}><b>Saves {usd0(met["immediate_batch_scrap_usd"])} in scrap immediately '
            f'for a {usd0(met["option_a_pause_cost_usd"])} downtime cost</b></td>'
            f'<td{cb}>Batch keeps running · {db["residual_scrap_units"]:,.0f} residual scrap units in window</td></tr>'
            f'<tr><td class="metric">Projected cost</td><td{ca}>{usd(a["projected_cost_usd"])}</td><td{cb}>{usd(b["projected_cost_usd"])}</td></tr>'
            f'<tr><td class="metric">Scrap avoided over {coi["risk_window_hours"]} h (units)</td><td{ca}>{a["scrap_avoided_units"]:,}</td><td{cb}>{b["scrap_avoided_units"]:,}</td></tr>'
            f'<tr><td class="metric">Scrap avoided over {coi["risk_window_hours"]} h ($)</td><td{ca}>{usd(da["scrap_usd_avoided"])}</td><td{cb}>{usd(db["scrap_usd_avoided"])}</td></tr>'
            f'<tr><td class="metric"><b>Net benefit</b></td><td{ca}><b>{usd(net_a)}</b></td><td{cb}><b>{usd(net_b)}</b></td></tr>'
            '</table>'
        )
        panel("Trade-off Matrix", matrix + f'<div style="margin-top:.6rem">{chip("HIGHEST NET BENEFIT: " + best.replace("_", " "), "warn")} '
              f'{chip("SELECTED: " + sel.replace("_", " "), "info")}</div>'
              f'<div style="color:{MUTED};font-size:.72rem;margin-top:.5rem">Option parameters (15 min, 25 %, 4 h) are read from the SOP; '
              f'costs use the deterministic cost model (planned-stop rate ${res["meta"]["cost_formula"]["planned_pause_cost_per_hr_usd"]:,.0f}/h). '
              f'Decision support only - the SOP, not this tool, authorises the action.</div>')

# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #
if res:
    for w in res["meta"]["warnings"]:
        st.warning(w)
    with st.expander("Engine trace & cost-formula audit"):
        m = res["meta"]
        st.json({"engine": m["engine"], "model": m["model"], "attempts": m["attempts"],
                 "fields_overridden_by_deterministic_backend": m["overrides"], "cost_formula": m["cost_formula"]})
        with st.expander("Raw dossier JSON (schema.json)"):
            st.json(dossier)

# --------------------------------------------------------------------------- #
# Bottom: human approval checkpoint
# --------------------------------------------------------------------------- #
st.markdown("---")
st.markdown(f'<div class="tf-panel" style="border-top-color:{AMBER}"><h4 style="color:{AMBER}">'
            'Human Approval Checkpoint</h4><div style="color:#c9d4ea;font-size:.86rem">'
            'TraceForge never commands the PLC. Sealing records <b>your</b> decision, badge and the full evidence '
            'dossier in a SHA-256 hash-chained audit ledger; the action itself is executed by you at the line.</div></div>',
            unsafe_allow_html=True)

if not dossier:
    st.info("Awaiting investigation.")
else:
    gate_ok = dossier["safety_gate"]["status"] == "PASSED_CONFIRMATION_REQUIRED"
    if not gate_ok:
        st.error("Safety gate REJECTED this dossier (ungrounded or low confidence). Sign-off is disabled.")
    ap_l, ap_m, ap_r = st.columns([1.2, 1.2, 1.2])
    with ap_l:
        badge = st.text_input("Operator Badge ID", placeholder="TECH_8412_DESHMUKH", max_chars=64, disabled=not gate_ok,
                              help="Format: (TECH|ENG|MGR)_NNNN_NAME")
    with ap_m:
        st.radio("Selected action", ["OPTION_A", "OPTION_B"], key="selected_option", horizontal=True,
                 format_func=lambda o: {"OPTION_A": "Option A · Full stop", "OPTION_B": "Option B · Derate 25%"}[o],
                 disabled=not gate_ok)
    with ap_r:
        comment = st.text_input("Comment (optional)", max_chars=500, disabled=not gate_ok)

    already = ss.sealed is not None and ss.sealed["ledger_record"]["incident_id"] == dossier["incident_id"] \
        and ss.sealed["ledger_record"]["selected_option_id"] == ss.selected_option
    if st.button("Approve Action & Seal Audit Ledger", type="primary", disabled=not gate_ok or already):
        if not BADGE_RE.match(badge.strip()):
            st.error("Badge ID must look like TECH_8412_DESHMUKH — (TECH|ENG|MGR)_4 digits_NAME in capitals.")
        else:
            try:
                ss.sealed = api("POST", "/incidents/signoff", json={
                    "operator_badge_id": badge.strip(),
                    "selected_option_id": ss.selected_option,
                    "dossier": dossier,
                    "signature": res["signature"],
                    "comment": comment or None,
                })
                st.rerun()
            except ApiError as exc:
                st.error(f"**{exc.error}** — {exc.detail}")

    if ss.sealed:
        rec = ss.sealed["ledger_record"]
        st.markdown(
            f'<div class="tf-seal"><b style="color:{GREEN}">SEALED · ledger sequence #{rec["sequence"]}</b><br>'
            f'{esc(ss.sealed["message"])}<br><br>'
            f'<span style="color:{MUTED}">operator</span> {esc(rec["operator_badge_id"])} · '
            f'<span style="color:{MUTED}">action</span> {esc(rec["selected_option_id"])} · '
            f'<span style="color:{MUTED}">at</span> {esc(rec["recorded_at"])}<br>'
            f'<span style="color:{MUTED}">dossier SHA-256</span><div class="tf-hash">{esc(rec["dossier_sha256"])}</div>'
            f'<span style="color:{MUTED}">prev hash</span><div class="tf-hash">{esc(rec["prev_hash"])}</div>'
            f'<span style="color:{MUTED}">record SHA-256</span><div class="tf-hash">{esc(rec["record_hash"])}</div></div>',
            unsafe_allow_html=True,
        )
