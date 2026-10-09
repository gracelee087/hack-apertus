"""The two decision paths that are compared in the evaluation.

baseline: Apertus alone gets the policy, manual, mandate, supplier list, this
          month's payments and the invoice, and decides pay/escalate/reject.
harness:  Apertus extracts the fields -> badge enforces the mandate in code ->
          only if every hard check passes, Apertus reviews the content against
          the firm manual (+ lessons). Any model failure fails closed (escalate).
"""
import json

from . import badge, llm, prompts
from .common import DECISIONS


def mandate_view(m: dict) -> dict:
    keys = ("name", "allowed_categories", "single_limit_chf", "monthly_limit_chf",
            "valid_from", "valid_until", "revoked", "revoked_at")
    return {k: m[k] for k in keys}


def supplier_view(suppliers: list) -> list:
    return [{"name": s["name"], "iban": s["iban"], "category": s["category"]} for s in suppliers]


def _decision(raw, allowed=DECISIONS) -> str:
    d = str(raw or "").strip().lower()
    return d if d in allowed else "escalate"


def baseline(case: dict, mandate: dict, suppliers: list, manual: str = prompts.MANUAL_GEN0) -> dict:
    user = prompts.BASELINE_USER.format(
        policy=prompts.POLICY, manual=manual, client_name=mandate["name"],
        mandate=json.dumps(mandate_view(mandate), ensure_ascii=False),
        suppliers=json.dumps(supplier_view(suppliers), ensure_ascii=False),
        prior=json.dumps(case["prior_payments"], ensure_ascii=False),
        invoice=case["invoice_text"])
    msgs = [{"role": "system", "content": prompts.ROLE}, {"role": "user", "content": user}]
    try:
        out = llm.parse_json(llm.chat(msgs, max_tokens=300, tag=f"baseline:{case['case_id']}"))
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


def harness(case: dict, mandate: dict, suppliers: list, manual: str, fields: dict = None,
            promoted: bool = False) -> dict:
    fields = fields if fields is not None else extract(case)
    verdict, findings = badge.check(mandate, suppliers, case["prior_payments"], fields)
    if promoted:
        findings = findings + badge.promoted_checks(mandate, fields)
        if verdict == "pass" and findings:
            verdict = "escalate"
    result = {"fields": fields, "badge": verdict, "findings": findings, "badge_only": "pay" if verdict == "pass" else verdict}
    if verdict != "pass":
        result.update(decision=verdict, reason="; ".join(f["msg"] for f in findings))
    else:
        r = review(case, mandate, fields, manual)
        result.update(decision=r["decision"], reason=r["reason"], review=r)
    return result
