import runpy
import sys

action = sys.argv.pop(1) if len(sys.argv) > 1 else ""
if action not in {"manual", "worker", "ops", "portal", "keeper"}:
    raise SystemExit("app.pyz manual|worker|ops|portal|keeper ...")
runpy.run_module(action, run_name="__main__")
