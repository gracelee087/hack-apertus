"""Why a decision stands: the sources it relied on, as tags in the signed request.

Each tag names a source, the part of it that applied and who applied it:
  CUSTOMER REQUIREMENT - the client-signed mandate (a field of it)
  FIRM POLICY          - the payment policy (prompts.POLICY)
  RULE BOOK            - the firm manual, generation 0 (rule 1, rule 2)
  LESSON               - a lesson from reviewer notes (L1, L2, ...), or one a human signed off as a hard check
  SUPPLIER FILE        - the client's list of known suppliers and their accounts
  LEDGER               - this month's payments for the client
  INVOICE TEXT         - the invoice itself (Apertus quoted where it read the values, code compared the quotes)
  RISK MODEL           - the risk score and its threshold
  HUMAN                - the person who approved or rejected, the basis they gave and their note
Tags are derived in code from what actually ran; a rule is named only if Apertus cited it and it is in the manual.
"""
import re

CODE, APERTUS, CROSS, MODEL = "code", "Apertus", "Apertus + code", "risk model"

# finding code -> (source, ref, who)
FINDING = {
    "CREDENTIAL_INVALID": ("CUSTOMER REQUIREMENT", "client signature", CODE),
    "R_REVOKED": ("CUSTOMER REQUIREMENT", "revoked / valid_from-valid_until", CODE),
    "R_SCOPE": ("CUSTOMER REQUIREMENT", "allowed_categories", CODE),
    "E_LIMIT_SINGLE": ("CUSTOMER REQUIREMENT", "single_limit_chf", CODE),
    "E_LIMIT_MONTH": ("CUSTOMER REQUIREMENT", "monthly_limit_chf", CODE),
    "R_IBAN_INVALID": ("FIRM POLICY", "IBAN checksum", CODE),
    "E_IBAN_CHANGED": ("SUPPLIER FILE", "account on file", CODE),
    "E_NEW_VENDOR": ("SUPPLIER FILE", "known suppliers", CODE),
    "R_DUPLICATE": ("LEDGER", "same supplier and invoice number", CODE),
    "E_POSSIBLE_DUPLICATE": ("LEDGER", "same supplier and amount", CODE),
    "E_VAT_MISMATCH": ("LESSON", "signed off as a hard check: net + VAT = total", CODE),
    "E_WRONG_ADDRESSEE": ("LESSON", "signed off as a hard check: addressed to the client", CODE),
    "E_NOT_IN_DOCUMENT": ("INVOICE TEXT", "cross-check of what Apertus read", CROSS),
    "EXTRACTION_INCOMPLETE": ("INVOICE TEXT", "fields Apertus could not read", APERTUS),
    "CATEGORY_UNCLEAR": ("INVOICE TEXT", "expense type", APERTUS),
    "RISK_SCORE": ("RISK MODEL", "second look", MODEL),
}

# what a person may give as the basis for approving a held payment
HUMAN_BASIS = {
    "called_supplier": "called the supplier on a number already on file",
    "client_confirmed": "the client confirmed this payment",
    "other": "other (see note)",
}


def _tag(source, ref, by, detail=""):
    return {"source": source, "ref": ref, "by": by, "detail": detail}


def rules_cited(reason: str, manual: str) -> list:
    """Rules Apertus named in its reason ("manual rule 1", "L3", "L6-L10") that exist in the manual it was given."""
    lessons = {m.group(1): m.group(2).strip() for m in re.finditer(r"^L(\d+)\.\s*(.+)$", manual or "", re.M)}
    rules = {m.group(1): m.group(2).strip() for m in re.finditer(r"^(\d+)\.\s*(.+)$", manual or "", re.M)}
    seen, out = set(), []

    def add(source, ref, text):
        if ref not in seen:
            seen.add(ref)
            out.append(_tag(source, ref, APERTUS, text))
    for a, b in re.findall(r"\bL(\d+)\s*[-–]\s*L?(\d+)\b", reason or ""):
        for n in range(int(a), int(b) + 1):
            if str(n) in lessons:
                add("LESSON", f"L{n}", lessons[str(n)])
    for n in re.findall(r"\bL(\d+)\b", reason or ""):
        if n in lessons:
            add("LESSON", f"L{n}", lessons[n])
    for n in re.findall(r"\brule\s+(\d+)\b", reason or "", re.I):
        if n in rules:
            add("RULE BOOK", f"rule {n}", rules[n])
    return out


def of_decision(h: dict, mandate: dict, manual: str, grounded: bool = True) -> list:
    """Tags for an agent decision (pay, escalate or reject) from the harness result h."""
    findings, decision = h.get("findings") or [], h.get("decision")
    tags = []
    for f in findings:
        source, ref, by = FINDING.get(f.get("code"), ("FIRM POLICY", f.get("code", ""), CODE))
        tags.append(_tag(source, ref, by, f.get("msg", "")))
    hard = [f for f in findings if f.get("code") != "RISK_SCORE"]
    if hard:
        return tags
    # every hard check passed: name what was checked, then what Apertus relied on
    passed = [_tag("CUSTOMER REQUIREMENT", "mandate", CODE,
                   f"in force, category allowed, within CHF {mandate['single_limit_chf']:,.0f} per invoice "
                   f"and CHF {mandate['monthly_limit_chf']:,.0f} per month"),
              _tag("SUPPLIER FILE", "account on file", CODE, "known supplier, IBAN matches the account on file"),
              _tag("LEDGER", "this month", CODE, "not paid before this month")]
    if grounded:
        passed.append(_tag("INVOICE TEXT", "cross-check of what Apertus read", CROSS,
                           "Apertus quoted where it read the IBAN, invoice number and total; code found the quotes in the invoice"))
    cited = rules_cited(h.get("reason", ""), manual)
    review = "pay" if decision == "pay" or any(f.get("code") == "RISK_SCORE" for f in findings) else decision
    if h.get("reason") == "model failure":
        passed.append(_tag("RULE BOOK", "no review", APERTUS, "the Apertus review failed, so the invoice is held (fail closed)"))
    else:
        passed += cited or [_tag("RULE BOOK", "no rule cited", APERTUS, f"Apertus review: {review}")]
    if h.get("risk_score") is not None and decision == "pay":
        passed.append(_tag("RISK MODEL", "below threshold", MODEL, f"risk score {h['risk_score']:.0f}"))
    return passed + tags


def of_intake(intake: dict) -> list:
    """How the text of an invoice file was obtained (none for an invoice that arrived as text)."""
    if not intake:
        return []
    how = "OCR (Tesseract)" if intake["method"] == "ocr" else "the PDF's own text"
    return [_tag("INVOICE TEXT", "OCR" if intake["method"] == "ocr" else "PDF text", "OCR" if intake["method"] == "ocr" else CODE,
                 f"read by {how} from file sha256 {intake['file_sha256'][:12]}, {intake['pages']} page(s); "
                 + ("Apertus read the image itself and every check compares its reading with this text"
                    if intake.get("reader") else "every check compares against this text"))]


def of_human(name: str, basis: str, note: str, action: str) -> list:
    detail = HUMAN_BASIS.get(basis, basis) if basis else ""
    return [_tag("HUMAN", f"{action} by {name}", "person", "; ".join(x for x in (detail, note.strip()) if x))]
