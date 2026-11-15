import json
import sys
from pathlib import Path
import archive
from collector import Client, once

def collect(root, url):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = archive.connect(root / "archive.sqlite")
    try:
        return once(Client(url), db, root)
    finally:
        db.close()

def main():
    action, root = sys.argv[1:3]
    if action == "collect":
        value = collect(root, sys.argv[3])
    elif action == "read":
        db = archive.connect(Path(root) / "archive.sqlite")
        try:
            value = archive.read(db)
        finally:
            db.close()
    else:
        raise SystemExit("manual.py collect ROOT URL | read ROOT")
    print(json.dumps(value, sort_keys=True))

if __name__ == "__main__":
    main()
