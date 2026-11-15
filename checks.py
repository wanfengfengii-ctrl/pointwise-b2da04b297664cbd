import base64
import hashlib
import io
import json
import sys
from http.client import RemoteDisconnected
from unittest.mock import patch
from urllib.error import HTTPError
import archive
import collector
import ops
import portal

FIXTURE = {"export_id": "notice-17", "body": "公告17\n取水量：12\n",
           "lease_seconds": 10, "download_seconds": 0}

class Clock:
    def __init__(self):
        self.now = 100.0
    def __call__(self):
        return self.now
    def sleep(self, seconds):
        self.now += seconds

class Wire:
    def __init__(self, request):
        self.incoming = io.BytesIO(request)
        self.outgoing = bytearray()
    def makefile(self, mode, *args):
        return self.incoming
    def sendall(self, data):
        self.outgoing.extend(data)
    def shutdown(self, *args):
        pass
    def close(self):
        pass

def opener(source):
    def open_request(request, timeout):
        body = request.data or b""
        path = request.selector
        head = (request.get_method() + " " + path + " HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Content-Length: " + str(len(body)) + "\r\nConnection: close\r\n\r\n")
        wire = Wire(head.encode("ascii") + body)
        portal.handler(source)(wire, ("127.0.0.1", 12345), object())
        raw = bytes(wire.outgoing)
        if not raw:
            raise RemoteDisconnected("Remote end closed connection without response")
        headers, payload = raw.split(b"\r\n\r\n", 1)
        code = int(headers.split(b" ", 2)[1])
        if code >= 400:
            raise HTTPError(request.full_url, code, "source rejected", {}, io.BytesIO(payload))
        return io.BytesIO(payload)
    return open_request

def scenario(fixture):
    clock = Clock()
    return portal.Source(fixture, clock, clock.sleep)

def main():
    raw = FIXTURE["body"].encode("utf-8")
    meta = {"export_id": "notice-17", "sha256": hashlib.sha256(raw).hexdigest(), "length": len(raw)}
    source = scenario(FIXTURE)
    db = archive.connect(":memory:")
    with patch("collector.urlopen", opener(source)):
        receipt = collector.once(collector.Client("http://127.0.0.1:8080"), db)
    rows = archive.read(db)
    assert len(rows) == 1 and base64.b64decode(rows[0]["raw_base64"]) == raw
    assert rows[0]["receipt"] == receipt and receipt["sha256"] == meta["sha256"]
    assert source.acked and source.raw is None
    assert [e["kind"] for e in source.events] == ["claim", "download", "ack"]
    assert archive.save(db, meta, raw) == receipt
    other = b"different"
    try:
        archive.save(db, dict(meta, sha256=hashlib.sha256(other).hexdigest(), length=len(other)), other)
    except ValueError as error:
        assert str(error) == "IDENTITY_CONFLICT"
    else:
        raise AssertionError("conflicting identity accepted")
    assert archive.read(db) == rows
    db.close()

    for mode in ("bad", "expired", "lost_ack"):
        fixture = dict(FIXTURE)
        if mode == "expired":
            fixture.update(lease_seconds=1, download_seconds=2)
        source = scenario(fixture)
        source.bad_download = mode == "bad"
        source.drop_ack_reply = mode == "lost_ack"
        db = archive.connect(":memory:")
        try:
            with patch("collector.urlopen", opener(source)):
                collector.once(collector.Client("http://127.0.0.1:8080"), db)
        except (ValueError, collector.RemoteError, RemoteDisconnected) as error:
            if mode == "bad":
                assert str(error) == "CONTENT_MISMATCH"
                assert archive.read(db) == [] and not source.acked
            elif mode == "expired":
                assert isinstance(error, collector.RemoteError) and error.code == "LEASE_EXPIRED"
                assert len(archive.read(db)) == 1 and not source.acked
            else:
                assert isinstance(error, RemoteDisconnected)
                assert len(archive.read(db)) == 1 and source.acked and source.raw is None
                status, mime, body = source.dispatch("POST", "/claim", {})
                assert status == 200 and json.loads(body) == {"state": "empty"}
        else:
            raise AssertionError("one-shot path hid " + mode)
        finally:
            db.close()

    sample = {"a.py": b"print(1)\n", "b.py": b"print(2)\n"}
    assert ops.pack(sample) == ops.pack(dict(reversed(list(sample.items()))))
    print("BUSINESS_REGRESSION_OK")
    if "--demo" in sys.argv:
        print(json.dumps({"archive": rows, "remote_deleted": True}, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
