import json
import sys
import time
from pathlib import Path
from manual import collect

def main():
    root = Path(sys.argv[1])
    while True:
        task = root / "task.json"
        status = root / "status.json"
        if task.exists():
            current = json.loads(status.read_text()) if status.exists() else {"state": "queued"}
            if current["state"] == "queued":
                try:
                    receipt = collect(root, json.loads(task.read_text())["source_url"])
                    value = {"state": "complete", "export_id": receipt["export_id"],
                             "receipt": receipt, "error": None}
                except Exception as error:
                    value = {"state": "failed", "export_id": None,
                             "receipt": None, "error": str(error)}
                status.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        time.sleep(0.1)

if __name__ == "__main__":
    main()
