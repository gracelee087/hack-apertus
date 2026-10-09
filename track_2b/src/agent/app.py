"""Reviewer web app for the fiduciary firm: client portfolio dashboard, per-client pages, evaluation.

Client status and risk signals are computed from the signed ledger, so the dashboard shows exactly
what the agents did and what the gate let through.
"""
import json
import secrets
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import badge, credential, decide, gate, identity, lessons, llm, pain001, risk
from .common import DATA, RESULTS, load_json, normalize_iban, read_jsonl, sha256
from .datagen import TODAY
from .desk import ACTIONS, Desk, seat_of
from .evaluate import PRODUCTION
from .ledger import Ledger, audit_identities, ledger_did, verify

app = FastAPI(title="Treuhand payment agents")
threading.Thread(target=risk.scorer, daemon=True).start()  # fit the risk model once, in the background
lock = threading.Lock()
LEDGER = Ledger(RESULTS / "ledger.jsonl")
STATE = RESULTS / "state.json"
FIRM_WIDE = "*"

RISKS = {  # finding code -> (label, kind)
    "E_IBAN_CHANGED": ("Bank details changed", "fraud"),
    "R_IBAN_INVALID": ("Invalid IBAN", "fraud"),
    "R_DUPLICATE": ("Duplicate invoice", "fraud"),
    "E_VAT_MISMATCH": ("Amounts do not add up", "fraud"),
    "E_WRONG_ADDRESSEE": ("Addressed to another company", "fraud"),
    "MODEL_FLAGGED": ("Flagged by Apertus review (e.g. pressure, secrecy)", "fraud"),
    "RISK_SCORE": ("High risk score (second look)", "fraud"),
    "E_NEW_VENDOR": ("New supplier", "control"),
    "E_LIMIT_SINGLE": ("Above single-payment limit", "control"),
    "E_LIMIT_MONTH": ("Monthly limit exceeded", "control"),
    "R_SCOPE": ("Outside the mandate's scope", "control"),
    "R_REVOKED": ("Mandate revoked", "control"),
    "CREDENTIAL_INVALID": ("Mandate signature invalid", "control"),
    "EXTRACTION_INCOMPLETE": ("Could not read the invoice", "control"),
    "CATEGORY_UNCLEAR": ("Expense type unclear", "control"),
}
# days a human needs before the money can go: phone the supplier on a known number, or ask the client
VERIFY_DAYS = {"E_IBAN_CHANGED": 2, "MODEL_FLAGGED": 2, "RISK_SCORE": 2, "E_WRONG_ADDRESSEE": 2, "E_VAT_MISMATCH": 2, "E_NEW_VENDOR": 2,
               "E_LIMIT_SINGLE": 3, "E_LIMIT_MONTH": 3, "R_SCOPE": 3}
BANK_DAYS = 1  # the payment file goes to the bank a day before the due date
OUTLIER_RATIO = 2.5  # invoice at least this many times the supplier's median amount for this client


def _read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def state():
    s = _read(STATE, None)
    if s is None:
        info = _read(RESULTS / "metrics.json", {}).get("lessons", {})
        s = {"mandates": load_json(DATA / "mandates.json"), "pending_notes": [], "manual_generation": 1,
             "lessons": [{"seat": FIRM_WIDE, "rule": r, "from_manual_generation": 1} for r in info.get("rules", [])],
             "approved": {}, "desk": _read(RESULTS / "desk.json", {})}
        _write(STATE, s)
    return s


def desk_of(s) -> Desk:
    return Desk(LEDGER, {m["client_id"]: m for m in s["mandates"]}, state=s["desk"])


def save(s, desk=None):
    if desk is not None:
        s["desk"] = desk.dump()
    _write(STATE, s)


def mandate(s, cid):
    m = next((m for m in s["mandates"] if m["client_id"] == cid), None)
    if m is None:
        raise HTTPException(404, "unknown client")
    return m


def manual_text(s, seat):
    """Firm-wide lessons and this seat's own lessons bind; other seats' lessons are reference only."""
    binding = [x["rule"] for x in s["lessons"] if x["seat"] in (FIRM_WIDE, seat)]
    reference = [f"[{x['seat']}] {x['rule']}" for x in s["lessons"] if x["seat"] not in (FIRM_WIDE, seat)]
    return lessons.manual_with_lessons(binding, s["manual_generation"], reference)


def names(s):
    sup = load_json(DATA / "vendors.json")
    return [m["name"] for m in s["mandates"]] + [v["name"] for vs in sup.values() for v in vs]


def queue_items():
    return _read(RESULTS / "queue.json", [])


def risk_codes(e) -> list:
    codes = [c for c in e.get("checks") or [] if c in RISKS]
    if e.get("decision") == "escalate" and not codes:
        codes = ["MODEL_FLAGGED"]
    return codes


def client_view(s, d, cid, entries=None):
    """Status, money and risk signals for one client, computed from the ledger."""
    m = mandate(s, cid)
    entries = [e for e in (entries if entries is not None else LEDGER.entries) if e.get("client_id") == cid]
    voided = {e.get("case_id") for e in entries if e.get("action") == "payment.voided"}
    decided = [e for e in entries if e.get("action") == "payment.decide" and e.get("gate") == "OK"
               and e.get("case_id") not in voided]
    auto = [e for e in decided if not e.get("grant_ref")]
    paid = [e for e in decided if e.get("decision") == "pay"]
    risks = Counter(c for e in auto for c in risk_codes(e))
    q = [i for i in queue_items() if i["client_id"] == cid]
    seat = seat_of(cid)
    return {
        "client_id": cid, "name": m["name"], "city": m["city"], "lang": m["lang"],
        "owner": {"name": identity.owner_of(cid), "title": identity.OWNER_TITLE},
        "mandate": {k: m[k] for k in ("allowed_categories", "single_limit_chf", "monthly_limit_chf", "valid_from",
                                      "valid_until", "revoked", "revoked_at", "client_did")},
        "mandate_ok": credential.verify(m),
        "agent": {"seat": seat, "generation": d.generations.get(seat, 0), "did": d.agent_did(seat),
                  "active": d.generations.get(seat, 0) > 0 and d.active(seat)},
        "invoices": len(auto), "paid": sum(e.get("decision") == "pay" for e in auto),
        "escalated": sum(e.get("decision") == "escalate" for e in auto),
        "rejected": sum(e.get("decision") == "reject" for e in auto),
        "approved_by_human": len(decided) - len(auto),
        "paid_chf": round(sum(e.get("amount_chf") or 0 for e in paid), 2),
        "held_chf": round(sum(e.get("amount_chf") or 0 for e in auto if e.get("decision") != "pay"), 2),
        "queue_open": sum(i["status"] == "open" for i in q),
        "gate_rejections": sum(e.get("action") == "request.rejected" for e in entries),
        "risks": {c: {"count": n, "label": RISKS[c][0], "kind": RISKS[c][1]} for c, n in risks.most_common()},
        "fraud_signals": sum(n for c, n in risks.items() if RISKS[c][1] == "fraud"),
    }


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""


class Text(BaseModel):
    invoice_text: str


class Note(BaseModel):
    note: str = ""


class Settings(BaseModel):
    max_single_chf: float
    actions: list


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


def _days_until(iso):
    try:
        return (datetime.fromisoformat(str(iso)).date() - TODAY).days
    except ValueError:
        return None


# --- portfolio -----------------------------------------------------------------------------

@app.get("/api/overview")
def overview():
    s = state()
    d = desk_of(s)
    entries = LEDGER.entries
    clients = [client_view(s, d, m["client_id"], entries) for m in s["mandates"]]
    total = Counter()
    for c in clients:
        for code, r in c["risks"].items():
            total[code] += r["count"]
    names_by_id = {c["client_id"]: c["name"] for c in clients}
    scores = _read(RESULTS / "risk_model.json", {}).get("scores", {})
    alerts = []  # open approvals, fraud signals first, then by amount
    for i in queue_items():
        if i["status"] != "open":
            continue
        codes = [c for c in i.get("checks") or [] if c in RISKS] or ["MODEL_FLAGGED"]
        fraud = any(RISKS[c][1] == "fraud" for c in codes)
        days_left = _days_until(i.get("due_date"))
        sc = scores.get(i["case_id"]) or {"risk_score": i.get("risk_score"), "top_factors": i.get("risk_factors", []),
                                          "expected_loss_chf": None if i.get("risk_score") is None or i.get("amount_chf") is None
                                          else round(i["risk_score"] / 100 * float(i["amount_chf"]), 2)}
        need = max(VERIFY_DAYS.get(c, 1) for c in codes) + BANK_DAYS
        alerts.append({"case_id": i["case_id"], "client_id": i["client_id"], "client": names_by_id.get(i["client_id"]),
                       "vendor": i["vendor"], "amount_chf": i["amount_chf"], "fraud": fraud,
                       "due_date": i.get("due_date"), "days_left": days_left, "days_needed": need,
                       "slack": None if days_left is None else days_left - need,
                       "risk_score": sc.get("risk_score"), "expected_loss_chf": sc.get("expected_loss_chf"),
                       "risk_factors": sc.get("top_factors") or [],
                       "risks": [{"code": c, "label": RISKS[c][0], "kind": RISKS[c][1]} for c in codes]})
    # priority: fraud first, then least time to spare before the due date, then amount
    alerts.sort(key=lambda a: (not a["fraud"], a["slack"] if a["slack"] is not None else 10**6, -(a["amount_chf"] or 0)))
    for n, a in enumerate(alerts, 1):
        a["rank"] = n
    return {"firm": identity.FIRM_NAME, "model": llm.model_name(), "llm_mode": llm.mode(), "clients": clients,
            "alerts": alerts, "reviewer": identity.RESPONSIBLE_HUMAN,
            "risk_totals": {c: {"count": n, "label": RISKS[c][0], "kind": RISKS[c][1]} for c, n in total.most_common()},
            "totals": {k: sum(c[k] for c in clients) for k in ("invoices", "paid", "escalated", "rejected", "queue_open",
                                                               "paid_chf", "held_chf", "fraud_signals", "gate_rejections")},
            "ledger_ok": verify(entries)[0], "audit_ok": audit_identities(entries)["ok"]}


@app.get("/api/clients/{cid}")
def client(cid: str):
    s = state()
    d = desk_of(s)
    view = client_view(s, d, cid)
    seat = seat_of(cid)
    entries = [e for e in LEDGER.entries if e.get("client_id") == cid]
    tests = [e for e in entries if e.get("action") == "agent.self_test" and e.get("generation") == d.generations.get(seat)]
    decisions = [e for e in entries if e.get("action") in ("payment.decide", "request.rejected") and e.get("case_id")]
    risky = [dict(e, risk=[{"code": c, "label": RISKS[c][0], "kind": RISKS[c][1]} for c in risk_codes(e)])
             for e in decisions if e.get("action") == "payment.decide" and risk_codes(e)]
    return {**view,
            "agent": {**view["agent"], "card": d.card(seat), "badge": d.badge(seat),
                      "card_ok": bool(d.card(seat)) and identity.verify_doc(d.card(seat)),
                      "badge_ok": bool(d.badge(seat)) and identity.verify_doc(d.badge(seat)),
                      "revoked": [x for x in d.revoked_agents if d.cards.get(x, {}).get("seat") == seat],
                      "self_test": ({k: tests[-1][k] for k in ("ts", "result", "passed")} if tests else None),
                      "all_actions": list(ACTIONS)},
            "decisions": decisions[::-1], "risky": risky[::-1],
            "queue": [i for i in queue_items() if i["client_id"] == cid],
            "lessons": {"binding": [x for x in s["lessons"] if x["seat"] in (FIRM_WIDE, seat)],
                        "reference": [x for x in s["lessons"] if x["seat"] not in (FIRM_WIDE, seat)],
                        "pending": [n["note"] for n in s["pending_notes"] if n["client_id"] == cid],
                        "manual_sha256": lessons.fingerprint(manual_text(s, seat))},
            "rejections": [e for e in entries if e.get("action") == "request.rejected"][-8:][::-1],
            "files": [f for f in payments() if f["file"].startswith(cid)],
            "ledger": entries[-80:][::-1]}


# --- per-client agent: settings, kill switch, replacement, attacks ---------------------------

@app.post("/api/clients/{cid}/agent/settings")
def agent_settings(cid: str, st: Settings):
    if st.max_single_chf <= 0 or any(a not in ACTIONS for a in st.actions):
        raise HTTPException(400, "invalid settings")
    with lock:
        s = state()
        mandate(s, cid)
        d = desk_of(s)
        if not d.active(seat_of(cid)):
            raise HTTPException(409, "no active agent for this client")
        d.update_badge(cid, st.max_single_chf, st.actions)
        save(s, d)
        return {"ok": True, "badge": d.badge(seat_of(cid))}


@app.post("/api/clients/{cid}/agent/kill")
def kill(cid: str, n: Note):
    with lock:
        s = state()
        d = desk_of(s)
        if not d.active(seat_of(cid)):
            raise HTTPException(409, "agent already revoked")
        d.kill(cid, n.note)
        if n.note.strip():
            s["pending_notes"].append({"client_id": cid, "note": lessons.mask(n.note, names(s))})
        save(s, d)
        return {"ok": True}


@app.post("/api/clients/{cid}/agent/onboard")
def onboard(cid: str):
    """One click: deploy (or replace) this client's agent; the badge is derived from the client's mandate."""
    with lock:
        s = state()
        mandate(s, cid)
        d = desk_of(s)
        seat = seat_of(cid)
        steps = d.onboard(cid, manual_sha256=lessons.fingerprint(manual_text(s, seat)))
        save(s, d)
        return {"ok": all(x["ok"] for x in steps), "steps": steps, "generation": d.generations.get(seat, 0)}


@app.post("/api/clients/{cid}/attack/{kind}")
def attack(cid: str, kind: str):
    """Demo buttons: resend this agent's last request, or forge a payment in its name."""
    with lock:
        s = state()
        d = desk_of(s)
        seat = seat_of(cid)
        if kind == "replay":
            last = d.last_stamp.get(seat)
            if not last:
                raise HTTPException(400, "no request to replay yet")
            res = d.submit(last["stamp"], last["payload"], attack="replay")
        elif kind == "forged":
            payload = {"case_id": "FORGED", "decision": "pay", "vendor": "Attacker GmbH", "client_id": cid,
                       "iban": "CH9300762011623852957", "amount_chf": 9500.0, "executes_payment": True}
            st = identity.stamp(identity.key("attacker"), "payment.decide", cid, payload)
            st["agent"] = d.agent_did(seat)
            res = d.submit(st, payload, attack="forged_signature")
        else:
            raise HTTPException(404, "unknown attack")
        save(s, d)
        return res


@app.post("/api/clients/{cid}/revoke")
def revoke_mandate(cid: str):
    """Demo of the client's side: the client re-issues its signed mandate as revoked."""
    with lock:
        s = state()
        m = mandate(s, cid)
        m["revoked"], m["revoked_at"] = True, datetime.now().date().isoformat()
        m["signature"] = credential.sign(m)
        LEDGER.append(actor=m["client_did"], action="mandate.revoked", client_id=cid,
                      note=f"{m['name']} revoked its mandate")
        save(s)
        return {"ok": True}


def prior_payments(cid) -> list:
    """This month's payments for one client, read from the ledger: the reference run and everything since."""
    entries = [e for e in LEDGER.entries if e.get("client_id") == cid]
    voided = {e.get("case_id") for e in entries if e.get("action") == "payment.voided"}
    return [{"vendor": e.get("vendor") or "", "invoice_no": str(e.get("invoice_no") or ""),
             "amount_chf": e.get("amount_chf") or 0} for e in entries
            if e.get("action") == "payment.decide" and e.get("gate") == "OK" and e.get("decision") == "pay"
            and e.get("case_id") not in voided]


@app.post("/api/clients/{cid}/process")
def process(cid: str, inv: Text):
    with lock:
        s = state()
        m = mandate(s, cid)
        manual = manual_text(s, seat_of(cid))
        case = {"case_id": f"LIVE-{secrets.token_hex(3).upper()}", "client_id": cid, "invoice_text": inv.invoice_text,
                "prior_payments": prior_payments(cid)}
    sup = load_json(DATA / "vendors.json").get(cid, [])
    h = decide.harness(case, m, sup, manual, promoted=True)  # model calls run outside the lock
    f = h["fields"]
    amount = badge._num(f.get("total_chf"))
    score, factors = risk.scorer().score(case, f)
    with lock:
        s = state()
        m = mandate(s, cid)
        d = desk_of(s)
        if h["decision"] == "pay":  # other payments may have been made while Apertus was reading: check again
            verdict, findings = badge.check(m, sup, prior_payments(cid), f)
            if verdict != "pass":
                h = {**h, "decision": verdict, "findings": findings, "reason": "; ".join(x["msg"] for x in findings)}
        h = decide.second_look(h, score, factors)
        item = {"case_id": case["case_id"], "client_id": cid, "client": m["name"], "vendor": f.get("vendor_name"),
                "amount_chf": None if amount is None else round(amount, 2), "iban": normalize_iban(f.get("vendor_iban")),
                "invoice_no": f.get("invoice_no"), "due_date": f.get("due_date"),
                "checks": [x["code"] for x in h["findings"]], "reason": h.get("reason", ""),
                "risk_score": score, "risk_factors": factors, "invoice_text": inv.invoice_text, "status": "open"}
        log = dict(reason=h.get("reason", ""), checks=item["checks"], manual_sha256=lessons.fingerprint(manual),
                   model=llm.model_name(), risk_score=score)
        if h["decision"] == "pay":
            res = _pay(s, d, m, item, "live", {}, **log)
            queued = not res["ok"]  # whatever the agent could not carry out waits for a human
            if queued:
                item["reason"] = f"not executed ({res['gate']}); " + item["reason"]
        else:
            res = d.request(cid, "payment.decide",
                            {"case_id": item["case_id"], "decision": h["decision"], "vendor": item["vendor"],
                             "iban": item["iban"], "amount_chf": item["amount_chf"], "executes_payment": False},
                            case_id=item["case_id"], decision=h["decision"], vendor=item["vendor"], iban=item["iban"],
                            amount_chf=item["amount_chf"], invoice_no=item["invoice_no"], **log)
            queued = h["decision"] == "escalate"
        if queued:
            _write(RESULTS / "queue.json", queue_items() + [item])
        save(s, d)
    seat = seat_of(cid)
    return {**h, "case_id": case["case_id"], "risk_factors": factors, "gate": res["gate"], "executed": res["ok"] and h["decision"] == "pay",
            "recorded": res["ok"], "queued": queued, "file": res.get("file"), "agent_did": d.agent_did(seat),
            "generation": d.generations.get(seat, 0)}


@app.post("/api/clients/{cid}/lessons/publish")
def publish(cid: str):
    with lock:
        s = state()
        mandate(s, cid)
        notes = [n["note"] for n in s["pending_notes"] if n["client_id"] == cid]
        if not notes:
            raise HTTPException(400, "no pending reviewer notes for this client")
        seat = seat_of(cid)
        s["manual_generation"] += 1
        s["lessons"] = (s["lessons"] + [{"seat": seat, "rule": r, "from_manual_generation": s["manual_generation"]}
                                        for r in lessons.distill(notes)])[-20:]
        s["pending_notes"] = [n for n in s["pending_notes"] if n["client_id"] != cid]
        save(s)
        sha = lessons.fingerprint(manual_text(s, seat))
        LEDGER.append(actor=identity.owner_of(cid), action="manual.published", client_id=cid, seat=seat,
                      generation=s["manual_generation"], manual_sha256=sha,
                      note="new lessons bind this seat; other seats see them for reference only")
        return {"ok": True, "manual_sha256": sha}


# --- approval queue --------------------------------------------------------------------------

@app.post("/api/queue/{case_id}")
def review(case_id: str, r: Review):
    if r.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    with lock:
        q, s = queue_items(), state()
        item = next((i for i in q if i["case_id"] == case_id), None)
        if item is None or item["status"] != "open":
            raise HTTPException(404, "no open item")
        cid = item["client_id"]
        m = mandate(s, cid)
        d = desk_of(s)
        if r.action == "approve":
            amount = badge._num(item["amount_chf"])
            ref = d.grant(cid, case_id, amount or 0, item["iban"], r.note)
            res = _pay(s, d, m, {**item, "amount_chf": amount}, "approved", {"grant_ref": ref}, grant_ref=ref,
                       reason=f"approved by human: {r.note}")
            if not res["ok"]:  # the item stays open
                save(s, d)
                raise HTTPException(409, f"payment not made: {res['gate']}")
        else:
            LEDGER.append(actor=identity.owner_of(cid), action="consent.denied", client_id=cid, case_id=case_id,
                          amount_chf=item.get("amount_chf"), note=r.note)
            if r.note.strip():
                s["pending_notes"].append({"client_id": cid, "note": lessons.mask(r.note, names(s))})
        item["status"] = "approved" if r.action == "approve" else "rejected"
        item["note"] = r.note
        _write(RESULTS / "queue.json", q)
        save(s, d)
        return {"ok": True}


FILE_SUFFIX = {"approved": "A", "live": "L"}  # payments after the reference run: approved by a human / paid live


def _pay(s, d, m, item, kind, extra, **log):
    """Decide and file one payment as one step: the decision only stands if its payment file is issued."""
    cid, sfx = m["client_id"], FILE_SUFFIX[kind]
    items = s.setdefault(kind, {}).get(cid, []) + [item]
    amount = item["amount_chf"]
    xml = None
    if gate._positive(amount):  # otherwise the gate refuses the request below
        pays = [{"end_to_end_id": f"{i['case_id']}-{sfx}", "creditor": i["vendor"] or "", "iban": i["iban"],
                 "amount_chf": float(i["amount_chf"]), "reference": i["case_id"]} for i in items]
        xml = pain001.build(m, pays, f"{cid}-{TODAY:%Y%m%d}-{sfx}", datetime.now(), TODAY.isoformat())
        errors = pain001.validate(xml)
        if errors:
            LEDGER.append(actor="system", action="payment.file.blocked", client_id=cid, case_id=item["case_id"],
                          validation_errors=errors)
            return {"ok": False, "gate": "FILE_INVALID"}
    res = d.request(cid, "payment.decide",
                    {"case_id": item["case_id"], "decision": "pay", "vendor": item["vendor"], "iban": item["iban"],
                     "amount_chf": amount, "executes_payment": True, **extra},
                    case_id=item["case_id"], decision="pay", vendor=item["vendor"], iban=item["iban"],
                    amount_chf=amount, invoice_no=item.get("invoice_no"), **log)
    if not res["ok"]:
        return res
    name = f"{cid}_pain001_{kind}.xml"
    filed = d.request(cid, "payment.file", {"file_sha256": sha256(xml), "transactions": len(items)},
                      file=f"payments/{name}", transactions=len(items), case_id=item["case_id"])
    if not filed["ok"]:
        LEDGER.append(actor="system", action="payment.voided", client_id=cid, case_id=item["case_id"],
                      note=f"payment file refused by the gate ({filed['gate']}); the decision does not stand")
        return filed
    s[kind].setdefault(cid, []).append(item)
    path = RESULTS / "payments" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    return {**res, "file": name}


# --- evaluation, ledger, files -----------------------------------------------------------------

@app.get("/api/evaluation")
def evaluation():
    s = state()
    return {"metrics": _read(RESULTS / "metrics.json", {}), "holdout": _read(RESULTS / "holdout.json", None),
            "agents": agents_by_client(s), "manual": manual_text(s, FIRM_WIDE), "llm_mode": llm.mode(),
            "model": llm.model_name()}


def agents_by_client(s) -> list:
    """One row per client: its agent, how it scored on that client's test invoices, what it did this month."""
    preds = read_jsonl(RESULTS / "predictions.jsonl") if (RESULTS / "predictions.jsonl").exists() else []
    d, rows = desk_of(s), []
    for m in s["mandates"]:
        cid, seat = m["client_id"], seat_of(m["client_id"])
        v = client_view(s, d, cid)
        ps = [p for p in preds if p["client_id"] == cid]
        dec = lambda p: (p.get(PRODUCTION) or {}).get("decision")
        tests = [e for e in LEDGER.entries if e.get("action") == "agent.self_test" and e.get("client_id") == cid
                 and e.get("generation") == d.generations.get(seat)]
        b = d.badge(seat) if d.generations.get(seat) else None
        rows.append({"client_id": cid, "client": m["name"], "owner": identity.owner_of(cid), "seat": seat,
                     "generation": d.generations.get(seat, 0), "did": d.agent_did(seat), "active": v["agent"]["active"],
                     "mandate_revoked": m["revoked"], "cap": b and b["max_single_chf"], "actions": b and b["actions"],
                     "earlier_versions": sum(1 for x in d.revoked_agents if d.cards.get(x, {}).get("seat") == seat),
                     "manual_generation": s["manual_generation"],
                     "manual_sha256": lessons.fingerprint(manual_text(s, seat)), "model": llm.model_name(),
                     "self_test": tests[-1]["passed"] if tests else None,
                     "test": {"n": len(ps), "correct": sum(dec(p) == p["label"] for p in ps),
                              "must_not_pay": sum(p["label"] != "pay" for p in ps),
                              "wrong_payments": sum(p["label"] != "pay" and dec(p) == "pay" for p in ps),
                              "missed": sum(p["label"] == "pay" and dec(p) != "pay" for p in ps)},
                     "month": {k: v[k] for k in ("paid", "escalated", "rejected", "queue_open", "gate_rejections")}})
    return rows


@app.get("/api/analytics")
def analytics():
    """Risk analytics for the firm: who caught what (test set), account reuse across clients, amount outliers."""
    preds = read_jsonl(RESULTS / "predictions.jsonl") if (RESULTS / "predictions.jsonl").exists() else []
    production = PRODUCTION
    caught = {}  # reason code -> how the production system handled invoices it must not pay automatically
    for p in preds:
        if p["label"] == "pay":
            continue
        h = p.get(production) or {}
        codes = [x["code"] for x in h.get("findings") or []]
        how = "missed" if h.get("decision") == "pay" else "score" if codes == ["RISK_SCORE"] else "code" if codes else "review"
        caught.setdefault(p["reason_code"], Counter())[how] += 1
    decided = [e for e in LEDGER.entries if e.get("action") == "payment.decide" and e.get("gate") == "OK"
               and not e.get("grant_ref")]
    by_iban = {}
    for e in decided:
        if e.get("iban"):
            x = by_iban.setdefault(e["iban"], {"vendors": {}, "clients": set(), "cases": []})
            x["vendors"].setdefault(badge._fold(e.get("vendor")), e.get("vendor") or "?")  # one name per spelling
            x["clients"].add(e["client_id"])
            x["cases"].append(e.get("case_id"))
    reused = [{"iban": k, "vendors": sorted(v["vendors"].values()), "clients": sorted(v["clients"]), "cases": v["cases"]}
              for k, v in by_iban.items() if len(v["vendors"]) > 1 or len(v["clients"]) > 1]
    amounts = {}
    for e in decided:
        if e.get("amount_chf"):
            amounts.setdefault((e["client_id"], e.get("vendor")), []).append(e)
    outliers = []
    for (cid, vendor), es in amounts.items():
        if len(es) < 3:
            continue
        med = sorted(x["amount_chf"] for x in es)[len(es) // 2]
        for e in es:
            if med and e["amount_chf"] / med >= OUTLIER_RATIO:
                outliers.append({"case_id": e.get("case_id"), "client_id": cid, "vendor": vendor,
                                 "amount_chf": e["amount_chf"], "median_chf": med,
                                 "ratio": round(e["amount_chf"] / med, 1), "decision": e.get("decision")})
    outliers.sort(key=lambda o: -o["ratio"])
    return {"production": production, "n_test": len(preds),
            "caught": {k: {"code": c["code"], "review": c["review"], "score": c["score"], "missed": c["missed"]}
                       for k, c in caught.items()},
            "ibans_seen": len(by_iban), "iban_reuse": reused, "outlier_ratio": OUTLIER_RATIO, "outliers": outliers,
            "risk_model": {k: v for k, v in _read(RESULTS / "risk_model.json", {}).items() if k != "scores"}}


@app.get("/api/datascience")
def datascience():
    """The risk model's full pipeline: data, features, split, search, curves, confusion matrices, errors."""
    return {k: v for k, v in _read(RESULTS / "risk_model.json", {}).items() if k != "scores"}


@app.get("/api/ledger")
def ledger():
    ok, bad = verify(LEDGER.entries)
    return {"verifies": ok, "first_bad": bad, "ledger_did": ledger_did(), "firm_did": identity.did_of(identity.firm_key()),
            "audit": audit_identities(LEDGER.entries), "entries": LEDGER.entries[-300:][::-1]}


@app.get("/files/payments/{name}")
def payment_file(name: str):
    path = (RESULTS / "payments" / name).resolve()
    if path.parent != (RESULTS / "payments").resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/xml", filename=name)


@app.get("/api/payments")
def payments():
    d = RESULTS / "payments"
    out = []
    for p in sorted(d.glob("*.xml")) if d.exists() else []:
        xml = p.read_text(encoding="utf-8")
        out.append({"file": p.name, "validation_errors": pain001.validate(xml), "bytes": len(xml)})
    return out


PAGE = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
