import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import archive
import hooks
from collector import Client, RemoteError

POLL_SECONDS = 0.2
RETRY_SECONDS = 0.5

class Permanent(RuntimeError):
    """A fault that retrying cannot fix: bad body, identity or digest
    conflict, damaged archive, or an empty source without archive evidence."""

def damaged(error):
    text = str(error).lower()
    return "malformed" in text or "not a database" in text or "corrupt" in text

def load_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None

def store_json(path, value):
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    os.replace(str(temporary), str(path))

def note(message):
    print("[worker] " + message, file=sys.stderr, flush=True)

def write_status(root, state, export_id, receipt, error):
    store_json(root / "status.json", {"state": state, "export_id": export_id,
                                      "receipt": receipt, "error": error})

def archived_receipt(root, meta):
    # Short-lived read-only connection: no remote call ever happens while a
    # SQLite transaction is open, and network waits never hold the database.
    db = archive.connect(root / "archive.sqlite")
    try:
        return archive.receipt_for(db, meta)
    except sqlite3.DatabaseError as error:
        if damaged(error):
            raise Permanent("ARCHIVE_DAMAGED") from error
        raise
    finally:
        db.close()

def bind(root, meta):
    path = root / "binding.json"
    binding = load_json(path)
    if binding is None:
        store_json(path, meta)
        note("bound identity " + meta["export_id"])
        return
    if binding.get("export_id") != meta["export_id"]:
        raise Permanent("IDENTITY_REBIND")
    if binding.get("sha256") != meta["sha256"] or binding.get("length") != meta["length"]:
        raise Permanent("IDENTITY_CONFLICT")

def finish_empty(root):
    # The source only reports empty after a successful ack deleted the export.
    # That alone is not proof: require this task's own persisted archive with a
    # matching identity before declaring completion.
    binding = load_json(root / "binding.json")
    receipt = None
    if binding is not None:
        try:
            receipt = archived_receipt(root, binding)
        except ValueError as error:
            raise Permanent(str(error))
    if receipt is None:
        raise Permanent("REMOTE_EMPTY")
    write_status(root, "complete", binding["export_id"], receipt, None)
    note("source empty with archived evidence for " + binding["export_id"] + "; complete")

def guarded(call, *args):
    try:
        return call(*args)
    except RemoteError as error:
        if error.code == "LEASE_EXPIRED" or error.status >= 500:
            raise  # retryable: re-claim for a current token, then retry
        raise Permanent("REMOTE_" + str(error.code)) from error

def advance(root, task):
    client = Client(task["source_url"])
    answer = guarded(client.claim)
    if not isinstance(answer, dict):
        raise Permanent("CLAIM_RESPONSE")
    if answer.get("state") == "empty":
        finish_empty(root)
        return
    if answer.get("state") != "leased":
        raise Permanent("CLAIM_RESPONSE")
    try:
        meta = {"export_id": answer["export_id"], "sha256": answer["sha256"],
                "length": answer["length"]}
        token = answer["lease_token"]
    except KeyError as error:
        raise Permanent("CLAIM_RESPONSE") from error
    if not isinstance(token, str) or not token:
        raise Permanent("CLAIM_RESPONSE")
    try:
        archive.metadata(meta)
    except ValueError as error:
        raise Permanent(str(error))
    bind(root, meta)
    lease = {"export_id": meta["export_id"], "lease_token": token}
    try:
        receipt = archived_receipt(root, meta)
    except ValueError as error:
        raise Permanent(str(error))
    if receipt is None:
        write_status(root, "collecting", meta["export_id"], None, None)
        raw = guarded(client.download, lease)
        hooks.pause(root, "downloaded")
        db = archive.connect(root / "archive.sqlite")
        try:
            try:
                receipt = archive.save(db, meta, raw)
            except ValueError as error:
                raise Permanent(str(error))
            except sqlite3.DatabaseError as error:
                if damaged(error):
                    raise Permanent("ARCHIVE_DAMAGED") from error
                raise
        finally:
            db.close()
        note("archived " + meta["export_id"])
    # The archive transaction has committed; only the confirmation remains.
    # Reaching here with an existing archive reuses it instead of downloading
    # again, so a download that outlives its lease cannot stall the task.
    write_status(root, "awaiting_ack", meta["export_id"], receipt, None)
    hooks.pause(root, "archived")
    acked = False
    try:
        try:
            guarded(client.ack, lease)
        except ValueError as error:
            raise Permanent(str(error))
        acked = True
    finally:
        hooks.pause(root, "ack_attempted")
    if acked:
        write_status(root, "complete", meta["export_id"], receipt, None)
        note("acked " + meta["export_id"] + "; complete")

def fail(root, message):
    binding = load_json(root / "binding.json")
    export_id = binding["export_id"] if binding else None
    receipt = None
    if binding:
        try:
            receipt = archived_receipt(root, binding)
        except Exception:
            receipt = None
    write_status(root, "failed", export_id, receipt, message)
    note("permanent failure: " + message)

def step(root):
    task = load_json(root / "task.json")
    if task is None:
        time.sleep(POLL_SECONDS)
        return
    status = load_json(root / "status.json")
    if status and status.get("state") in ("complete", "failed"):
        time.sleep(POLL_SECONDS)
        return
    try:
        advance(root, task)
    except Permanent as error:
        fail(root, str(error))

def main():
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
    note("worker started, pid " + str(os.getpid()))
    while True:
        try:
            step(root)
        except Permanent as error:
            fail(root, str(error))
            time.sleep(RETRY_SECONDS)
        except Exception as error:
            # Retryable: network interruption, timeout, lost ack reply, or an
            # expired lease. Never park the task in failed for these.
            note("retryable failure: " + repr(error))
            time.sleep(RETRY_SECONDS)

if __name__ == "__main__":
    main()
