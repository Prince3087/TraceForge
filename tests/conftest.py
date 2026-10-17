"""Test fixtures: isolate the ledger and force deterministic settings."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import get_settings


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TRACEFORGE_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("TRACEFORGE_OFFLINE_MODE", "true")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("LEDGER_BUCKET_NAME", raising=False)
    monkeypatch.setenv("TRACEFORGE_SIGNING_KEY", "test-signing-key")
    get_settings.cache_clear()
    import backend.main as main_mod

    main_mod._remote_store = None
    yield tmp_path
    main_mod._remote_store = None
    get_settings.cache_clear()


@pytest.fixture()
def client(env):
    from backend.main import app

    return TestClient(app)


@pytest.fixture()
def dossier(client):
    r = client.post("/incidents/investigate", json={})
    assert r.status_code == 200, r.text
    return r.json()["dossier"]


BADGE = "TECH_8412_DESHMUKH"


@pytest.fixture()
def issued(client):
    """(dossier, signature) exactly as /incidents/investigate issued them."""
    r = client.post("/incidents/investigate", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["dossier"], body["signature"]


def signoff_payload(dossier, signature, option="OPTION_A", badge=BADGE, **extra):
    return {
        "operator_badge_id": badge,
        "selected_option_id": option,
        "dossier": dossier,
        "signature": signature,
        **extra,
    }
