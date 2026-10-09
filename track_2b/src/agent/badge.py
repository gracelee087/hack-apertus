"""Hard checks ("badge"): what the mandate allows, enforced in code, not by the model."""
import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

from . import credential
from .common import CATEGORIES, iban_valid, normalize_iban

LEGAL_FORMS = r"\b(ag|gmbh|sa|sàrl|sarl|sagl|ltd|inc)\b"


def _norm(name) -> str:
    s = re.sub(LEGAL_FORMS, "", str(name or "").lower())
    return re.sub(r"[^a-z0-9à-ÿ]+", " ", s).strip()


def same_name(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.85)


def _fold(name) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    return _norm(s)


def addressed_to(addressee, client_name) -> bool:
    """Tolerates accents and an appended address ("Cabinet Favre Sarl, 1003 Lausanne")."""
    a, c = _fold(addressee), _fold(client_name)
    return bool(a and c) and (c in a or SequenceMatcher(None, a, c).ratio() >= 0.85)


def promoted_checks(mandate: dict, fields: dict) -> list:
    """Lessons from the train split that a human signed off as hard rules (see technical report §5)."""
    findings = []
    net, vat, total = (_num(fields.get(k)) for k in ("net_chf", "vat_chf", "total_chf"))
    if None not in (net, vat, total) and abs(net + vat - total) > 0.05:
        findings.append({"severity": "escalate", "code": "E_VAT_MISMATCH",
                         "msg": f"net {net:.2f} + VAT {vat:.2f} != total {total:.2f}"})
    if fields.get("addressee") and not addressed_to(fields["addressee"], mandate["name"]):
        findings.append({"severity": "escalate", "code": "E_WRONG_ADDRESSEE",
                         "msg": f"invoice addressed to {fields['addressee']}, not {mandate['name']}"})
    return findings


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def check(mandate: dict, suppliers: list, prior: list, fields: dict, today: date = date(2026, 10, 9)):
    """Return (verdict, findings). verdict: reject | escalate | pass."""
    findings = []

    def add(severity, code, msg):
        findings.append({"severity": severity, "code": code, "msg": msg})

    if not credential.verify(mandate):
        add("reject", "CREDENTIAL_INVALID", "mandate credential signature does not verify")
    if mandate.get("revoked"):
        add("reject", "R_REVOKED", f"mandate revoked on {mandate.get('revoked_at')}")
    elif not (mandate["valid_from"] <= today.isoformat() <= mandate["valid_until"]):
        add("reject", "R_REVOKED", "mandate not valid today")

    iban = normalize_iban(fields.get("vendor_iban"))
    total = _num(fields.get("total_chf"))
    vendor = fields.get("vendor_name")
    if not iban or total is None or not vendor:
        add("escalate", "EXTRACTION_INCOMPLETE", "could not read supplier, IBAN or total")

    known = next((s for s in suppliers if same_name(s["name"], vendor)), None) \
        or next((s for s in suppliers if iban and s["iban"] == iban), None)
    category = known["category"] if known else fields.get("category")

    if category not in CATEGORIES:
        add("escalate", "CATEGORY_UNCLEAR", "could not classify the expense")
    elif category not in mandate["allowed_categories"]:
        add("reject", "R_SCOPE", f"category '{category}' is outside the mandate")
    if iban and not iban_valid(iban):
        add("reject", "R_IBAN_INVALID", "IBAN fails checksum")
    elif known and iban and iban != known["iban"]:
        add("escalate", "E_IBAN_CHANGED", f"IBAN differs from the account on file for {known['name']}")
    if not known and category in mandate["allowed_categories"]:
        add("escalate", "E_NEW_VENDOR", "supplier not in the client's supplier list")

    inv_no = str(fields.get("invoice_no") or "").strip().upper()
    if inv_no and any(same_name(p["vendor"], vendor) and p["invoice_no"].upper() == inv_no for p in prior):
        add("reject", "R_DUPLICATE", f"invoice {inv_no} already paid this month")

    if total is not None:
        if total > mandate["single_limit_chf"]:
            add("escalate", "E_LIMIT_SINGLE", f"CHF {total:.2f} above single limit CHF {mandate['single_limit_chf']}")
        spent = sum(p["amount_chf"] for p in prior)
        if spent + total > mandate["monthly_limit_chf"]:
            add("escalate", "E_LIMIT_MONTH", f"month total would be CHF {spent + total:.2f} "
                                            f"(limit CHF {mandate['monthly_limit_chf']})")

    if any(f["severity"] == "reject" for f in findings):
        return "reject", findings
    if findings:
        return "escalate", findings
    return "pass", findings
