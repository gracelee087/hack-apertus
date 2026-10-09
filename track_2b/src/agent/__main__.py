"""CLI: python -m agent [gen|eval|holdout|study gen|tune|final|serve|all]"""
import os
import sys

from .common import RESULTS


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "all"
    if cmd in ("gen",):
        from . import datagen
        datagen.main()
    if cmd in ("eval", "all"):
        from . import evaluate
        evaluate.run()
    if cmd in ("holdout", "all"):
        from . import evaluate
        evaluate.holdout()
    if cmd == "study":
        from . import study
        study.main(argv[2:])
    if cmd in ("serve", "all"):
        import uvicorn
        if not (RESULTS / "metrics.json").exists():
            sys.exit("no results yet: run `python -m agent eval` first")
        port = int(os.environ.get("PORT", "8000"))
        print(f"\nWeb app: http://localhost:{port}  (Ctrl+C to stop)", flush=True)
        uvicorn.run("agent.app:app", host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main(sys.argv)
