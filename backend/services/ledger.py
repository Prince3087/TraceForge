"""Cryptographic audit ledger: append-only, SHA-256 hash-chained JSONL.

Simulates an append-only BigQuery ledger table. Every record contains

    prev_hash   - record_hash of the previous record ("0"*64 for genesis)
    record_hash - SHA-256 over the canonical JSON of every other field

so editing, deleting or re-ordering any historical line breaks the chain, which
``verify_chain`` detects. Appends are serialised across threads (``threading.Lock``) and across
processes (``fcntl.flock``) so concurrent uvicorn workers cannot fork the chain.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("traceforge.ledger")

try:  # POSIX only; on other platforms the in-process lock still applies.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

GENESIS_HASH = "0" * 64
_THREAD_LOCK = threading.Lock()


class LedgerPersistenceError(RuntimeError):
    """Remote (GCS) persistence failed; the sign-off was NOT recorded anywhere (maps to HTTP 503)."""


class LedgerIntegrityError(RuntimeError):
    """The remote ledger copy fails hash-chain verification; refusing to build on it."""


class RemoteStore(Protocol):
    """Durable object store holding one JSON object per sealed record."""

    def put(self, record_hash: str, body: str) -> None: ...

    def list_bodies(self) -> list[str]: ...


class GcsStore:
    """gs://<bucket>/<prefix>/<record_sha256>.json via google-cloud-storage."""

    def __init__(self, bucket_name: str, prefix: str = "ledger", client: Any | None = None):
        if client is None:
            from google.cloud import storage  # lazy: only needed when a bucket is configured

            client = storage.Client()
        self._bucket = client.bucket(bucket_name)
        self._client = client
        self._bucket_name = bucket_name
        self._prefix = prefix.strip("/")

    def put(self, record_hash: str, body: str) -> None:
        blob = self._bucket.blob(f"{self._prefix}/{record_hash}.json")
        # if_generation_match=0 -> create-only: an existing object is never overwritten.
        try:
            blob.upload_from_string(body, content_type="application/json", if_generation_match=0)
        except Exception as exc:  # noqa: BLE001
            # 412 Precondition Failed == identical content-addressed object already exists: fine.
            if getattr(exc, "code", None) != 412:
                raise

    def list_bodies(self) -> list[str]:
        return [
            b.download_as_text()
            for b in self._client.list_blobs(self._bucket_name, prefix=f"{self._prefix}/")
            if b.name.endswith(".json")
        ]


def make_remote_store(settings: Any) -> "RemoteStore | None":
    """GcsStore when LEDGER_BUCKET_NAME is set, else None (local-only ledger)."""
    if not settings.ledger_bucket_name:
        return None
    return GcsStore(settings.ledger_bucket_name, settings.ledger_remote_prefix)


def canonical_json(obj: Any) -> str:
    """Deterministic serialisation used for every hash in the ledger."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_hash(record_without_hash: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(record_without_hash))


def _read_last_record(path: Path) -> dict[str, Any] | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        block = 4096
        data = b""
        while size > 0:
            step = min(block, size)
            size -= step
            fh.seek(size)
            data = fh.read(step) + data
            lines = [ln for ln in data.splitlines() if ln.strip()]
            if len(lines) >= 2 or size == 0:
                break
    lines = [ln for ln in data.splitlines() if ln.strip()]
    return json.loads(lines[-1]) if lines else None


def append_signoff(
    ledger_path: Path,
    *,
    incident_id: str,
    operator_badge_id: str,
    selected_option_id: str,
    dossier: dict[str, Any],
    gate_checks: list[dict[str, Any]],
    comment: str | None = None,
    dossier_signature: str | None = None,
    remote: RemoteStore | None = None,
) -> dict[str, Any]:
    """Append one operator sign-off record and return it (including its hash).

    With ``remote`` configured the record is written to the bucket FIRST; if that fails nothing is
    appended locally and LedgerPersistenceError is raised (fail closed - an operator must never be
    told "sealed" for a record that would vanish with the container).
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    selected = next(o for o in dossier["decision_options"] if o["option_id"] == selected_option_id)

    with _THREAD_LOCK:
        # "a+b" creates the file if needed; the flock guards read-last + append as one critical section.
        with ledger_path.open("a+b") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                if remote is not None and _is_empty(ledger_path):
                    # Fresh container: restore history first or we would fork the chain at genesis.
                    _hydrate_locked(ledger_path, remote)
                last = _read_last_record(ledger_path)
                body: dict[str, Any] = {
                    "sequence": (last["sequence"] + 1) if last else 1,
                    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                    "event_type": "OPERATOR_SIGNOFF",
                    "incident_id": incident_id,
                    "operator_badge_id": operator_badge_id,
                    "selected_option_id": selected_option_id,
                    "dossier_sha256": sha256_hex(canonical_json(dossier)),
                    "payload": {
                        "selected_option": selected,
                        "dossier": dossier,
                        "gate_checks": gate_checks,
                        "comment": comment,
                        "plc_command_issued": False,
                        "dossier_signature": dossier_signature,
                    },
                    "prev_hash": last["record_hash"] if last else GENESIS_HASH,
                }
                record = {**body, "record_hash": _record_hash(body)}
                if remote is not None:
                    try:
                        remote.put(record["record_hash"], json.dumps(record, indent=2, sort_keys=True))
                    except Exception as exc:  # noqa: BLE001
                        raise LedgerPersistenceError(
                            f"Remote ledger write failed; sign-off NOT recorded: {exc}"
                        ) from exc
                fh.write((canonical_json(record) + "\n").encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    return record


def _is_empty(path: Path) -> bool:
    return not path.exists() or path.stat().st_size == 0


def _chain_ok(records: list[dict[str, Any]]) -> tuple[bool, str]:
    prev = GENESIS_HASH
    for i, rec in enumerate(records, start=1):
        body = {k: v for k, v in rec.items() if k != "record_hash"}
        if rec.get("sequence") != i:
            return False, f"sequence gap at position {i}"
        if rec.get("prev_hash") != prev:
            return False, f"broken hash link at sequence {i}"
        if _record_hash(body) != rec.get("record_hash"):
            return False, f"record hash mismatch at sequence {i}"
        prev = rec["record_hash"]
    return True, "chain intact"


def _hydrate_locked(ledger_path: Path, remote: RemoteStore) -> int:
    """Rebuild an EMPTY local file from the bucket. Caller holds the file lock."""
    try:
        records = [json.loads(b) for b in remote.list_bodies()]
    except Exception as exc:  # noqa: BLE001
        raise LedgerPersistenceError(f"Cannot read remote ledger; refusing to start a new chain: {exc}") from exc
    if not records:
        return 0
    records.sort(key=lambda r: r.get("sequence", 0))
    ok, why = _chain_ok(records)
    if not ok:
        raise LedgerIntegrityError(f"Remote ledger failed verification ({why}); local ledger not restored.")
    with ledger_path.open("ab") as fh:
        for rec in records:
            fh.write((canonical_json(rec) + "\n").encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())
    log.info("restored %d ledger record(s) from remote store", len(records))
    return len(records)


def hydrate_from_remote(ledger_path: Path, remote: RemoteStore | None) -> int:
    """Startup hook: if the local JSONL is empty, restore it from the bucket. Returns records restored."""
    if remote is None:
        return 0
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        with ledger_path.open("a+b") as fh:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                if not _is_empty(ledger_path):
                    return 0
                return _hydrate_locked(ledger_path, remote)
            finally:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def verify_chain(ledger_path: Path) -> dict[str, Any]:
    """Recompute every hash and link. Returns {valid, records, first_invalid_sequence, reason}."""
    if not ledger_path.exists():
        return {"valid": True, "records": 0, "head_hash": GENESIS_HASH, "reason": "ledger empty"}
    prev = GENESIS_HASH
    count = 0
    with ledger_path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                claimed = rec.pop("record_hash")
            except (json.JSONDecodeError, KeyError):
                return {"valid": False, "records": count, "first_invalid_line": lineno, "reason": "unparseable record"}
            count += 1
            if rec.get("prev_hash") != prev:
                return {"valid": False, "records": count, "first_invalid_line": lineno, "reason": "broken hash link"}
            if rec.get("sequence") != count:
                return {"valid": False, "records": count, "first_invalid_line": lineno, "reason": "sequence gap"}
            if _record_hash(rec) != claimed:
                return {"valid": False, "records": count, "first_invalid_line": lineno, "reason": "record hash mismatch"}
            prev = claimed
    return {"valid": True, "records": count, "head_hash": prev, "reason": "chain intact"}
