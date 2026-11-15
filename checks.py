import base64
import hashlib
import io
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from http.client import RemoteDisconnected
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
import archive
import collector
import ops
import portal

FIXTURE = {"export_id": "notice-17", "body": "公告17\n取水量：12\n",
           "lease_seconds": 10, "download_seconds": 0}

EXPECTED_SHA256 = "8695bbda668a795e0880c13b93ee639ae0f4d88a08369ede7b70f04a1567cda4"
EXPECTED_RAW_B64 = "5YWs5ZGKMTcK5Y+W5rC06YeP77yaMTIK"

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

def regression():
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

# --- Go-live acceptance through the installed artifact ---------------------
#
# Every scenario runs the built package only: the development copies in this
# directory are neither on sys.path nor the working directory of any spawned
# process. The source process stays alive across client restarts. On macOS the
# worker is supervised by the real LaunchAgent (ops agent enable, launchd
# RunAtLoad/KeepAlive); elsewhere the installed keeper provides the same
# user-level start and auto-restart semantics. Workers are killed with SIGKILL
# and must converge without any manual restart.

HERE = Path(__file__).resolve().parent
DARWIN = sys.platform == "darwin"
T_HOOK = 60.0 if DARWIN else 30.0
T_RESTART = 90.0 if DARWIN else 30.0
T_COMPLETE = 180.0 if DARWIN else 60.0

def wait_for(label, timeout, probe):
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = probe()
        except Exception:
            value = None  # transient: source not up yet, half-written file, ...
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError("TIMEOUT waiting for " + label)
        time.sleep(0.1)

class Live:
    """One installed-prefix collection against one real source process."""

    def __init__(self, package, name, **fixture_updates):
        self.name = name
        self.package = Path(package)
        self.dir = Path(tempfile.mkdtemp(prefix="golive_" + name + "_"))
        self.work = self.dir / "cwd"
        self.work.mkdir()
        self.prefix = self.dir / "prefix with space"
        fixture = dict(FIXTURE, **fixture_updates)
        self.fixture_path = self.dir / "fixture.json"
        self.fixture_path.write_text(json.dumps(fixture, ensure_ascii=False), encoding="utf-8")
        self.env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", str(self.dir))}
        if "TMPDIR" in os.environ:
            self.env["TMPDIR"] = os.environ["TMPDIR"]
        self.source = None
        self.keeper = None
        self.handles = []
        self.agent_enabled = False

    @property
    def app(self):
        return self.prefix / "app.pyz"

    @property
    def state(self):
        return self.prefix / "state"

    def run_app(self, app, *args, check=True):
        result = subprocess.run([sys.executable, str(app), *args], cwd=self.work, env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and result.returncode != 0:
            raise AssertionError("app %s failed\nstdout: %s\nstderr: %s"
                                 % (" ".join(args), result.stdout, result.stderr))
        return result

    def install(self):
        # The package installs itself: only installed entry points are used.
        self.run_app(self.package, "ops", "install", str(self.package), str(self.prefix))

    def start_source(self, port=0):
        logs = open(self.dir / "source.stderr.log", "a")
        self.handles.append(logs)
        self.source = subprocess.Popen([sys.executable, str(self.app), "portal",
                                        str(self.fixture_path), "--port", str(port)],
                                       cwd=self.work, env=self.env,
                                       stdout=subprocess.PIPE, stderr=logs, text=True)
        line = self.source.stdout.readline()
        self.url = json.loads(line)["source_url"]

    def submit(self, url=None, check=True):
        result = self.run_app(self.app, "ops", "submit", str(self.prefix), url or self.url,
                              check=check)
        return result if not check else json.loads(result.stdout)

    def status(self):
        return json.loads(self.run_app(self.app, "ops", "status", str(self.prefix)).stdout)

    def read(self):
        return json.loads(self.run_app(self.app, "ops", "read", str(self.prefix)).stdout)

    def status_file(self):
        path = self.state / "status.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def audit(self):
        with urllib.request.urlopen(self.url + "/audit", timeout=5) as response:
            return json.loads(response.read())

    def control(self, **flags):
        request = urllib.request.Request(self.url + "/control", method="POST",
                                         data=json.dumps(flags).encode("utf-8"))
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read())

    def supervise(self):
        if DARWIN:
            self.run_app(self.app, "ops", "agent", str(self.prefix), "enable")
            self.agent_enabled = True
            return
        out = open(self.prefix / "logs" / "stdout.log", "a")
        err = open(self.prefix / "logs" / "stderr.log", "a")
        self.handles += [out, err]
        self.keeper = subprocess.Popen([sys.executable, str(self.app), "keeper", str(self.prefix)],
                                       cwd=self.work, env=self.env, stdout=out, stderr=err)

    def unsupervise(self):
        if self.agent_enabled:
            self.run_app(self.app, "ops", "agent", str(self.prefix), "disable", check=False)
            self.agent_enabled = False
        if self.keeper is not None:
            self.keeper.terminate()
            try:
                self.keeper.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.keeper.kill()
            self.keeper = None
        pid = self.worker_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            wait_for("worker exit", T_RESTART, lambda: not self.pid_alive(pid))

    @staticmethod
    def pid_alive(pid):
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def worker_pid(self):
        try:
            return int((self.state / "worker.pid").read_text(encoding="ascii"))
        except (OSError, ValueError):
            return None

    def kill_worker(self, pid):
        os.kill(pid, signal.SIGKILL)
        wait_for("worker auto-restart", T_RESTART,
                 lambda: self.worker_pid() not in (None, pid))

    def hold(self, label):
        hooks_dir = self.state / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        (hooks_dir / (label + ".hold")).write_text("", encoding="ascii")

    def release(self, label):
        (self.state / "hooks" / (label + ".hold")).unlink(missing_ok=True)

    def reached(self, label):
        path = self.state / "hooks" / (label + ".reached")
        return int(path.read_text(encoding="ascii")) if path.exists() else None

    def wait_complete(self):
        wait_for("complete", T_COMPLETE,
                 lambda: (self.status_file() or {}).get("state") == "complete")
        return self.status()

    def wait_failed(self):
        wait_for("failed", T_COMPLETE,
                 lambda: (self.status_file() or {}).get("state") == "failed")
        return self.status()

    def assert_complete(self):
        status = self.status()
        assert set(status) == {"state", "export_id", "receipt", "error"}, status
        assert status["state"] == "complete" and status["error"] is None, status
        assert status["export_id"] == "notice-17", status
        receipt = status["receipt"]
        assert receipt["export_id"] == "notice-17" and receipt["receipt_id"], receipt
        assert receipt["sha256"] == EXPECTED_SHA256 and receipt["length"] == 24, receipt
        rows = self.read()
        assert len(rows) == 1, rows
        row = rows[0]
        assert row["export_id"] == "notice-17" and row["sha256"] == EXPECTED_SHA256, row
        assert row["length"] == 24 and row["raw_base64"] == EXPECTED_RAW_B64, row
        assert row["receipt"] == receipt, (row, receipt)
        audit = self.audit()
        assert audit["acked"] is True and audit["deleted"] is True, audit
        return receipt, [event["kind"] for event in audit["events"]]

    def diagnostics(self):
        for path in (self.state / "task.json", self.state / "binding.json",
                     self.state / "status.json"):
            if path.exists():
                print("--- %s: %s" % (path.name, path.read_text(encoding="utf-8")))
        try:
            print("--- audit: " + json.dumps(self.audit(), sort_keys=True))
        except Exception as error:
            print("--- audit unavailable: %r" % error)
        for name in ("stderr.log", "stdout.log"):
            path = self.prefix / "logs" / name
            if path.exists():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                print("--- logs/%s (tail):" % name)
                for line in lines[-20:]:
                    print("    " + line)

    def close(self):
        self.unsupervise()
        if self.source is not None:
            self.source.terminate()
            try:
                self.source.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.source.kill()
        for handle in self.handles:
            handle.close()
        shutil.rmtree(self.dir, ignore_errors=True)

def scenario_normal(package):
    live = Live(package, "normal")
    try:
        live.install()
        idle = live.status()
        assert idle == {"state": "idle", "export_id": None, "receipt": None, "error": None}, idle
        live.start_source()
        first = live.submit()
        assert first["state"] == "queued" and first["export_id"] is None, first
        assert live.submit() == first  # same URL resubmitted returns the original state
        conflict = live.submit("http://127.0.0.1:9/", check=False)
        assert conflict.returncode != 0 and "TASK_CONFLICT" in conflict.stderr, conflict.stderr
        task = json.loads((live.state / "task.json").read_text(encoding="utf-8"))
        assert task == {"source_url": live.url}, task
        live.supervise()
        live.wait_complete()
        receipt, kinds = live.assert_complete()
        assert kinds == ["claim", "download", "ack"], kinds
        assert live.submit()["state"] == "complete"  # resubmit after completion is a no-op
        # A corrupted package must be rejected before the installed app is replaced.
        good = live.app.read_bytes()
        forged = bytearray(live.package.read_bytes())
        forged[len(forged) // 2] ^= 0xFF
        bad = live.dir / "bad.pyz"
        bad.write_bytes(bytes(forged))
        shutil.copyfile(str(live.package) + ".sha256", str(bad) + ".sha256")
        rejected = live.run_app(live.app, "ops", "install", str(bad), str(live.prefix), check=False)
        assert rejected.returncode != 0 and "BAD_PACKAGE" in rejected.stderr, rejected.stderr
        assert live.app.read_bytes() == good
        assert live.status()["state"] == "complete"
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_NORMAL_OK")

def scenario_expiry(package):
    live = Live(package, "expiry", lease_seconds=1, download_seconds=2)
    try:
        live.install()
        live.start_source()
        live.submit()
        live.supervise()
        live.wait_complete()
        receipt, kinds = live.assert_complete()
        # Every download outlives its lease; the task must re-claim and confirm
        # against the reused archive instead of downloading again.
        assert kinds.count("download") == 1, kinds
        assert kinds.count("claim") >= 2 and kinds.count("rejected_ack") >= 1, kinds
        assert kinds[-1] == "ack", kinds
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_EXPIRY_OK")

def scenario_kill_download(package):
    live = Live(package, "kill_download", download_seconds=3)
    try:
        live.install()
        live.start_source()
        live.submit()
        live.supervise()
        wait_for("download in progress", T_HOOK,
                 lambda: any(e["kind"] == "download" for e in live.audit()["events"]))
        pid = wait_for("worker pid", T_HOOK, live.worker_pid)
        live.kill_worker(pid)
        live.wait_complete()
        receipt, kinds = live.assert_complete()
        assert kinds.count("download") >= 2, kinds  # killed mid-download, fetched again
        assert kinds[-1] == "ack", kinds
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_KILL_DOWNLOAD_OK")

def scenario_kill_archived(package):
    live = Live(package, "kill_archived")
    try:
        live.install()
        live.start_source()
        live.submit()
        live.hold("archived")
        live.supervise()
        pid = wait_for("archived hook", T_HOOK, lambda: live.reached("archived"))
        paused = live.status_file()
        assert paused["state"] == "awaiting_ack" and paused["receipt"], paused
        live.kill_worker(pid)
        live.release("archived")
        live.wait_complete()
        receipt, kinds = live.assert_complete()
        assert receipt == paused["receipt"]  # stable receipt across the restart
        assert kinds.count("download") == 1, kinds  # archive reused, not re-fetched
        assert kinds.count("claim") == 2 and kinds[-1] == "ack", kinds
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_KILL_ARCHIVED_OK")

def scenario_lost_ack(package):
    live = Live(package, "lost_ack")
    try:
        live.install()
        live.start_source()
        live.control(drop_ack_reply=True)  # ack takes effect, then the reply is dropped
        live.submit()
        live.hold("ack_attempted")
        live.supervise()
        pid = wait_for("ack_attempted hook", T_HOOK, lambda: live.reached("ack_attempted"))
        paused = live.status_file()
        assert paused["state"] == "awaiting_ack" and paused["receipt"], paused
        audit = live.audit()
        assert audit["acked"] is True and audit["deleted"] is True, audit
        live.kill_worker(pid)
        live.release("ack_attempted")
        live.wait_complete()
        receipt, kinds = live.assert_complete()
        assert receipt == paused["receipt"]
        # The ack already happened remotely; recovery sees the empty source and
        # completes from local archive evidence without confirming again.
        assert kinds == ["claim", "download", "ack", "empty"], kinds
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_LOST_ACK_OK")

def scenario_bad_body(package):
    live = Live(package, "bad_body")
    try:
        live.install()
        live.start_source()
        live.control(bad_download=True)  # next download carries an extra byte
        live.submit()
        live.supervise()
        status = live.wait_failed()
        time.sleep(1.5)  # a permanent failure must not retry into a download
        status = live.status()
        assert status["state"] == "failed" and status["error"], status
        assert "CONTENT_MISMATCH" in status["error"], status
        assert status["export_id"] == "notice-17" and status["receipt"] is None, status
        assert live.read() == []  # a bad body never produces an archive
        audit = live.audit()
        kinds = [event["kind"] for event in audit["events"]]
        assert kinds == ["claim", "download"], kinds
        assert audit["acked"] is False and audit["deleted"] is False, audit
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_BAD_BODY_OK")

def scenario_network_retry(package):
    live = Live(package, "net_retry")
    try:
        live.install()
        # Submit while nothing listens yet: connection refused must be retried,
        # never mistaken for completion or a permanent failure.
        reserved = socket.socket()
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
        reserved.close()
        live.url = "http://127.0.0.1:" + str(port)
        live.submit()
        live.supervise()
        time.sleep(2.0)
        pending = live.status()
        assert pending == {"state": "queued", "export_id": None, "receipt": None,
                           "error": None}, pending
        live.start_source(port)
        assert live.url == "http://127.0.0.1:" + str(port)
        live.wait_complete()
        live.assert_complete()
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_NETWORK_RETRY_OK")

def scenario_damaged_archive(package):
    live = Live(package, "damaged_archive")
    try:
        live.install()
        live.start_source()
        live.submit()
        live.hold("archived")
        live.supervise()
        pid = wait_for("archived hook", T_HOOK, lambda: live.reached("archived"))
        live.unsupervise()  # worker parked between archive commit and ack
        # Corrupt the archived bytes while the worker is down; the restart must
        # detect the damage and fail instead of confirming the remote deletion.
        db = sqlite3.connect(str(live.state / "archive.sqlite"))
        try:
            db.execute("UPDATE archives SET raw=? WHERE export_id=?",
                       (b"tampered-bytes", "notice-17"))
            db.commit()
        finally:
            db.close()
        live.release("archived")
        live.supervise()
        status = live.wait_failed()
        assert status["state"] == "failed" and "ARCHIVE_DAMAGED" in status["error"], status
        audit = live.audit()
        kinds = [event["kind"] for event in audit["events"]]
        assert "ack" not in kinds and audit["acked"] is False and audit["deleted"] is False, audit
    except Exception:
        live.diagnostics()
        raise
    finally:
        live.close()
    print("GOLIVE_DAMAGED_ARCHIVE_OK")

GOLIVE = (scenario_normal, scenario_expiry, scenario_kill_download,
          scenario_kill_archived, scenario_lost_ack, scenario_bad_body,
          scenario_network_retry, scenario_damaged_archive)

def build_package(directory):
    package = Path(directory) / "app.pyz"
    subprocess.run([sys.executable, str(HERE / "ops.py"), "build", str(package)],
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return package

def golive_all():
    directory = tempfile.mkdtemp(prefix="golive_build_")
    try:
        package = build_package(directory)
        for run in GOLIVE:
            run(package)
        print("GOLIVE_OK")
    finally:
        shutil.rmtree(directory, ignore_errors=True)

def demo():
    directory = tempfile.mkdtemp(prefix="golive_demo_")
    live = None
    try:
        package = build_package(directory)
        live = Live(package, "demo")
        live.install()
        live.start_source()
        live.submit()
        live.supervise()
        status = live.wait_complete()
        audit = live.audit()
        print(json.dumps({"status": status, "archive": live.read(),
                          "audit": {"acked": audit["acked"], "deleted": audit["deleted"]}},
                         ensure_ascii=False, sort_keys=True))
    finally:
        if live is not None:
            live.close()
        shutil.rmtree(directory, ignore_errors=True)

def main():
    regression()
    golive_all()
    if "--demo" in sys.argv:
        demo()

if __name__ == "__main__":
    main()
