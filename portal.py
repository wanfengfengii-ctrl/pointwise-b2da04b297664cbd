import argparse
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

def encoded(value):
    return json.dumps(value, sort_keys=True).encode("utf-8")

class Source:
    """One disposable export. The source process remains alive during client restarts."""
    def __init__(self, fixture, clock=time.monotonic, sleep=time.sleep):
        self.ident = fixture["export_id"]
        self.raw = fixture["body"].encode("utf-8")
        self.digest = hashlib.sha256(self.raw).hexdigest()
        self.size = len(self.raw)
        self.ttl = float(fixture.get("lease_seconds", 10))
        self.delay = float(fixture.get("download_seconds", 0))
        if self.ttl <= 0 or self.delay < 0:
            raise ValueError("TIMING")
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.serial, self.token, self.deadline = 0, None, 0
        self.acked = False
        self.drop_ack_reply = self.bad_download = False
        self.events = []

    def dispatch(self, method, url, data=None):
        path = urlsplit(url).path
        data = data or {}
        with self.lock:
            if method == "GET" and path == "/audit":
                return 200, "application/json", encoded({
                    "acked": self.acked, "deleted": self.raw is None,
                    "events": self.events, "current_token": self.token})
            if method == "POST" and path == "/control":
                if data.get("expire"):
                    self.deadline = 0
                for key in ("drop_ack_reply", "bad_download"):
                    if key in data:
                        setattr(self, key, bool(data[key]))
                return 200, "application/json", encoded({"controlled": True})
            if method == "POST" and path == "/claim":
                if self.acked:
                    self.events.append({"kind": "empty"})
                    return 200, "application/json", encoded({"state": "empty"})
                self.serial += 1
                self.token = "lease-" + str(self.serial)
                self.deadline = self.clock() + self.ttl
                self.events.append({"kind": "claim", "token": self.token})
                return 200, "application/json", encoded({
                    "state": "leased", "export_id": self.ident, "sha256": self.digest,
                    "length": self.size, "lease_token": self.token, "expires_in": self.ttl})
            if method == "GET" and path == "/download":
                token = parse_qs(urlsplit(url).query).get("token", [None])[0]
                if self.acked:
                    return 410, "application/json", encoded({"error": "GONE"})
                if token != self.token or self.clock() >= self.deadline:
                    return 409, "application/json", encoded({"error": "LEASE_EXPIRED"})
                raw = self.raw
                if self.bad_download:
                    self.bad_download = False
                    raw = raw + b"!"
                self.events.append({"kind": "download", "token": token})
            elif method == "POST" and path == "/ack":
                token = data.get("lease_token")
                if (self.acked or data.get("export_id") != self.ident or
                        token != self.token or self.clock() >= self.deadline):
                    self.events.append({"kind": "rejected_ack", "token": token})
                    return 409, "application/json", encoded({"error": "LEASE_EXPIRED"})
                self.acked, self.raw = True, None
                self.events.append({"kind": "ack", "token": token})
                if self.drop_ack_reply:
                    self.drop_ack_reply = False
                    return 0, "", b""
                return 200, "application/json", encoded({"state": "acked"})
            else:
                return 404, "application/json", encoded({"error": "NOT_FOUND"})
        # A valid download continues after lease expiry. Never hold the source lock while waiting.
        self.sleep(self.delay)
        return 200, "application/octet-stream", raw

def handler(source):
    class Handler(BaseHTTPRequestHandler):
        def run_request(self):
            try:
                count = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(count)) if count else None
                status, mime, body = source.dispatch(self.command, self.path, data)
            except (ValueError, KeyError, TypeError):
                status, mime, body = 400, "application/json", encoded({"error": "BAD_REQUEST"})
            if status == 0:
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = run_request

        def log_message(self, *args):
            pass
    return Handler

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("fixture")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    with open(args.fixture, encoding="utf-8") as stream:
        source = Source(json.load(stream))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(source))
    print(json.dumps({"source_url": "http://127.0.0.1:" + str(server.server_port)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
