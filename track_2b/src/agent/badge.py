"""Hard checks ("badge"): what the mandate allows, enforced in code, not by the model."""
import math
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
    if isinstance(x, bool):  # JSON true is not an amount
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def amounts_in(text: str) -> set:
    """Every amount written in the text, as a number (1'234.50, 1 234,50, CHF 1,234.50, 570.00 ...)."""
    out = set()
    for m in re.finditer(r"\d[\d'’ .,]*\d|\d", text):
        s = re.sub(r"['’ ]", "", m.group())
        s = re.sub(r"[.,]", "", s[:-3]) + "." + s[-2:] if re.search(r"[.,]\d{2}$", s) else re.sub(r"[.,]", "", s)
        try:
            out.add(round(float(s), 2))
        except ValueError:
            pass
    return out


INVOICE_NO_LABELS = r"(?:Rechnungs-Nr\.|Facture n°|Fattura n\.|Invoice no\.|RECHNUNG|FACTURE|FATTURA|INVOICE)"
PAYMENT_REF = r"(?:Referenz|Référence|Riferimento|Reference)\W{0,3}(\d+)"


def invoice_no_written(text: str, invoice_no) -> bool:
    """The invoice number must stand next to an invoice-number label, and be neither the payment reference
    nor a heading word. Added after the study's final set (technical report, section 5)."""
    no = str(invoice_no or "").strip()
    if not no or no.upper() in ("RECHNUNG", "FACTURE", "FATTURA", "INVOICE") or no in re.findall(PAYMENT_REF, text):
        return False
    return re.search(INVOICE_NO_LABELS + r"\s*:?\s*" + re.escape(no) + r"\b", text) is not None


def grounding(text: str, fields: dict) -> list:
    """Fields the payment depends on must be written in the document: the IBAN as a whole IBAN, the total as
    an amount, the invoice number as an invoice number. A model that fills a gap (a total from net + VAT, a
    reference read as an IBAN or as the invoice number) is caught here. IBAN and total: added after the study's
    tune set; invoice number: added after its final set (technical report, section 5)."""
    ibans = {normalize_iban(m.group()) for m in re.finditer(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{1,4}){3,8}\b", text)}
    iban, total, no = normalize_iban(fields.get("vendor_iban")), _num(fields.get("total_chf")), fields.get("invoice_no")
    missing = ([f"IBAN {iban}"] if iban and iban not in ibans else []) + \
              ([f"total CHF {total:.2f}"] if total is not None and round(total, 2) not in amounts_in(text) else []) + \
              ([f"invoice number {no}"] if str(no or "").strip() and not invoice_no_written(text, no) else [])
    return [{"severity": "escalate", "code": "E_NOT_IN_DOCUMENT",
             "msg": f"not written in the invoice: {', '.join(missing)}"}] if missing else []


def already_paid(prior: list, vendor, invoice_no) -> bool:
    """Same supplier and invoice number among this month's payments."""
    inv_no = str(invoice_no or "").strip().upper()
    return bool(inv_no) and any(same_name(p["vendor"], vendor) and p["invoice_no"].upper() == inv_no for p in prior)


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
    inv_no = str(fields.get("invoice_no") or "").strip().upper()
    missing = [k for k, ok in (("supplier", vendor), ("IBAN", iban), ("total", total is not None), ("invoice number", inv_no),
                               ("net amount", _num(fields.get("net_chf")) is not None),
                               ("VAT", _num(fields.get("vat_chf")) is not None)) if not ok]
    if missing:  # without these the duplicate and sum checks cannot run
        add("escalate", "EXTRACTION_INCOMPLETE", f"could not read: {', '.join(missing)}")
    elif total <= 0:
        add("escalate", "EXTRACTION_INCOMPLETE", f"total CHF {total:.2f} is not a positive amount")

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

    if already_paid(prior, vendor, inv_no):
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
