"""Unit tests: cost model, SOP grounding, and Gemini agent retry/validation with a fake client."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.config import get_settings
from backend.models.incident import InvestigateRequest, LineParams
from backend.services import cost_model, investigator
from backend.services.evidence import extract_log_facts, load_aoi
from backend.services.gemini_agent import AgentFailure, generate_dossier, offline_rules_engine
from backend.services.sop_grounding import load_sop


@pytest.fixture()
def sop(env):
    return load_sop(str(get_settings().sop_md_path))


# ------------------------------------------------------------------ cost model
def test_scrap_formula_line_4b_defaults():
    inp = cost_model.CostInputs.from_params(LineParams(), 0.182, 5000.0, 8.5)
    assert cost_model.scrap_units(inp) == pytest.approx(240 * 0.182 * 48)
    assert cost_model.scrap_risk_usd(inp) == pytest.approx(240 * 0.182 * 30 * 48)


def test_parse_drift():
    assert cost_model.parse_drift_fraction("+18.2%") == pytest.approx(0.182)
    with pytest.raises(cost_model.CostModelError):
        cost_model.parse_drift_fraction("n/a")


def test_option_parameters_come_from_sop(sop):
    opts = sop.execution_options()
    assert opts["OPTION_A"].pause_min == 15
    assert opts["OPTION_B"].derate_pct == 25.0 and opts["OPTION_B"].derate_max_hours == 4.0


# -------------------------------------------------------------------- grounding
def test_page_of_section_4_2(sop):
    assert sop.page_of("4.2") == 4 and sop.document_id == "SOP-SMT-4402-REV4"


def test_markdown_insensitive_but_otherwise_verbatim(sop):
    q = sop.directive_quote()
    assert sop.verify_citation("SOP-SMT-4402-REV4.pdf", "Section 4.2", 4, q).valid
    assert sop.verify_citation("SOP-SMT-4402-REV4", "4.2", 4, "**" + q + "**").valid
    assert sop.verify_citation("SOP-SMT-4402_HighSpeed_Feeder.pdf", "§4.2.1", 4, q).valid


@pytest.mark.parametrize(
    "doc,section,page,quote",
    [
        ("SOP-SMT-9999.pdf", "Section 4.2", 4, "Residue discoloration exceeding Delta-E > 3.0 indicates seal degradation."),
        ("SOP-SMT-4402-REV4.pdf", "Section 2.1", 2, "An absolute RMS reading below 4.5 mm/s does NOT guarantee mechanical integrity."),
        ("SOP-SMT-4402-REV4.pdf", "Section 4.2", 3, "Residue discoloration exceeding Delta-E > 3.0 indicates seal degradation."),
        ("SOP-SMT-4402-REV4.pdf", "Section 4.2", 4, "Residue discoloration exceeding Delta-E > 2.0 indicates seal degradation."),
        ("SOP-SMT-4402-REV4.pdf", "Section 4.2", 4, ""),
        ("SOP-SMT-4402-REV4.pdf", "garbage", 4, "x"),
    ],
)
def test_citation_rejections(sop, doc, section, page, quote):
    assert not sop.verify_citation(doc, section, page, quote).valid


# ------------------------------------------------------------------- gemini agent
class FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(text=reply)


class FakeClient:
    def __init__(self, replies):
        self.models = FakeModels(replies)


def _good_reply(settings, sop):
    aoi = load_aoi(settings, "aoi_defective_sample.svg")
    logf = extract_log_facts(settings, None)
    auth, _, _ = investigator._authoritative(settings, sop, aoi, logf, InvestigateRequest())
    return offline_rules_engine(settings, sop, aoi, logf, auth).dossier.model_dump_json(), aoi, logf, auth


def test_structured_output_config_enforced(env, sop):
    s = get_settings()
    reply, aoi, logf, auth = _good_reply(s, sop)
    client = FakeClient([reply])
    res = generate_dossier(s, sop, aoi, logf, auth, client=client)
    cfg = client.models.calls[0]["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema["title"] == "TraceForgeIncidentSynthesis"
    assert "example" not in json.dumps(cfg.response_schema) and "$schema" not in cfg.response_schema
    assert cfg.temperature == 0.0
    assert res.engine == "gemini" and res.attempts == 1


def test_retry_on_schema_violation_then_success(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    s = get_settings()
    reply, aoi, logf, auth = _good_reply(s, sop)
    client = FakeClient(["not json at all", json.dumps({"incident_id": "x"}), reply])
    res = generate_dossier(s, sop, aoi, logf, auth, client=client)
    assert res.attempts == 3
    # error feedback was supplied on the retry
    last_parts = client.models.calls[-1]["contents"][0].parts
    assert any("rejected for these reasons" in (p.text or "") for p in last_parts)


def test_clean_failure_after_exhausting_retries(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    s = get_settings()
    _, aoi, logf, auth = _good_reply(s, sop)
    client = FakeClient(["{}"] * 3)
    with pytest.raises(AgentFailure) as ei:
        generate_dossier(s, sop, aoi, logf, auth, client=client)
    assert ei.value.attempts == 3 and "schema-valid" in str(ei.value)


def test_retry_when_citation_not_verbatim(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    s = get_settings()
    reply, aoi, logf, auth = _good_reply(s, sop)
    bad = json.loads(reply)
    bad["grounded_sop_citation"]["verbatim_quote"] = "Seal degradation is fine under Delta-E 10."
    client = FakeClient([json.dumps(bad), reply])
    res = generate_dossier(s, sop, aoi, logf, auth, client=client)
    assert res.attempts == 2


def test_investigate_overrides_hallucinated_numbers_and_gates_bad_citation(env, sop, monkeypatch):
    """Model lies about cost, Delta-E, options and cites a fake quote on every attempt."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    s = get_settings()
    reply, *_ = _good_reply(s, sop)
    lie = json.loads(reply)
    lie["cost_of_inaction"]["projected_scrap_cost_usd"] = 1.0
    lie["anomaly_signature"]["optical_delta_e"] = 9.99
    lie["anomaly_signature"]["confidence_score"] = 0.97
    lie["decision_options"][0]["scrap_avoided_units"] = 99999
    lie["grounded_sop_citation"]["verbatim_quote"] = "Invented procedure."
    client = FakeClient([json.dumps(lie)] * 3)
    res = investigator.investigate(s, sop, InvestigateRequest(), client=client)
    d = res.dossier
    assert d.cost_of_inaction.projected_scrap_cost_usd == 62899.2
    assert d.anomaly_signature.optical_delta_e == 3.82
    assert d.decision_options[0].scrap_avoided_units != 99999
    assert {"cost_of_inaction", "anomaly_signature.optical_delta_e", "decision_options"} <= set(res.meta.overrides)
    assert d.safety_gate.status == "REJECTED_UNGROUNDED"
    assert d.grounded_sop_citation.citation_valid is False


def test_model_cannot_self_approve_low_confidence(env, sop):
    s = get_settings()
    reply, *_ = _good_reply(s, sop)
    low = json.loads(reply)
    low["anomaly_signature"]["confidence_score"] = 0.72
    low["safety_gate"]["status"] = "PASSED_CONFIRMATION_REQUIRED"
    res = investigator.investigate(s, sop, InvestigateRequest(), client=FakeClient([json.dumps(low)]))
    assert res.dossier.safety_gate.status == "REJECTED_UNGROUNDED"


# ------------------------------------------------------------ P1: schema sanitizer
def _all_keys(node, acc=None):
    acc = set() if acc is None else acc
    if isinstance(node, dict):
        for k, v in node.items():
            acc.add(k)
            _all_keys(v, acc)
    elif isinstance(node, list):
        for v in node:
            _all_keys(v, acc)
    return acc


def test_sanitizer_strips_unsupported_keywords_recursively():
    from backend.services.gemini_agent import sanitize_schema_for_gemini

    raw = {
        "$schema": "x", "type": "object", "default": 1,
        "properties": {
            "timestamp": {"type": "string", "format": "date-time"},
            "opts": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "object", "default": {}}},
        },
    }
    clean = sanitize_schema_for_gemini(raw)
    assert _all_keys(clean).isdisjoint({"$schema", "format", "minItems", "maxItems", "default"})
    assert clean["properties"]["opts"]["items"] == {"type": "object"}
    assert raw["properties"]["timestamp"]["format"] == "date-time"  # input not mutated


def test_sanitizer_keeps_property_names_that_collide_with_keywords():
    from backend.services.gemini_agent import sanitize_schema_for_gemini

    clean = sanitize_schema_for_gemini(
        {"type": "object", "properties": {"format": {"type": "string", "format": "x"}, "default": {"type": "string"}}}
    )
    assert set(clean["properties"]) == {"format", "default"}
    assert clean["properties"]["format"] == {"type": "string"}


def test_real_schema_is_clean_but_server_validation_stays_strict(env):
    from backend.services.gemini_agent import gemini_response_schema, validate_reply

    s = get_settings()
    sent = gemini_response_schema(s)
    assert _all_keys(sent).isdisjoint({"$schema", "format", "minItems", "maxItems", "default", "example"})
    assert sent["properties"]["decision_options"]["type"] == "array"
    # server side: min/max items + enum are still enforced by jsonschema and Pydantic
    sop = load_sop(str(s.sop_md_path))
    good, aoi, logf, auth = _good_reply(s, sop)
    one_option = json.loads(good)
    one_option["decision_options"] = one_option["decision_options"][:1]
    dossier, errors = validate_reply(s, json.dumps(one_option))
    assert dossier is None and any("decision_options" in e for e in errors)
    bad_enum = json.loads(good)
    bad_enum["safety_gate"]["status"] = "YOLO"
    assert validate_reply(s, json.dumps(bad_enum))[0] is None


# --------------------------------------------------- P1: transient-failure fallback
def _server_error():
    from google.genai import errors

    return errors.ServerError(503, {"error": {"message": "unavailable", "status": "UNAVAILABLE"}})


def _rate_limit():
    from google.genai import errors

    return errors.ClientError(429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}})


def test_rate_limit_falls_back_to_deterministic_engine(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    res = investigator.investigate(get_settings(), sop, InvestigateRequest(),
                                   client=FakeClient([_rate_limit(), _rate_limit(), _rate_limit()]))
    assert res.meta.engine == "offline_deterministic_fallback"
    assert any("DETERMINISTIC FALLBACK" in w for w in res.meta.warnings)
    assert res.dossier.safety_gate.status == "PASSED_CONFIRMATION_REQUIRED"  # schema-compliant, gate-verified
    IncidentDossier_ = type(res.dossier)
    IncidentDossier_.model_validate(res.dossier.model_dump())
    assert len(res.signature) == 64


def test_network_error_falls_back(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    res = investigator.investigate(get_settings(), sop, InvestigateRequest(),
                                   client=FakeClient([ConnectionError("dns"), TimeoutError("t"), _server_error()]))
    assert res.meta.engine == "offline_deterministic_fallback"


def test_fallback_output_is_deterministic(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    outs = []
    for _ in range(2):
        r = investigator.investigate(get_settings(), sop, InvestigateRequest(),
                                     client=FakeClient([_server_error()] * 3))
        outs.append(json.dumps(r.dossier.model_dump(), sort_keys=True))
    assert outs[0] == outs[1]


def test_auth_error_is_not_masked_by_fallback(env, sop, monkeypatch):
    from google.genai import errors

    monkeypatch.setattr("time.sleep", lambda *_: None)
    forbidden = errors.ClientError(403, {"error": {"message": "denied", "status": "PERMISSION_DENIED"}})
    with pytest.raises(AgentFailure) as ei:
        investigator.investigate(get_settings(), sop, InvestigateRequest(), client=FakeClient([forbidden]))
    assert not ei.value.transient


def test_bad_model_output_is_not_masked_by_fallback(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(AgentFailure) as ei:
        investigator.investigate(get_settings(), sop, InvestigateRequest(), client=FakeClient(["{}"] * 3))
    assert not ei.value.transient


def test_fallback_can_be_disabled(env, sop, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setenv("GEMINI_FALLBACK_ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(AgentFailure):
        investigator.investigate(get_settings(), sop, InvestigateRequest(), client=FakeClient([_server_error()] * 3))


# ------------------------------------------------------------------ P1: provider choice
def test_provider_selected_from_environment(env, monkeypatch):
    s = get_settings()
    assert s.use_vertex and not s.gemini_configured
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    get_settings.cache_clear()
    assert get_settings().use_vertex and get_settings().gemini_configured
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    get_settings.cache_clear()
    assert not get_settings().use_vertex and get_settings().gemini_configured  # AI Studio wins


# --------------------------------------------------------------------- P2: HMAC unit
def test_hmac_roundtrip_and_canonicalisation():
    a = {"b": 1, "a": {"y": 2, "x": [1, 2]}}
    b = {"a": {"x": [1, 2], "y": 2}, "b": 1}  # same content, different key order
    sig = investigator.sign_dossier(a, "k")
    assert len(sig) == 64 and sig == investigator.sign_dossier(b, "k")
    assert investigator.verify_dossier_signature(b, sig, "k")


def test_hmac_rejects_tamper_wrong_key_and_garbage():
    d = {"a": 1}
    sig = investigator.sign_dossier(d, "k")
    assert not investigator.verify_dossier_signature({"a": 2}, sig, "k")
    assert not investigator.verify_dossier_signature(d, sig, "other")
    assert not investigator.verify_dossier_signature(d, "", "k")
    assert not investigator.verify_dossier_signature(d, None, "k")  # type: ignore[arg-type]


def test_signing_key_never_defaults_to_a_known_constant(env, monkeypatch):
    monkeypatch.delenv("TRACEFORGE_SIGNING_KEY", raising=False)
    get_settings.cache_clear()
    k1 = get_settings().signing_key
    get_settings.cache_clear()
    k2 = get_settings().signing_key
    assert get_settings().signing_key_ephemeral and len(k1) == 64 and "interrupt" not in k1.lower()
    assert k1 != "firmware-secret-interrupt0xff"
    assert k1 != k2  # random per settings load, never a shared constant


# --------------------------------------------------------------- P3: cost metrics
def test_cost_metrics_match_brief():
    inp = cost_model.CostInputs.from_params(LineParams(), 0.182, 5000.0, 8.5)
    m = cost_model.compute_cost_metrics(inp, pause_min=15)
    assert m["immediate_batch_units"] == 120 and m["immediate_batch_scrap_usd"] == 3600.0
    assert m["catastrophic_exposure_48h_usd"] == 62899.2
    assert m["downtime_interruption_loss_usd"] == 42500.0
    assert m["total_financial_exposure_usd"] == 105399.2
    assert m["option_a_pause_cost_usd"] == 750.0 and m["option_a_immediate_net_usd"] == 2850.0


def test_immediate_batch_scales_with_run_rate_and_scrap_cost():
    inp = cost_model.CostInputs.from_params(
        LineParams(run_rate_units_per_hr=100, unit_scrap_cost_usd=10, risk_horizon_hrs=24), 0.1, 5000.0, 8.5
    )
    m = cost_model.compute_cost_metrics(inp, 15)
    assert m["immediate_batch_units"] == 50 and m["immediate_batch_scrap_usd"] == 500.0
    assert m["catastrophic_exposure_48h_usd"] == round(100 * 0.1 * 10 * 24, 2)  # key name is historical; horizon-driven


def test_option_a_costs_planned_pause_not_unplanned_downtime(env, sop):
    inp = cost_model.CostInputs.from_params(LineParams(), 0.182, 5000.0, 8.5)
    opts, _ = cost_model.compute_decision_options(inp, sop.execution_options())
    assert opts[0].projected_cost_usd == 750.0  # not 15/60*5000 = 1250


def test_not_configured_is_clean_503(env, monkeypatch):
    monkeypatch.setenv("TRACEFORGE_OFFLINE_MODE", "false")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from backend.main import app

    r = TestClient(app).post("/incidents/investigate", json={})
    assert r.status_code == 503 and r.json()["error"] == "GEMINI_NOT_CONFIGURED"
