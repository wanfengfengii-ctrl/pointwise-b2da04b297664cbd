import hashlib
import io
import json
import os
import plistlib
import subprocess
import sys
import zipfile
from pathlib import Path
from collector import Client

MODULES = ("__main__", "archive", "portal", "collector", "hooks", "manual", "worker", "ops")

def pack(sources):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(sources.items()):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()

def build(out):
    source = Path(__file__).resolve().parent
    raw = pack({name + ".py": (source / (name + ".py")).read_bytes() for name in MODULES})
    target = Path(out).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    Path(str(target) + ".sha256").write_text(digest + "\n", encoding="ascii")
    print(json.dumps({"package": str(target), "sha256": digest}))

def install(package, prefix):
    package, prefix = Path(package).resolve(), Path(prefix).resolve()
    raw = package.read_bytes()
    expected = Path(str(package) + ".sha256").read_text(encoding="ascii").strip()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("BAD_PACKAGE")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.testzip() is not None or set(archive.namelist()) != {x + ".py" for x in MODULES}:
            raise ValueError("BAD_PACKAGE")
    prefix.mkdir(parents=True, exist_ok=True)
    for name in ("state", "logs"):
        (prefix / name).mkdir(exist_ok=True)
    temporary = prefix / "app.pyz.new"
    temporary.write_bytes(raw)
    os.replace(str(temporary), str(prefix / "app.pyz"))
    print(str(prefix / "app.pyz"))

def agent(prefix, action):
    if sys.platform != "darwin":
        raise RuntimeError("LaunchAgent requires macOS")
    prefix = Path(prefix).resolve()
    label = "local.pointwise.capture." + hashlib.sha256(str(prefix).encode()).hexdigest()[:12]
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    domain = "gui/" + str(os.getuid())
    subprocess.run(["launchctl", "bootout", domain + "/" + label], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if action == "disable":
        plist.unlink(missing_ok=True)
        return
    if action != "enable":
        raise ValueError("AGENT_ACTION")
    config = {"Label": label,
              "ProgramArguments": [str(Path(sys.executable).resolve()), str(prefix / "app.pyz"),
                                   "worker", str(prefix / "state")],
              "RunAtLoad": True, "KeepAlive": True,
              "StandardOutPath": str(prefix / "logs" / "stdout.log"),
              "StandardErrorPath": str(prefix / "logs" / "stderr.log")}
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps(config))
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print(label)

def status(prefix):
    root = Path(prefix) / "state"
    path = root / "status.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"state": "queued" if (root / "task.json").exists() else "idle",
            "export_id": None, "receipt": None, "error": None}

def submit(prefix, url):
    Client(url)
    root = Path(prefix) / "state"
    root.mkdir(parents=True, exist_ok=True)
    task = root / "task.json"
    request = {"source_url": url.rstrip("/")}
    if task.exists():
        if json.loads(task.read_text(encoding="utf-8")) != request:
            raise ValueError("TASK_CONFLICT")
    else:
        temporary = root / "task.json.new"
        temporary.write_text(json.dumps(request, sort_keys=True), encoding="utf-8")
        os.replace(str(temporary), str(task))
    return status(prefix)

def main():
    action = sys.argv[1]
    if action == "build":
        build(sys.argv[2])
    elif action == "install":
        install(sys.argv[2], sys.argv[3])
    elif action == "agent":
        agent(sys.argv[2], sys.argv[3])
    elif action == "submit":
        print(json.dumps(submit(sys.argv[2], sys.argv[3]), sort_keys=True))
    elif action == "status":
        print(json.dumps(status(Path(sys.argv[2]).resolve()), sort_keys=True))
    elif action == "read":
        prefix = Path(sys.argv[2]).resolve()
        subprocess.run([sys.executable, str(prefix / "app.pyz"), "manual", "read",
                        str(prefix / "state")], check=True)
    else:
        raise SystemExit("ops.py build OUT | install PACKAGE PREFIX | agent PREFIX enable|disable | submit PREFIX URL | status PREFIX | read PREFIX")

if __name__ == "__main__":
    main()
