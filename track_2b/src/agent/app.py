"""Reviewer web app for the fiduciary firm: client portfolio dashboard, per-client pages, evaluation.

Client status and risk signals are computed from the signed ledger, so the dashboard shows exactly
what the agents did and what the gate let through.
"""
import json
import secrets
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import badge, basis, credential, decide, gate, identity, lessons, llm, pain001, risk
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
    "FILE_INVALID": ("Payment file not issued", "control"),
    "E_NOT_IN_DOCUMENT": ("Read value not in the invoice", "control"),
    "E_POSSIBLE_DUPLICATE": ("Same supplier and amount already paid", "fraud"),
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


_STARTED = datetime.now(timezone.utc)


def demo_now() -> datetime:
    """Business time of the demo: the synthetic world's date (TODAY 09:00 UTC), advancing with real time since start.
    Badge, gate, month totals and mandate revocation all use it; ledger timestamps stay real."""
    return datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0, tzinfo=timezone.utc) + (datetime.now(timezone.utc) - _STARTED)


def desk_of(s) -> Desk:
    return Desk(LEDGER, {m["client_id"]: m for m in s["mandates"]}, clock=demo_now, state=s["desk"])


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


def standing(entries) -> list:
    """Accepted payment.decide entries, minus those a later payment.voided cancelled (each void cancels the latest one)."""
    out, last = [], {}
    for e in entries:
        if e.get("action") == "payment.decide" and e.get("gate") == "OK":
            last[e.get("case_id")] = e
            out.append(e)
        elif e.get("action") == "payment.voided" and e.get("case_id") in last:
            out.remove(last.pop(e.get("case_id")))
    return out


def client_view(s, d, cid, entries=None):
    """Status, money and risk signals for one client, computed from the ledger."""
    m = mandate(s, cid)
    entries = [e for e in (entries if entries is not None else LEDGER.entries) if e.get("client_id") == cid]
    decided = standing(entries)
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
    if not gate._positive(st.max_single_chf) or any(a not in ACTIONS for a in st.actions):
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
        m["revoked"], m["revoked_at"] = True, demo_now().date().isoformat()
        m["signature"] = credential.sign(m)
        LEDGER.append(actor=m["client_did"], action="mandate.revoked", client_id=cid,
                      note=f"{m['name']} revoked its mandate")
        save(s)
        return {"ok": True}


def prior_payments(cid) -> list:
    """This month's payments for one client, read from the ledger: the reference run and everything since.
    The month is the business month (demo_now); reference-run entries carry no business_date and are dated TODAY."""
    month = demo_now().date().isoformat()[:7]
    return [{"vendor": e.get("vendor") or "", "invoice_no": str(e.get("invoice_no") or ""),
             "amount_chf": e.get("amount_chf") or 0}
            for e in standing(x for x in LEDGER.entries if x.get("client_id") == cid)
            if e.get("decision") == "pay" and str(e.get("business_date") or TODAY.isoformat())[:7] == month]


@app.post("/api/clients/{cid}/process")
def process(cid: str, inv: Text):
    return process_text(cid, inv.invoice_text)


def process_text(cid: str, invoice_text: str):
    with lock:
        s = state()
        m = mandate(s, cid)
        manual = manual_text(s, seat_of(cid))
        case = {"case_id": f"LIVE-{secrets.token_hex(3).upper()}", "client_id": cid, "invoice_text": invoice_text,
                "prior_payments": prior_payments(cid)}
    sup = load_json(DATA / "vendors.json").get(cid, [])
    h = decide.harness(case, m, sup, manual, promoted=True, grounded=True)  # model calls run outside the lock
    f = h["fields"]
    amount = badge._num(f.get("total_chf"))
    score, factors = risk.scorer().score(case, f)
    with lock:
        s = state()
        m = mandate(s, cid)
        d = desk_of(s)
        if h["decision"] == "pay":  # other payments may have been made while Apertus was reading: check again
            prior = prior_payments(cid)
            verdict, findings = badge.check(m, sup, prior, f, today=demo_now().date())
            if verdict == "pass" and badge.possible_duplicate(prior, f.get("vendor_name"), f.get("total_chf")):
                verdict, findings = "escalate", [{"severity": "escalate", "code": "E_POSSIBLE_DUPLICATE",
                                                  "msg": "same supplier and amount already paid this month"}]
            if verdict != "pass":
                h = {**h, "decision": verdict, "findings": findings, "reason": "; ".join(x["msg"] for x in findings)}
        h = decide.second_look(h, score, factors)
        item = {"case_id": case["case_id"], "client_id": cid, "client": m["name"], "vendor": f.get("vendor_name"),
                "amount_chf": None if amount is None else round(amount, 2), "iban": normalize_iban(f.get("vendor_iban")),
                "invoice_no": f.get("invoice_no"), "due_date": f.get("due_date"),
                "checks": [x["code"] for x in h["findings"]], "reason": h.get("reason", ""),
                "risk_score": score, "risk_factors": factors, "invoice_text": invoice_text, "status": "open",
                "basis": basis.of_decision(h, m, manual)}
        log = dict(reason=h.get("reason", ""), checks=item["checks"], manual_sha256=lessons.fingerprint(manual),
                   model=llm.model_name(), risk_score=score)
        if h["decision"] == "pay":
            res = _pay(s, d, m, item, "live", {"basis": item["basis"]}, **log)
            queued = not res["ok"]  # whatever the agent could not carry out waits for a human
            if queued:
                item["reason"] = f"not executed ({res['gate']}); " + item["reason"]
        else:
            res = d.request(cid, "payment.decide",
                            {"case_id": item["case_id"], "decision": h["decision"], "vendor": item["vendor"],
                             "iban": item["iban"], "amount_chf": item["amount_chf"], "executes_payment": False,
                             "basis": item["basis"]},
                            case_id=item["case_id"], decision=h["decision"], vendor=item["vendor"], iban=item["iban"],
                            amount_chf=item["amount_chf"], invoice_no=item["invoice_no"], **log)
            queued = h["decision"] == "escalate"
        if queued:
            _write(RESULTS / "queue.json", queue_items() + [item])
        save(s, d)
    seat = seat_of(cid)
    return {**h, "case_id": case["case_id"], "risk_factors": factors, "basis": item["basis"], "gate": res["gate"], "executed": res["ok"] and h["decision"] == "pay",
            "recorded": res["ok"], "queued": queued, "file": res.get("file"), "agent_did": d.agent_did(seat),
            "generation": d.generations.get(seat, 0)}


@app.post("/api/clients/{cid}/lessons/publish")
def publish(cid: str):
    with lock:
        s = state()
        mandate(s, cid)
        taken = [n for n in s["pending_notes"] if n["client_id"] == cid]
        if not taken:
            raise HTTPException(400, "no pending reviewer notes for this client")
    rules = lessons.distill([n["note"] for n in taken])  # the model call runs outside the lock: the kill switch never waits for it
    with lock:
        s = state()
        if any(n not in s["pending_notes"] for n in taken):
            raise HTTPException(409, "these notes were published meanwhile")
        seat = seat_of(cid)
        s["manual_generation"] += 1
        kept = s["lessons"] + [{"seat": seat, "rule": r, "from_manual_generation": s["manual_generation"]} for r in rules]
        mine = [i for i, x in enumerate(kept) if x["seat"] == seat]
        drop = set(mine[:-20])  # at most 20 lessons per client; other clients' lessons are never dropped
        s["lessons"] = [x for i, x in enumerate(kept) if i not in drop]
        s["pending_notes"] = [n for n in s["pending_notes"] if n not in taken]
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
            if not r.note.strip():
                raise HTTPException(400, "to approve a held payment, write why in a note")
            if badge.already_paid(prior_payments(cid), item.get("vendor"), item.get("invoice_no")):
                raise HTTPException(409, f"payment not made: invoice {item.get('invoice_no')} from {item.get('vendor')} "
                                         "is already paid this month (R_DUPLICATE)")
            amount = badge._num(item["amount_chf"])
            ref = d.grant(cid, case_id, amount or 0, item["iban"], r.note)
            tags = basis.of_human(identity.owner_of(cid), r.note, "approved")
            res = _pay(s, d, m, {**item, "amount_chf": amount}, "approved", {"grant_ref": ref, "basis": tags, "held_basis": item.get("basis", [])}, grant_ref=ref,
                       reason=f"approved by human: {r.note}")
            if not res["ok"]:  # the item stays open
                save(s, d)
                raise HTTPException(409, f"payment not made: {res['gate']}")
        else:
            LEDGER.append(actor=identity.owner_of(cid), action="consent.denied", client_id=cid, case_id=case_id,
                          amount_chf=item.get("amount_chf"), note=r.note,
                          basis=basis.of_human(identity.owner_of(cid), r.note, "rejected"))
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
    # each payment is its own batch with its own message ID; a file once issued is never rewritten, so a file
    # already sent to the bank is never sent again with later payments added
    n = len(s.setdefault(kind, {}).get(cid, [])) + 1
    while (RESULTS / "payments" / f"{cid}_pain001_{kind}_{n:03d}.xml").exists():  # the saved state can lag behind the files issued
        n += 1
    items = [item]
    amount = item["amount_chf"]
    xml = None
    if gate._positive(amount):  # otherwise the gate refuses the request below
        pays = [{"end_to_end_id": f"{i['case_id']}-{sfx}", "creditor": i["vendor"] or "", "iban": i["iban"],
                 "amount_chf": float(i["amount_chf"]), "reference": i["case_id"]} for i in items]
        xml = pain001.build(m, pays, f"{cid}-{TODAY:%Y%m%d}-{sfx}{n:03d}", datetime.now(), TODAY.isoformat())
        errors = pain001.validate(xml)
        if errors:
            LEDGER.append(actor="system", action="payment.file.blocked", client_id=cid, case_id=item["case_id"],
                          validation_errors=errors)
            return {"ok": False, "gate": "FILE_INVALID"}
    res = d.request(cid, "payment.decide",
                    {"case_id": item["case_id"], "decision": "pay", "vendor": item["vendor"], "iban": item["iban"],
                     "amount_chf": amount, "executes_payment": True, **extra},
                    case_id=item["case_id"], decision="pay", vendor=item["vendor"], iban=item["iban"],
                    amount_chf=amount, invoice_no=item.get("invoice_no"), business_date=d.clock().date().isoformat(), **log)
    if not res["ok"]:
        return res
    name = f"{cid}_pain001_{kind}_{n:03d}.xml"
    path = RESULTS / "payments" / name
    tmp = path.with_name(name + ".tmp")

    def void(why):
        tmp.unlink(missing_ok=True)
        LEDGER.append(actor="system", action="payment.voided", client_id=cid, case_id=item["case_id"],
                      note=f"{why}; the decision does not stand")

    try:  # write first, record second: the ledger never names a file that is not on disk
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(xml, encoding="utf-8")
    except OSError as e:
        void(f"payment file could not be written ({e.__class__.__name__})")
        return {"ok": False, "gate": "FILE_WRITE_FAILED"}
    filed = d.request(cid, "payment.file", {"file_sha256": sha256(xml), "transactions": len(items)},
                      file=f"payments/{name}", transactions=len(items), case_id=item["case_id"])
    if not filed["ok"]:
        void(f"payment file refused by the gate ({filed['gate']})")
        return filed
    try:
        tmp.replace(path)
    except OSError as e:
        void(f"payment file could not be issued ({e.__class__.__name__})")
        return {"ok": False, "gate": "FILE_WRITE_FAILED"}
    s[kind].setdefault(cid, []).append(item)
    return {**res, "file": name}


# --- evaluation, ledger, files -----------------------------------------------------------------

@app.get("/api/evaluation")
def evaluation():
    s = state()
    return {"metrics": _read(RESULTS / "metrics.json", {}), "holdout": _read(RESULTS / "holdout.json", None),
            "confirm": confirm_study(), "agents": agents_by_client(s), "manual": manual_text(s, FIRM_WIDE),
            "llm_mode": llm.mode(), "model": llm.model_name()}


def confirm_study():
    """The study's confirm set (500 unseen invoices, run once): wrong payments by system and where each invoice went."""
    res, path = _read(RESULTS / "study" / "confirm.json", None), RESULTS / "study" / "confirm_predictions.jsonl"
    if not res or not path.exists():
        return None
    outcome = {}
    for p in read_jsonl(path):
        k = ("must_not_pay" if p["label"] != "pay" else "legitimate", p["production"]["decision"])
        outcome[f"{k[0]}:{k[1]}"] = outcome.get(f"{k[0]}:{k[1]}", 0) + 1
    gen1 = _read(RESULTS / "study" / "confirm_baseline_gen1.json", None)
    return {"n": res["n"], "seed": res["seed"], "threshold": res.get("threshold_used"),
            "metrics": {k: {f: v[f] for f in ("accuracy", "wrong_payments", "wrong_payment_chf", "missed_payment_rate",
                                                "wrong_payment_rate_ci95")} for k, v in res["metrics"].items()},
            "baseline_gen1": gen1 and {f: gen1["metrics"]["baseline_gen1"][f] for f in
                                       ("accuracy", "wrong_payments", "wrong_payment_chf", "missed_payment_rate")},
            "outcome": outcome}


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


DATA_FILES = {  # published name -> (path under data/, what it is)
    "mandates.json": ("mandates.json", "Client mandates, signed by each client"),
    "vendors.json": ("vendors.json", "Known suppliers per client"),
    "cases.jsonl": ("cases.jsonl", "120 invoices with labels and reviewer notes (42 train, 78 test)"),
    "holdout.jsonl": ("holdout.jsonl", "42 fresh invoices, run once (seed 2027)"),
    "payments.jsonl": ("history/payments.jsonl", "3,000 past payments that train the risk score"),
    "apertus_responses.jsonl": ("cache/apertus_responses.jsonl", "Every Apertus request and response, for keyless replay"),
}
RULES = [  # layer, code, what happens, where it is enforced
    ("hard check", "CREDENTIAL_INVALID", "reject", "badge.py: the client's mandate signature does not verify"),
    ("hard check", "R_REVOKED", "reject", "badge.py: mandate revoked or not in force"),
    ("hard check", "R_SCOPE", "reject", "badge.py: expense type outside the mandate"),
    ("hard check", "R_IBAN_INVALID", "reject", "badge.py: IBAN fails the checksum"),
    ("hard check", "R_DUPLICATE", "reject", "badge.py: same supplier and invoice number already paid this month"),
    ("hard check", "E_IBAN_CHANGED", "to a human", "badge.py: IBAN differs from the account on file"),
    ("hard check", "E_NEW_VENDOR", "to a human", "badge.py: supplier not in the client's list"),
    ("hard check", "E_LIMIT_SINGLE", "to a human", "badge.py: above the mandate's single-payment limit"),
    ("hard check", "E_LIMIT_MONTH", "to a human", "badge.py: month total would exceed the mandate"),
    ("hard check", "EXTRACTION_INCOMPLETE", "to a human", "badge.py: supplier, IBAN or a positive total could not be read"),
    ("grounding", "E_NOT_IN_DOCUMENT", "to a human", "badge.grounding / badge.cited: the IBAN or total is not written in the invoice, or Apertus cannot quote where it read IBAN, invoice number and total"),
    ("grounding", "E_POSSIBLE_DUPLICATE", "to a human", "badge.possible_duplicate: same supplier and amount already paid this month"),
    ("hard check", "CATEGORY_UNCLEAR", "to a human", "badge.py: expense type unclear"),
    ("promoted lesson", "E_VAT_MISMATCH", "to a human", "badge.promoted_checks: net + VAT != total"),
    ("promoted lesson", "E_WRONG_ADDRESSEE", "to a human", "badge.promoted_checks: invoice addressed to another company"),
    ("content review", "MODEL_FLAGGED", "to a human", "decide.review: Apertus reads against the firm manual and lessons"),
    ("second look", "RISK_SCORE", "to a human", "decide.second_look: risk score >= 95 on an invoice the agent would pay (threshold chosen on the study tune set)"),
    ("gate", "G1", "refused", "gate.py: signature and payload hash"),
    ("gate", "G2", "refused", "gate.py: nonce never seen, request fresh"),
    ("gate", "G3", "refused", "gate.py: firm-issued entity card, agent not revoked"),
    ("gate", "G4", "refused", "gate.py: badge covers action, client, date, amount; human grant bound and used once"),
    ("gate", "G5", "refused", "gate.py: client mandate signed, for this firm, in force, single limit"),
]


def _rows(x) -> int:  # vendors.json is grouped by client
    return sum(len(v) for v in x.values()) if isinstance(x, dict) else len(x)


@app.get("/api/database")
def database():
    """The data the system is built and tested on, with the rules it enforces."""
    s = state()
    files = []
    for name, (rel, what) in DATA_FILES.items():
        path = DATA / rel
        rows = (_rows(load_json(path)) if path.suffix == ".json" else sum(1 for line in open(path, encoding="utf-8") if line.strip())) \
            if path.exists() else 0
        files.append({"name": name, "path": f"data/{rel}", "what": what, "rows": rows,
                      "bytes": path.stat().st_size if path.exists() else 0})
    cases = read_jsonl(DATA / "cases.jsonl")
    held = read_jsonl(DATA / "holdout.jsonl") if (DATA / "holdout.jsonl").exists() else []
    hist = read_jsonl(DATA / "history" / "payments.jsonl") if (DATA / "history" / "payments.jsonl").exists() else []
    cache = read_jsonl(DATA / "cache" / "apertus_responses.jsonl") if (DATA / "cache" / "apertus_responses.jsonl").exists() else []
    lat = sorted(r.get("latency_s") or 0 for r in cache if r.get("latency_s"))
    vendors = load_json(DATA / "vendors.json")
    row = lambda c: {"case_id": c["case_id"], "client_id": c["client_id"], "lang": c["lang"], "split": c["split"],
                     "type": c["reason_code"], "label": c["label"], "total_chf": c["gold"]["total_chf"], "vendor": c["gold"]["vendor"]}
    return {
        "files": files,
        "mandates": [{k: m[k] for k in ("client_id", "name", "city", "lang", "allowed_categories", "single_limit_chf",
                                        "monthly_limit_chf", "valid_from", "valid_until", "revoked")} | {"signature_ok": credential.verify(m)}
                     for m in s["mandates"]],
        "vendors": [{"client_id": cid, "name": v["name"], "category": v["category"], "lang": v["lang"], "iban": v["iban"]}
                    for cid, vs in vendors.items() for v in vs],
        "cases": [row(c) for c in cases] + [row(c) for c in held],
        "history": {"rows": len(hist), "fraud": sum(r["is_fraud"] for r in hist),
                    "must_not_autopay": sum(r["must_not_autopay"] for r in hist),
                    "kinds": dict(Counter(r["kind"] for r in hist)),
                    "patterns": dict(Counter(f"{r['kind']}:{r['pattern'] or 'normal'}" for r in hist).most_common()),
                    "sample": hist[:12]},
        "cache": {"calls": len(cache), "model": llm.model_name(),
                  "by_step": dict(Counter(str(r.get("tag", "")).split(":")[0] or "other" for r in cache)),
                  "latency_median_s": lat[len(lat) // 2] if lat else None},
        "rules": [{"layer": a, "code": b, "effect": c, "where": d} for a, b, c, d in RULES],
        "lessons": [x["rule"] for x in s["lessons"] if x["seat"] == FIRM_WIDE],
        "concept": "Identity, badge, stamp, gate, ledger and lessons follow the author's earlier Agentic ID (know-your-agent) "
                   "design; the code here is new.",
    }


def _flat(x, pre="") -> dict:  # nested fields become dotted columns; lists stay as JSON text
    out = {}
    for k, v in x.items():
        if isinstance(v, dict):
            out |= _flat(v, f"{pre}{k}.")
        else:
            out[pre + k] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v
    return out


@app.get("/api/data/{name}")
def data_rows(name: str, offset: int = 0, limit: int = 200, q: str = "", sort: str = "", desc: bool = False):
    """One data file as a sheet: flattened rows, searched and sorted on the server, a page at a time."""
    if name not in DATA_FILES or not (DATA / DATA_FILES[name][0]).exists():
        raise HTTPException(404)
    path = DATA / DATA_FILES[name][0]
    if path.suffix == ".json":
        x = load_json(path)
        rows = [{"client_id": cid} | v for cid, vs in x.items() for v in vs] if isinstance(x, dict) else x
    else:
        rows = read_jsonl(path)
    rows = [_flat(r) for r in rows]
    cols = list(dict.fromkeys(k for r in rows for k in r))
    if q:
        rows = [r for r in rows if any(q.lower() in str(v).lower() for v in r.values())]
    if sort in cols:
        rows.sort(key=lambda r: (r.get(sort) is None, isinstance(r.get(sort), str), r.get(sort) if r.get(sort) is not None else 0),
                  reverse=desc)
    limit = max(1, min(limit, 500))
    return {"name": name, "columns": cols, "total": len(rows), "offset": offset, "rows": rows[offset:offset + limit]}


@app.get("/files/data/{name}")
def data_file(name: str):
    if name not in DATA_FILES or not (DATA / DATA_FILES[name][0]).exists():
        raise HTTPException(404)
    return FileResponse(DATA / DATA_FILES[name][0], filename=name)


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
