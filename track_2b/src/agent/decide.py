"""The two decision paths that are compared in the evaluation.

baseline: Apertus alone gets the policy, manual, mandate, supplier list, this
          month's payments and the invoice, and decides pay/escalate/reject.
harness:  Apertus extracts the fields -> badge enforces the mandate in code ->
          only if every hard check passes, Apertus reviews the content against
          the firm manual (+ lessons). Any model failure fails closed (escalate).
second look: an invoice the harness would pay goes to a human instead if the
          risk score (logistic regression on past payments, see risk.py) is high.
"""
import json

from . import badge, llm, prompts
from .common import DECISIONS
from .datagen import TODAY


def mandate_view(m: dict) -> dict:
    keys = ("name", "allowed_categories", "single_limit_chf", "monthly_limit_chf",
            "valid_from", "valid_until", "revoked", "revoked_at")
    return {k: m[k] for k in keys}


def supplier_view(suppliers: list) -> list:
    return [{"name": s["name"], "iban": s["iban"], "category": s["category"]} for s in suppliers]


def _decision(raw, allowed=DECISIONS) -> str:
    d = str(raw or "").strip().lower()
    return d if d in allowed else "escalate"


def baseline(case: dict, mandate: dict, suppliers: list, manual: str = prompts.MANUAL_GEN0, tag: str = "baseline") -> dict:
    user = prompts.BASELINE_USER.format(
        policy=prompts.POLICY, manual=manual, client_name=mandate["name"],
        mandate=json.dumps(mandate_view(mandate), ensure_ascii=False),
        suppliers=json.dumps(supplier_view(suppliers), ensure_ascii=False),
        prior=json.dumps(case["prior_payments"], ensure_ascii=False),
        invoice=case["invoice_text"])
    msgs = [{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}]
    try:
        out = llm.parse_json(llm.chat(msgs, max_tokens=300, tag=f"{tag}:{case['case_id']}"))
        return {"decision": _decision(out.get("decision")), "reason": str(out.get("reason", ""))}
    except Exception as e:  # fail closed
        return {"decision": "escalate", "reason": "model failure", "error": f"{type(e).__name__}: {e}"}


def extract(case: dict) -> dict:
    try:
        reply = llm.chat(prompts.extract_prompt(case["invoice_text"]), max_tokens=400,
                         tag=f"extract:{case['case_id']}")
        return llm.parse_json(reply)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def review(case: dict, mandate: dict, fields: dict, manual: str) -> dict:
    user = prompts.REVIEW_USER.format(manual=manual, client_name=mandate["name"], invoice=case["invoice_text"],
                                      fields=json.dumps(fields, ensure_ascii=False))
    msgs = [{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}]
    try:
        out = llm.parse_json(llm.chat(msgs, max_tokens=300, tag=f"review:{case['case_id']}"))
        return {"decision": _decision(out.get("decision"), ("pay", "escalate")), "reason": str(out.get("reason", ""))}
    except Exception as e:
        return {"decision": "escalate", "reason": "model failure", "error": f"{type(e).__name__}: {e}"}


def cite(case: dict, fields: dict) -> dict:
    """Ask Apertus where in the invoice it read the values a payment depends on (checked in code)."""
    user = prompts.CITE_USER.format(invoice=case["invoice_text"], iban=fields.get("vendor_iban"),
                                    invoice_no=fields.get("invoice_no"), total=fields.get("total_chf"))
    try:
        return llm.parse_json(llm.chat([{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}],
                                       max_tokens=200, tag=f"cite:{case['case_id']}"))
    except Exception as e:  # no quotes: the code check fails closed
        return {"error": f"{type(e).__name__}: {e}"}


SECOND_LOOK = 95.0  # risk score (0-100) from which an invoice the agent would pay goes to a human; chosen on the study's tune set


def second_look(result: dict, risk_score: float, factors=()) -> dict:
    """Hold a payment the harness would make when the risk score is high; other decisions are unchanged."""
    result = {**result, "risk_score": risk_score}
    if result["decision"] != "pay" or risk_score < SECOND_LOOK:
        return result
    why = ", ".join(f for f, _ in factors) or "unusual pattern"
    finding = {"severity": "escalate", "code": "RISK_SCORE",
               "msg": f"risk score {risk_score:.0f} >= {SECOND_LOOK:.0f} ({why}): second look by a human"}
    return {**result, "decision": "escalate", "findings": result["findings"] + [finding], "reason": finding["msg"]}


def harness(case: dict, mandate: dict, suppliers: list, manual: str, fields: dict = None,
            promoted: bool = False, grounded: bool = False) -> dict:
    fields = fields if fields is not None else extract(case)
    verdict, findings = badge.check(mandate, suppliers, case["prior_payments"], fields, today=TODAY)
    if promoted:
        findings = findings + badge.promoted_checks(mandate, fields)
        if verdict == "pass" and findings:
            verdict = "escalate"
    quotes = None
    if grounded:
        g = badge.grounding(case["invoice_text"], fields)
        if g and "IBAN" in g[0]["msg"]:  # a misread account is a reading problem for a human, not a checksum rejection
            findings = [f for f in findings if f["code"] != "R_IBAN_INVALID"]
        if verdict == "pass" and badge.possible_duplicate(case["prior_payments"], fields.get("vendor_name"),
                                                           fields.get("total_chf")):
            g = g + [{"severity": "escalate", "code": "E_POSSIBLE_DUPLICATE",
                      "msg": "same supplier and amount already paid this month"}]
        if verdict == "pass" and not g:  # only a payment candidate is worth the extra call
            quotes = cite(case, fields)
            g = badge.cited(case["invoice_text"], fields, quotes)
        if g:
            findings = findings + g
            verdict = "reject" if any(f["severity"] == "reject" for f in findings) else "escalate"
    result = {"fields": fields, "badge": verdict, "findings": findings, "badge_only": "pay" if verdict == "pass" else verdict}
    if quotes is not None:
        result["quotes"] = quotes
    if verdict != "pass":
        result.update(decision=verdict, reason="; ".join(f["msg"] for f in findings))
    else:
        r = review(case, mandate, fields, manual)
        result.update(decision=r["decision"], reason=r["reason"], review=r)
    return result
