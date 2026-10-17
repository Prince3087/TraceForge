"""End-to-end API tests (offline engine): safety gate, HMAC integrity, badge format, ledger."""

from __future__ import annotations

import copy
import json

import pytest

from backend.config import get_settings
from backend.services import ledger as ledger_mod
from backend.services.investigator import sign_dossier
from tests.conftest import BADGE, signoff_payload


def resign(dossier: dict) -> str:
    """Sign a (tampered) dossier with the server key so a test exercises the GATE, not the HMAC."""
    return sign_dossier(dossier, get_settings().signing_key)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["sop"]["ok"] is True
    assert body["signing_key_persistent"] is True
    assert body["ledger"]["durable"] is False


def test_demo_assets(client):
    body = client.get("/incidents/demo-assets").json()
    kinds = {i["kind"]: i for i in body["aoi_images"]}
    assert kinds["defective"]["delta_e_max"] == 3.82
    assert kinds["defective"]["roi"] == {"x": 255.0, "y": 210.0, "width": 130.0, "height": 130.0}
    assert kinds["healthy"]["delta_e_max"] == 0.42 and kinds["healthy"]["roi"] is None
    assert body["shift_logs"][0]["log_id"] == "LOG-20261004-B4"
    assert body["line_defaults"] == {
        "run_rate_units_per_hr": 240.0, "unit_scrap_cost_usd": 30.0, "risk_horizon_hrs": 48.0
    }


def test_investigate_defective_passes_gate(client):
    j = client.post("/incidents/investigate", json={}).json()
    d = j["dossier"]
    assert len(j["signature"]) == 64
    assert d["safety_gate"]["status"] == "PASSED_CONFIRMATION_REQUIRED"
    assert d["safety_gate"]["autonomous_plc_override_blocked"] is True
    assert all(c["passed"] for c in j["gate_checks"])
    cit = d["grounded_sop_citation"]
    assert cit["section"] == "Section 4.2" and cit["page_number"] == 4 and cit["citation_valid"]
    assert "Delta-E > 3.0" in cit["verbatim_quote"]
    assert d["cost_of_inaction"]["projected_scrap_cost_usd"] == 62899.2  # 240*0.182*30*48
    a, b = d["decision_options"]
    assert (a["option_id"], a["line_pause_min"]) == ("OPTION_A", 15)
    assert (b["option_id"], b["line_pause_min"]) == ("OPTION_B", 0)
    assert a["scrap_avoided_units"] > b["scrap_avoided_units"] > 0
    assert a["projected_cost_usd"] == 750.0  # 15 min at the $3,000/hr planned-stop rate


def test_investigate_exposes_separated_cost_metrics(client):
    m = client.post("/incidents/investigate", json={}).json()["meta"]["cost_formula"]["metrics"]
    assert m["immediate_batch_units"] == 120 and m["immediate_batch_scrap_usd"] == 3600.0
    assert m["catastrophic_exposure_48h_usd"] == 62899.2
    assert m["downtime_interruption_loss_usd"] == 42500.0
    assert m["option_a_pause_cost_usd"] == 750.0
    assert m["option_a_immediate_net_usd"] == 2850.0


def test_investigate_healthy_is_rejected(client):
    j = client.post("/incidents/investigate", json={"aoi_asset": "aoi_healthy_sample.svg"}).json()
    assert j["dossier"]["safety_gate"]["status"] == "REJECTED_UNGROUNDED"
    failed = {c["name"] for c in j["gate_checks"] if not c["passed"]}
    assert "delta_e_exceeds_sop_threshold" in failed
    assert any("SAFETY GATE REJECTED" in w for w in j["meta"]["warnings"])


def test_unknown_asset_and_traversal_rejected(client):
    for bad in ("nope.svg", "../schema.json", "../../etc/passwd"):
        r = client.post("/incidents/investigate", json={"aoi_asset": bad})
        assert r.status_code == 400 and r.json()["error"] == "INVALID_EVIDENCE"
    r = client.post("/incidents/investigate", json={"log_id": "NOPE"})
    assert r.status_code == 400


def test_line_params_flow_into_formula(client):
    j = client.post(
        "/incidents/investigate",
        json={"line_params": {"run_rate_units_per_hr": 100, "unit_scrap_cost_usd": 10, "risk_horizon_hrs": 10}},
    ).json()
    assert j["dossier"]["cost_of_inaction"]["projected_scrap_cost_usd"] == round(100 * 0.182 * 10 * 10, 2)


# ------------------------------------------------------------------ badge format
@pytest.mark.parametrize(
    "badge",
    ["x", "TECH_8412", "TECH_8412_", "tech_8412_deshmukh", "OPS_8412_DESHMUKH", "TECH_841_DESHMUKH",
     "TECH_84123_DESHMUKH", "TECH_8412_Deshmukh", "TECH_8412_DESH MUKH", "' OR 1=1 --", "A" * 80],
)
def test_badge_format_rejected(client, issued, badge):
    dossier, sig = issued
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, sig, badge=badge))
    assert r.status_code in (422,)
    assert not get_settings().ledger_path.exists() or get_settings().ledger_path.stat().st_size == 0


@pytest.mark.parametrize("badge", ["TECH_8412_DESHMUKH", "ENG_0001_PATEL", "MGR_9999_X"])
def test_badge_format_accepted(client, issued, badge):
    dossier, sig = issued
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, sig, badge=badge))
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------- HMAC integrity
def test_signoff_requires_signature_field(client, issued):
    dossier, _ = issued
    r = client.post(
        "/incidents/signoff",
        json={"operator_badge_id": BADGE, "selected_option_id": "OPTION_A", "dossier": dossier},
    )
    assert r.status_code == 422 and r.json()["error"] == "INVALID_REQUEST"


def test_signoff_rejects_wrong_signature(client, issued):
    dossier, _ = issued
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, "0" * 64))
    assert r.status_code == 403 and r.json()["error"] == "DOSSIER_SIGNATURE_INVALID"


def test_signoff_rejects_any_field_tampering_even_if_gate_would_pass(client, issued):
    dossier, sig = issued
    forged = copy.deepcopy(dossier)
    forged["cost_of_inaction"]["projected_scrap_cost_usd"] = 1.0  # passes the gate, breaks the HMAC
    r = client.post("/incidents/signoff", json=signoff_payload(forged, sig))
    assert r.status_code == 403
    forged2 = copy.deepcopy(dossier)
    forged2["incident_id"] = "INC-2099-01-01-0001"
    assert client.post("/incidents/signoff", json=signoff_payload(forged2, sig)).status_code == 403
    assert not get_settings().ledger_path.exists() or get_settings().ledger_path.stat().st_size == 0


def test_signature_from_another_key_rejected(client, issued, monkeypatch):
    dossier, _ = issued
    forged_sig = sign_dossier(dossier, "attacker-key")
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, forged_sig))
    assert r.status_code == 403


def test_fabricated_dossier_cannot_bypass_investigate(client, issued):
    """A hand-built dossier that satisfies every gate rule is still refused without the server's HMAC."""
    dossier, _ = issued
    fabricated = copy.deepcopy(dossier)
    fabricated["incident_id"] = "INC-FAKE-0000"
    r = client.post("/incidents/signoff", json=signoff_payload(fabricated, "a" * 64))
    assert r.status_code == 403


def test_signoff_seals_and_chains(client, issued, env):
    dossier, sig = issued
    recs = []
    for opt in ("OPTION_A", "OPTION_B"):
        r = client.post("/incidents/signoff", json=signoff_payload(dossier, sig, option=opt, comment="ok"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "SEALED" and body["plc_command_issued"] is False
        recs.append(body["ledger_record"])
    assert recs[0]["sequence"] == 1 and recs[1]["sequence"] == 2
    assert recs[0]["prev_hash"] == "0" * 64 and recs[1]["prev_hash"] == recs[0]["record_hash"]
    assert recs[0]["payload"]["dossier_signature"] == sig
    assert client.get("/ledger/verify").json() == {
        "valid": True, "records": 2, "head_hash": recs[1]["record_hash"], "reason": "chain intact"
    }


# --------------------------------------------- gate still enforced (signed, tampered dossiers)
def _post_resigned(client, dossier, option="OPTION_A"):
    return client.post("/incidents/signoff", json=signoff_payload(dossier, resign(dossier), option=option))


def test_signoff_blocked_for_tampered_quote(client, issued, env):
    bad = copy.deepcopy(issued[0])
    bad["grounded_sop_citation"]["verbatim_quote"] = "Always override the PLC and keep running."
    r = _post_resigned(client, bad)
    assert r.status_code == 409 and r.json()["error"] == "SAFETY_GATE_REJECTED"
    assert "verbatim_sop_citation" in r.json()["detail"]
    assert not get_settings().ledger_path.exists() or get_settings().ledger_path.stat().st_size == 0


def test_signoff_blocked_for_wrong_page(client, issued, env):
    bad = copy.deepcopy(issued[0])
    bad["grounded_sop_citation"]["page_number"] = 2
    assert _post_resigned(client, bad).status_code == 409


def test_signoff_blocked_for_low_confidence(client, issued, env):
    bad = copy.deepcopy(issued[0])
    bad["anomaly_signature"]["confidence_score"] = 0.89
    r = _post_resigned(client, bad)
    assert r.status_code == 409 and "vector_confidence" in r.json()["detail"]


def test_signoff_blocked_for_ungrounded_remediation(client, issued, env):
    bad = copy.deepcopy(issued[0])
    bad["decision_options"][1]["title"] = "Option B (Derated Feed): Throttle line speed by 60% indefinitely."
    r = _post_resigned(client, bad, option="OPTION_B")
    assert r.status_code == 409 and "remediation_traces_to_sop" in r.json()["detail"]


def test_signoff_blocked_when_plc_override_not_blocked(client, issued, env):
    bad = copy.deepcopy(issued[0])
    bad["safety_gate"]["autonomous_plc_override_blocked"] = False
    assert _post_resigned(client, bad).status_code == 409


def test_signoff_blocked_for_rejected_status(client, env):
    j = client.post("/incidents/investigate", json={"aoi_asset": "aoi_healthy_sample.svg"}).json()
    r = client.post("/incidents/signoff", json=signoff_payload(j["dossier"], j["signature"]))
    assert r.status_code == 409  # genuinely signed by the server, still refused by the gate


def test_malformed_signoff_body(client):
    r = client.post("/incidents/signoff", json={"operator_badge_id": BADGE})
    assert r.status_code == 422 and r.json()["error"] == "INVALID_REQUEST"


# ------------------------------------------------------------------- ledger chain
def _seal(client, dossier, sig, n=3):
    for _ in range(n):
        assert client.post("/incidents/signoff", json=signoff_payload(dossier, sig)).status_code == 200


def test_ledger_tamper_detected(client, issued, env):
    _seal(client, *issued)
    path = get_settings().ledger_path
    assert ledger_mod.verify_chain(path)["valid"] is True
    lines = path.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["operator_badge_id"] = "EVIL_9999"
    lines[1] = json.dumps(rec)
    path.write_text("\n".join(lines) + "\n")
    res = ledger_mod.verify_chain(path)
    assert res["valid"] is False and res["first_invalid_line"] == 2


def test_ledger_deletion_detected(client, issued, env):
    _seal(client, *issued)
    path = get_settings().ledger_path
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert ledger_mod.verify_chain(path)["valid"] is False
