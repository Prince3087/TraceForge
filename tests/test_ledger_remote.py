"""P5: durable ledger - GCS write-through, restart hydration, fail-closed behaviour."""

from __future__ import annotations

import json

import pytest

from backend.config import get_settings
from backend.services import ledger
from backend.services.ledger import (
    GcsStore,
    LedgerIntegrityError,
    LedgerPersistenceError,
    hydrate_from_remote,
)
from tests.conftest import signoff_payload


# ------------------------------------------------------------ in-memory fake of google-cloud-storage
class _PreconditionFailed(Exception):
    code = 412


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def upload_from_string(self, data, content_type=None, if_generation_match=None):
        if self.bucket.fail_writes:
            raise RuntimeError("GCS unavailable")
        if if_generation_match == 0 and self.name in self.bucket.objects:
            raise _PreconditionFailed("exists")
        self.bucket.objects[self.name] = data

    def download_as_text(self):
        return self.bucket.objects[self.name]


class FakeBucket:
    def __init__(self):
        self.objects: dict[str, str] = {}
        self.fail_writes = False
        self.fail_reads = False

    def blob(self, name):
        return FakeBlob(self, name)


class FakeStorageClient:
    def __init__(self, bucket: FakeBucket):
        self._b = bucket

    def bucket(self, _name):
        return self._b

    def list_blobs(self, _bucket_name, prefix=""):
        if self._b.fail_reads:
            raise RuntimeError("GCS read denied")
        return [FakeBlob(self._b, n) for n in sorted(self._b.objects) if n.startswith(prefix)]


@pytest.fixture()
def bucket():
    return FakeBucket()


@pytest.fixture()
def store(bucket):
    return GcsStore("tf-ledger", "ledger", client=FakeStorageClient(bucket))


def _append(path, store, n=1, **kw):
    dossier = {"decision_options": [{"option_id": "OPTION_A"}], "x": 1}
    out = []
    for _ in range(n):
        out.append(
            ledger.append_signoff(
                path, incident_id="INC-1", operator_badge_id="TECH_8412_DESHMUKH",
                selected_option_id="OPTION_A", dossier=dossier, gate_checks=[], remote=store, **kw,
            )
        )
    return out


# ------------------------------------------------------------------------------------ write-through
def test_each_record_lands_in_bucket_under_its_sha256(tmp_path, store, bucket):
    recs = _append(tmp_path / "l.jsonl", store, n=2)
    assert set(bucket.objects) == {f"ledger/{r['record_hash']}.json" for r in recs}
    stored = json.loads(bucket.objects[f"ledger/{recs[0]['record_hash']}.json"])
    assert stored == recs[0]  # exact record, hash included


def test_local_only_when_no_remote(tmp_path):
    rec = _append(tmp_path / "l.jsonl", None)[0]
    assert rec["sequence"] == 1 and ledger.verify_chain(tmp_path / "l.jsonl")["valid"]


def test_put_is_create_only_and_idempotent(store, bucket):
    store.put("abc", "one")
    store.put("abc", "two")  # 412 swallowed, original preserved (immutability)
    assert bucket.objects["ledger/abc.json"] == "one"


def test_put_propagates_real_errors(store, bucket):
    bucket.fail_writes = True
    with pytest.raises(RuntimeError):
        store.put("abc", "x")


# ----------------------------------------------------------------------------- restart hydration
def test_empty_local_is_rebuilt_from_bucket_and_chain_continues(tmp_path, store):
    p1 = tmp_path / "before" / "l.jsonl"
    old = _append(p1, store, n=3)
    p2 = tmp_path / "after_restart" / "l.jsonl"  # brand-new container: empty disk
    assert hydrate_from_remote(p2, store) == 3
    assert ledger.verify_chain(p2) == {"valid": True, "records": 3, "head_hash": old[-1]["record_hash"],
                                       "reason": "chain intact"}
    new = _append(p2, store, n=1)[0]
    assert new["sequence"] == 4 and new["prev_hash"] == old[-1]["record_hash"]
    assert ledger.verify_chain(p2)["valid"]


def test_hydration_orders_by_sequence_not_object_name(tmp_path, store, bucket):
    _append(tmp_path / "a.jsonl", store, n=5)  # object names are random hashes -> arbitrary listing order
    p = tmp_path / "b.jsonl"
    assert hydrate_from_remote(p, store) == 5 and ledger.verify_chain(p)["valid"]


def test_hydration_skips_nonempty_local(tmp_path, store):
    p = tmp_path / "l.jsonl"
    _append(p, store, n=2)
    assert hydrate_from_remote(p, store) == 0
    assert ledger.verify_chain(p)["records"] == 2  # not duplicated


def test_hydration_noop_without_remote_or_objects(tmp_path, store):
    assert hydrate_from_remote(tmp_path / "x.jsonl", None) == 0
    assert hydrate_from_remote(tmp_path / "y.jsonl", store) == 0


def test_first_append_on_fresh_container_hydrates_instead_of_forking_genesis(tmp_path, store):
    """Startup hook missed (e.g. bucket flaky at boot): append must still never restart at sequence 1."""
    _append(tmp_path / "a.jsonl", store, n=2)
    fresh = tmp_path / "fresh.jsonl"
    rec = _append(fresh, store, n=1)[0]
    assert rec["sequence"] == 3
    assert ledger.verify_chain(fresh)["records"] == 3


def test_tampered_remote_is_refused_not_restored(tmp_path, store, bucket):
    _append(tmp_path / "a.jsonl", store, n=3)
    name = sorted(bucket.objects)[0]
    rec = json.loads(bucket.objects[name])
    rec["operator_badge_id"] = "EVIL_0000_X"
    bucket.objects[name] = json.dumps(rec)
    p = tmp_path / "b.jsonl"
    with pytest.raises(LedgerIntegrityError):
        hydrate_from_remote(p, store)
    assert not p.exists() or p.stat().st_size == 0


def test_missing_remote_record_is_detected(tmp_path, store, bucket):
    recs = _append(tmp_path / "a.jsonl", store, n=3)
    del bucket.objects[f"ledger/{recs[1]['record_hash']}.json"]
    with pytest.raises(LedgerIntegrityError):
        hydrate_from_remote(tmp_path / "b.jsonl", store)


# --------------------------------------------------------------------------------- fail closed
def test_remote_write_failure_blocks_the_seal_and_leaves_local_untouched(tmp_path, store, bucket):
    p = tmp_path / "l.jsonl"
    _append(p, store, n=1)
    bucket.fail_writes = True
    with pytest.raises(LedgerPersistenceError):
        _append(p, store, n=1)
    assert ledger.verify_chain(p)["records"] == 1  # nothing half-written


def test_unreadable_bucket_refuses_to_start_a_new_chain(tmp_path, store, bucket):
    bucket.fail_reads = True
    with pytest.raises(LedgerPersistenceError):
        hydrate_from_remote(tmp_path / "l.jsonl", store)
    with pytest.raises(LedgerPersistenceError):
        _append(tmp_path / "l.jsonl", store, n=1)
    assert not (tmp_path / "l.jsonl").exists() or (tmp_path / "l.jsonl").stat().st_size == 0


# ------------------------------------------------------------------------ API integration
def test_api_signoff_writes_through_and_503_on_bucket_outage(client, issued, store, bucket, monkeypatch):
    import backend.main as m

    monkeypatch.setattr(m, "get_remote_store", lambda: store)
    dossier, sig = issued
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, sig))
    assert r.status_code == 200 and len(bucket.objects) == 1

    bucket.fail_writes = True
    r = client.post("/incidents/signoff", json=signoff_payload(dossier, sig))
    assert r.status_code == 503 and r.json()["error"] == "LEDGER_PERSISTENCE_FAILED"
    assert get_settings().ledger_path.read_text().count("\n") == 1  # outage never produced a local-only record


def test_api_startup_restores_ledger_from_bucket(tmp_path, store, monkeypatch):
    from fastapi.testclient import TestClient

    import backend.main as m

    _append(tmp_path / "seed.jsonl", store, n=2)
    monkeypatch.setenv("TRACEFORGE_LEDGER_PATH", str(tmp_path / "restart" / "l.jsonl"))
    monkeypatch.setenv("TRACEFORGE_OFFLINE_MODE", "true")
    monkeypatch.setenv("LEDGER_BUCKET_NAME", "tf-ledger")
    get_settings.cache_clear()
    monkeypatch.setattr(m, "get_remote_store", lambda: store)
    with TestClient(m.app) as c:  # context manager runs the lifespan startup hook
        v = c.get("/ledger/verify").json()
        health = c.get("/health").json()
    get_settings.cache_clear()
    assert v["valid"] and v["records"] == 2
    assert health["ledger"]["durable"] is True and health["ledger"]["remote_bucket"] == "tf-ledger"


def test_make_remote_store_none_without_bucket(env):
    assert ledger.make_remote_store(get_settings()) is None
