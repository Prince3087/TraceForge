"""UI-supplied Gemini model / API key: per-request overrides, never persisted or echoed."""

from __future__ import annotations

from backend.config import GEMINI_MODEL_CATALOG, get_settings
from backend.models.incident import InvestigateRequest
from backend.services import investigator

SECRET = "AQ.test-secret-key-123456"


def test_models_endpoint_lists_catalog_and_default(client):
    body = client.get("/models").json()
    assert body["default_model"] == get_settings().gemini_model
    assert [m["id"] for m in body["models"]] == [m["id"] for m in GEMINI_MODEL_CATALOG]
    assert body["models"][0]["id"] == "gemini-3.8-flash"
    assert body["server_key_configured"] is False and body["offline_mode"] is True


def test_override_applies_model_and_key_to_a_copy(env):
    base = get_settings()
    req = InvestigateRequest(gemini_model="gemini-3.1-flash-lite", gemini_api_key=SECRET)
    s = investigator._with_request_overrides(base, req)
    assert s.gemini_model == "gemini-3.1-flash-lite"
    assert s.gemini_api_key == SECRET and s.offline_mode is False
    assert base.gemini_api_key is None and base.offline_mode is True  # global settings untouched
    assert get_settings().gemini_api_key is None


def test_no_override_returns_same_settings(env):
    base = get_settings()
    assert investigator._with_request_overrides(base, InvestigateRequest()) is base
    blank = InvestigateRequest(gemini_api_key="   ")
    assert investigator._with_request_overrides(base, blank) is base


def test_secret_never_repr_or_echoed(client):
    req = InvestigateRequest(gemini_api_key=SECRET)
    assert SECRET not in repr(req) and SECRET not in req.model_dump_json()
    r = client.post("/incidents/investigate", json={"gemini_api_key": SECRET, "gemini_model": "gemini-3.1-flash-lite"})
    assert SECRET not in r.text


def test_request_model_and_key_reach_the_gemini_agent(client, monkeypatch):
    seen = {}

    def fake(settings, sop, aoi, logf, authoritative, client=None):
        seen["model"], seen["key"], seen["offline"] = settings.gemini_model, settings.gemini_api_key, settings.offline_mode
        return investigator.offline_rules_engine(settings, sop, aoi, logf, authoritative)

    monkeypatch.setattr(investigator, "generate_dossier", fake)
    r = client.post("/incidents/investigate", json={"gemini_api_key": SECRET, "gemini_model": "gemini-3.1-flash-lite"})
    assert r.status_code == 200, r.text
    assert seen == {"model": "gemini-3.1-flash-lite", "key": SECRET, "offline": False}
    assert get_settings().gemini_api_key is None  # not persisted past the request


def test_invalid_model_id_rejected(client):
    for bad in ["bad model", "x;rm -rf", "a" * 100, "../etc"]:
        r = client.post("/incidents/investigate", json={"gemini_model": bad})
        assert r.status_code == 422 and r.json()["error"] == "INVALID_REQUEST", bad
    assert SECRET not in client.post("/incidents/investigate", json={"gemini_model": "bad model", "gemini_api_key": SECRET}).text


def test_ui_key_unblocks_unconfigured_server(env, monkeypatch):
    """Server has no creds + offline off: without a UI key -> 503; with one the agent is reached."""
    monkeypatch.setenv("TRACEFORGE_OFFLINE_MODE", "false")
    get_settings.cache_clear()
    from fastapi.testclient import TestClient
    from backend.main import app

    c = TestClient(app)
    assert c.post("/incidents/investigate", json={}).json()["error"] == "GEMINI_NOT_CONFIGURED"
    reached = {}

    def fake(settings, sop, aoi, logf, authoritative, client=None):
        reached["key"] = settings.gemini_api_key
        return investigator.offline_rules_engine(settings, sop, aoi, logf, authoritative)

    monkeypatch.setattr(investigator, "generate_dossier", fake)
    assert c.post("/incidents/investigate", json={"gemini_api_key": SECRET}).status_code == 200
    assert reached["key"] == SECRET


def test_daily_quota_429_fails_fast_with_actionable_hint(env, monkeypatch):
    """Free-tier per-day quota: no 3x retry, no silent rules-engine fallback; tell the user to switch model."""
    from google.genai import errors as genai_errors

    from backend.services import gemini_agent

    calls = {"n": 0}

    class Boom:
        class models:  # noqa: N801
            @staticmethod
            def generate_content(**kw):
                calls["n"] += 1
                raise genai_errors.ClientError(
                    429,
                    {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                               "message": "Quota exceeded ... GenerateRequestsPerDayPerProjectPerModel-FreeTier "
                                          "Please retry in 7h10m52.7s."}},
                )

    monkeypatch.setattr(gemini_agent, "_make_client", lambda s: Boom)
    monkeypatch.setattr(gemini_agent.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    from backend.main import app

    r = TestClient(app).post("/incidents/investigate", json={"gemini_api_key": SECRET, "gemini_model": "gemini-3.8-flash"})
    assert calls["n"] == 1
    assert r.status_code == 502 and r.json()["error"] == "GEMINI_SYNTHESIS_FAILED"
    assert "quota" in r.json()["detail"].lower() and "7h10m" in r.json()["detail"]
    assert "different model" in r.json()["hint"]
    assert SECRET not in r.text


def test_per_minute_429_is_still_retried(env, monkeypatch):
    from google.genai import errors as genai_errors

    from backend.services import gemini_agent

    calls = {"n": 0}

    class Boom:
        class models:  # noqa: N801
            @staticmethod
            def generate_content(**kw):
                calls["n"] += 1
                raise genai_errors.ClientError(429, {"error": {"code": 429, "message": "slow down per minute"}})

    monkeypatch.setattr(gemini_agent, "_make_client", lambda s: Boom)
    monkeypatch.setattr(gemini_agent.time, "sleep", lambda s: None)
    from fastapi.testclient import TestClient
    from backend.main import app

    r = TestClient(app).post("/incidents/investigate", json={"gemini_api_key": SECRET})
    assert calls["n"] == 3 and r.status_code == 200  # transient -> labelled fallback
    assert r.json()["meta"]["engine"] == "offline_deterministic_fallback"
