"""Shared helpers: paths, IBAN checksum, canonical JSON, hashing."""
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("APP_ROOT", Path(__file__).resolve().parents[2]))
DATA = ROOT / "data"
RESULTS = Path(os.environ.get("RESULTS_DIR", ROOT / "results"))

DECISIONS = ("pay", "escalate", "reject")
CATEGORIES = (
    "ingredients", "materials", "utilities", "rent", "software", "cloud",
    "telecom", "insurance", "consulting", "travel_private", "luxury_goods",
    "entertainment",
)


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_iban(iban) -> str:
    return re.sub(r"\s+", "", str(iban or "")).upper()


def iban_valid(iban) -> bool:
    s = normalize_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", s):
        return False
    if s.startswith("CH") and len(s) != 21:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def make_ch_iban(bban: str) -> str:
    digits = "".join(str(int(c, 36)) for c in bban + "CH00")
    check = 98 - int(digits) % 97
    return f"CH{check:02d}{bban}"


def format_iban(iban: str) -> str:
    s = normalize_iban(iban)
    return " ".join(s[i:i + 4] for i in range(0, len(s), 4))


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
