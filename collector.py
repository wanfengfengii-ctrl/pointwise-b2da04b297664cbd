import json
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import archive
import hooks

class RemoteError(RuntimeError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status, self.code = status, code

class Client:
    def __init__(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.path not in ("", "/"):
            raise ValueError("LOCAL_SOURCE_URL")
        self.url = url.rstrip("/")

    def request(self, method, path, data=None):
        raw = json.dumps(data).encode("utf-8") if data is not None else None
        request = Request(self.url + path, data=raw, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.read(archive.MAX_BYTES + 1)
        except HTTPError as error:
            with error:
                detail = json.loads(error.read())
            raise RemoteError(error.code, detail["error"]) from error

    def claim(self):
        return json.loads(self.request("POST", "/claim", {}))

    def download(self, lease):
        return self.request("GET", "/download?" + urlencode({"token": lease["lease_token"]}))

    def ack(self, lease):
        answer = json.loads(self.request("POST", "/ack", {
            "export_id": lease["export_id"], "lease_token": lease["lease_token"]}))
        if answer != {"state": "acked"}:
            raise ValueError("ACK_RESPONSE")
        return answer

def once(client, db, root=None):
    lease = client.claim()
    if lease.get("state") != "leased":
        raise RuntimeError("REMOTE_EMPTY")
    archive.metadata(lease)
    raw = client.download(lease)
    hooks.pause(root, "downloaded")
    receipt = archive.save(db, lease, raw)
    hooks.pause(root, "archived")
    try:
        client.ack(lease)
    finally:
        hooks.pause(root, "ack_attempted")
    return receipt
