import signal
import subprocess
import sys
import time
from pathlib import Path

RESPAWN_SECONDS = 0.5

def main():
    """Portable user-level supervisor with launchd RunAtLoad/KeepAlive semantics.

    Starts the installed worker immediately and restarts it whenever it exits,
    so a killed or crashed collection process converges automatically. On macOS
    the same semantics are provided by the LaunchAgent installed via
    `ops agent PREFIX enable`; this keeper is the equivalent for other user
    sessions and for the go-live acceptance. Stop it with SIGTERM/SIGINT or by
    creating PREFIX/state/keeper.stop."""
    prefix = Path(sys.argv[1]).resolve()
    state = prefix / "state"
    state.mkdir(parents=True, exist_ok=True)
    stop = state / "keeper.stop"
    stop.unlink(missing_ok=True)
    child = None
    halted = []

    def handle(signum, frame):
        halted.append(signum)
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    while not halted and not stop.exists():
        child = subprocess.Popen([sys.executable, str(prefix / "app.pyz"),
                                  "worker", str(state)])
        while child.poll() is None:
            if halted or stop.exists():
                child.terminate()
                break
            time.sleep(0.1)
        child.wait()
        if halted or stop.exists():
            break
        time.sleep(RESPAWN_SECONDS)
    if child is not None and child.poll() is None:
        child.terminate()
        child.wait()

if __name__ == "__main__":
    main()
