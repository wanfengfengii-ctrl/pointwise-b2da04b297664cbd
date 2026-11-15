import os
import time
from pathlib import Path

def pause(root, label):
    if root is None:
        return
    directory = Path(root) / "hooks"
    hold = directory / (label + ".hold")
    if not hold.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (label + ".reached")).write_text(str(os.getpid()), encoding="ascii")
    while hold.exists():
        time.sleep(0.05)
