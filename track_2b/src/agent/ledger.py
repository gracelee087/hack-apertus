"""Append-only, hash-chained audit ledger: who acted for which client, on what basis."""
import json
import threading
from datetime import datetime, timezone

from .common import canonical, sha256

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.entries = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                self.entries = [json.loads(line) for line in f if line.strip()]

    def append(self, **fields) -> dict:
        with self.lock:
            prev = self.entries[-1]["hash"] if self.entries else GENESIS
            entry = {"seq": len(self.entries) + 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     **fields, "prev": prev}
            entry["hash"] = sha256(canonical(entry))
            self.entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry


def verify(entries: list):
    """Return (ok, index of first bad entry or None)."""
    prev = GENESIS
    for i, e in enumerate(entries):
        body = {k: v for k, v in e.items() if k != "hash"}
        if e.get("prev") != prev or sha256(canonical(body)) != e.get("hash"):
            return False, i
        prev = e["hash"]
    return True, None
