"""Streamlit cockpit tests (AppTest) driven against the real FastAPI app in-process."""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest
import requests
from streamlit.testing.v1 import AppTest

from backend.config import get_settings

APP = str(Path(__file__).resolve().parent.parent / "frontend" / "app.py")
RUN = "▶ RUN INVESTIGATION"
SEAL = "Approve Action & Seal Audit Ledger"


@pytest.fixture()
def app(client, monkeypatch):
    """Route the UI's `requests` calls into the in-process FastAPI app."""

    def fake_request(method, url, **kw):
        return client.request(method, url.replace("http://127.0.0.1:8000", ""), json=kw.get("json"))

    monkeypatch.setattr(requests, "request", fake_request)
    at = AppTest.from_file(APP, default_timeout=60).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def html(at) -> str:
    return " ".join(m.value for m in at.markdown)


def click(at, label):
    next(b for b in at.button if b.label == label).click()
    return at.run()


def highlighted(at) -> list[str]:
    return re.findall(r'<td class="sel">([^<]*)', html(at))


def embedded_svg(at) -> str:
    m = re.search(r"data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)", html(at))
    return base64.b64decode(m.group(1)).decode()


# ------------------------------------------------------------------------- initial state
def test_session_state_initialised_before_widgets(app):
    ss = app.session_state
    for key in ("selected_option", "investigation_result", "last_image_mode"):
        assert key in ss
    assert ss["selected_option"] == "OPTION_A" and ss["investigation_result"] is None
    assert ss["last_image_mode"] == "defective"


# --------------------------------------------------------- option highlight (same render pass)
def test_matrix_highlight_follows_radio_in_same_pass(app):
    click(app, RUN)
    assert highlighted(app)[0].startswith("Option A (Full Stop)")
    for choice, marker in (("OPTION_B", "Option B (Derated"), ("OPTION_A", "Option A (Full Stop)"),
                           ("OPTION_B", "Option B (Derated")):
        app.radio(key="selected_option").set_value(choice).run()
        assert highlighted(app) and highlighted(app)[0].startswith(marker), (choice, highlighted(app)[:1])
        assert f"SELECTED: {choice.replace('_', ' ')}" in html(app)


# ---------------------------------------------------------------- stale result clearing
def test_toggling_aoi_clears_stale_investigation(app):
    click(app, RUN)
    assert app.session_state["investigation_result"] is not None
    assert "Citation Evidence · SOP Grounding" in html(app)

    app.radio(key="aoi_kind").set_value("healthy").run()
    assert app.session_state["investigation_result"] is None
    assert app.session_state["last_image_mode"] == "healthy"
    h = html(app)
    assert "Citation Evidence · SOP Grounding" not in h and "Trade-off Matrix" not in h
    assert any("previous dossier cleared" in i.value for i in app.info)
    assert not [b for b in app.button if b.label == SEAL]  # no sign-off without a fresh dossier

    click(app, RUN)  # fresh investigation for the new frame
    res = app.session_state["investigation_result"]
    assert res["meta"]["aoi_asset"] == "aoi_healthy_sample.svg"
    assert res["dossier"]["safety_gate"]["status"] == "REJECTED_UNGROUNDED"


def test_toggle_back_and_forth_never_shows_other_frames_dossier(app):
    click(app, RUN)
    for kind in ("healthy", "defective", "healthy"):
        app.radio(key="aoi_kind").set_value(kind).run()
        assert app.session_state["investigation_result"] is None


def test_toggle_resets_selection_and_sealed_banner(app):
    click(app, RUN)
    app.radio(key="selected_option").set_value("OPTION_B").run()
    app.radio(key="aoi_kind").set_value("healthy").run()
    assert app.session_state["selected_option"] == "OPTION_A" and app.session_state["sealed"] is None


# ------------------------------------------------------------------------- single ROI box
def test_defective_frame_draws_exactly_one_roi_box(app):
    assert embedded_svg(app).count("ROI_01") == 1  # the SVG's own box + label
    assert 'class="tf-roi"' not in html(app)  # our overlay is suppressed -> no duplicate


def test_healthy_frame_has_no_roi_at_all(app):
    app.radio(key="aoi_kind").set_value("healthy").run()
    assert "ROI_" not in embedded_svg(app) and 'class="tf-roi"' not in html(app)


def test_roi_detector_distinguishes_svgs():
    """Suppression is conditional on the SVG's content, not a blanket removal of the overlay."""
    src = Path(APP).read_text()
    fn = re.search(r"def svg_has_embedded_roi.*?\n\n\n", src, re.S).group(0)
    ns: dict = {"re": re}
    exec(fn, ns)
    assert ns["svg_has_embedded_roi"]("<svg><text>ROI_01: x</text></svg>") is True
    assert ns["svg_has_embedded_roi"]('<svg><rect stroke="#00f3ff"/><text>CAM_04</text></svg>') is False


# ------------------------------------------------------------------------------ cost cards
def test_two_separate_cost_cards_with_exact_numbers(app):
    click(app, RUN)
    h = html(app)
    assert "CARD A · IMMEDIATE BATCH AT RISK" in h and "$3,600" in h and "(120 units in process)" in h
    assert "CARD B · PROJECTED 48H INACTION EXPOSURE" in h and "$62,899" in h and "Seizure Horizon" in h
    assert "$42,500.00" in h and "$105,399.20" in h
    before_b = h.split("CARD B")[0]
    assert "$3,600" in before_b and "$62,899" not in before_b  # two distinct figures, not merged


def test_matrix_states_option_a_saves_3600_for_750(app):
    click(app, RUN)
    h = html(app)
    assert "Saves $3,600 in scrap immediately for a $750 downtime cost" in h
    assert "$750.00" in h


# --------------------------------------------------------------------------- sign-off flow
def test_signoff_sends_signature_and_seals(app):
    click(app, RUN)
    app.text_input[0].set_value("TECH_8412_DESHMUKH").run()
    click(app, SEAL)
    assert not app.error, [e.value for e in app.error]
    rec = app.session_state["sealed"]["ledger_record"]
    assert rec["operator_badge_id"] == "TECH_8412_DESHMUKH" and rec["sequence"] == 1
    assert rec["payload"]["dossier_signature"] == app.session_state["investigation_result"]["signature"]
    assert "SEALED" in html(app)


@pytest.mark.parametrize("bad", ["", "TECH_8412", "evil", "tech_8412_deshmukh"])
def test_invalid_badge_blocked_client_side(app, bad):
    click(app, RUN)
    app.text_input[0].set_value(bad).run()
    click(app, SEAL)
    assert app.error and "Badge ID must look like" in app.error[0].value
    assert app.session_state["sealed"] is None
    assert not get_settings().ledger_path.exists() or get_settings().ledger_path.stat().st_size == 0


def test_tampered_signature_refused_by_server_and_shown(app):
    click(app, RUN)
    res = dict(app.session_state["investigation_result"])
    res["signature"] = "0" * 64
    app.session_state["investigation_result"] = res
    app.run()
    app.text_input[0].set_value("TECH_8412_DESHMUKH").run()
    click(app, SEAL)
    assert app.error and "DOSSIER_SIGNATURE_INVALID" in app.error[0].value
    assert app.session_state["sealed"] is None


def test_rejected_dossier_disables_signoff(app):
    app.radio(key="aoi_kind").set_value("healthy").run()
    click(app, RUN)
    assert next(b for b in app.button if b.label == SEAL).disabled
    assert any("REJECTED" in e.value for e in app.error)


def test_backend_down_shows_clean_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    monkeypatch.setattr(requests, "request", boom)
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert at.error and "BACKEND_UNREACHABLE" in at.error[0].value


def test_sidebar_model_dropdown_and_key_field(app):
    sel = next(s for s in app.sidebar.selectbox if s.label == "Model")
    assert sel.value == "gemini-3.8-flash"  # best model preselected
    assert any(o.startswith("Gemini 3.1 Flash-Lite") for o in sel.options) and "Custom model ID…" in sel.options
    key = next(t for t in app.sidebar.text_input if t.label == "Gemini API key")
    assert key.proto.type == 1  # PASSWORD: masked in the browser


def test_run_sends_selected_model_and_key(app, monkeypatch):
    sent = {}
    orig = requests.request

    def spy(method, url, **kw):
        if url.endswith("/incidents/investigate"):
            sent.update(kw["json"])
        return orig(method, url, **kw)

    monkeypatch.setattr(requests, "request", spy)
    next(s for s in app.sidebar.selectbox if s.label == "Model").select("gemini-3.1-flash-lite")
    next(t for t in app.sidebar.text_input if t.label == "Gemini API key").input("AQ.secret-123")
    app.run()
    click(app, RUN)
    assert sent["gemini_model"] == "gemini-3.1-flash-lite" and sent["gemini_api_key"] == "AQ.secret-123"
    assert "AQ.secret-123" not in html(app)  # never rendered back into the page
