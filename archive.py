import base64
import hashlib
import json
import re
import sqlite3
import uuid

MAX_BYTES = 1024 * 1024

def metadata(meta):
    ident, digest, size = meta["export_id"], meta["sha256"], meta["length"]
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
        raise ValueError("EXPORT_ID")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("DIGEST")
    if type(size) is not int or not 0 <= size <= MAX_BYTES:
        raise ValueError("LENGTH")
    return ident, digest, size

def connect(path):
    db = sqlite3.connect(str(path), timeout=5)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS archives(
        export_id TEXT PRIMARY KEY, sha256 TEXT NOT NULL, length INTEGER NOT NULL,
        raw BLOB NOT NULL, receipt TEXT NOT NULL)""")
    return db

def save(db, meta, raw):
    ident, digest, size = metadata(meta)
    if not isinstance(raw, bytes) or len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("CONTENT_MISMATCH")
    with db:
        old = db.execute("SELECT sha256,length,raw,receipt FROM archives WHERE export_id=?",
                         (ident,)).fetchone()
        if old:
            if old[:2] != (digest, size):
                raise ValueError("IDENTITY_CONFLICT")
            if bytes(old[2]) != raw:
                raise ValueError("ARCHIVE_DAMAGED")
            return json.loads(old[3])
        receipt = {"export_id": ident, "sha256": digest, "length": size,
                   "receipt_id": uuid.uuid4().hex}
        db.execute("INSERT INTO archives VALUES(?,?,?,?,?)",
                   (ident, digest, size, raw, json.dumps(receipt, sort_keys=True)))
    return receipt

def read(db):
    result = []
    for ident, digest, size, raw, receipt in db.execute(
            "SELECT export_id,sha256,length,raw,receipt FROM archives ORDER BY export_id"):
        result.append({"export_id": ident, "sha256": digest, "length": size,
                       "raw_base64": base64.b64encode(raw).decode("ascii"),
                       "receipt": json.loads(receipt)})
    return result
