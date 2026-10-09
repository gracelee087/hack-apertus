"""Reviewer web app for the fiduciary firm: client portfolio dashboard, per-client pages, evaluation.

Client status and risk signals are computed from the signed ledger, so the dashboard shows exactly
what the agents did and what the gate let through.
"""
import json
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import credential, decide, identity, lessons, llm, pain001
from .common import DATA, RESULTS, load_json, normalize_iban, sha256
from .datagen import TODAY
from .desk import ACTIONS, Desk, seat_of
from .ledger import Ledger, audit_identities, ledger_did, verify

app = FastAPI(title="Treuhand payment agents")
lock = threading.Lock()
LEDGER = Ledger(RESULTS / "ledger.jsonl")
STATE = RESULTS / "state.json"
REVIEWER = identity.RESPONSIBLE_HUMAN
FIRM_WIDE = "*"

RISKS = {  # finding code -> (label, kind)
    "E_IBAN_CHANGED": ("Bank details changed", "fraud"),
    "R_IBAN_INVALID": ("Invalid IBAN", "fraud"),
    "R_DUPLICATE": ("Duplicate invoice", "fraud"),
    "E_VAT_MISMATCH": ("Amounts do not add up", "fraud"),
    "E_WRONG_ADDRESSEE": ("Addressed to another company", "fraud"),
    "MODEL_FLAGGED": ("Flagged by Apertus review (e.g. pressure, secrecy)", "fraud"),
    "E_NEW_VENDOR": ("New supplier", "control"),
    "E_LIMIT_SINGLE": ("Above single-payment limit", "control"),
    "E_LIMIT_MONTH": ("Monthly limit exceeded", "control"),
    "R_SCOPE": ("Outside the mandate's scope", "control"),
    "R_REVOKED": ("Mandate revoked", "control"),
    "CREDENTIAL_INVALID": ("Mandate signature invalid", "control"),
    "EXTRACTION_INCOMPLETE": ("Could not read the invoice", "control"),
    "CATEGORY_UNCLEAR": ("Expense type unclear", "control"),
}


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
    decided = [e for e in entries if e.get("action") == "payment.decide" and e.get("gate") == "OK"]
    auto = [e for e in decided if not e.get("grant_ref")]
    paid = [e for e in decided if e.get("decision") == "pay"]
    risks = Counter(c for e in auto for c in risk_codes(e))
    q = [i for i in queue_items() if i["client_id"] == cid]
    seat = seat_of(cid)
    return {
        "client_id": cid, "name": m["name"], "city": m["city"], "lang": m["lang"],
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
    alerts = []  # open approvals, fraud signals first, then by amount
    for i in queue_items():
        if i["status"] != "open":
            continue
        codes = [c for c in i.get("checks") or [] if c in RISKS] or ["MODEL_FLAGGED"]
        fraud = any(RISKS[c][1] == "fraud" for c in codes)
        alerts.append({"case_id": i["case_id"], "client_id": i["client_id"], "client": names_by_id.get(i["client_id"]),
                       "vendor": i["vendor"], "amount_chf": i["amount_chf"], "fraud": fraud,
                       "risks": [{"code": c, "label": RISKS[c][0], "kind": RISKS[c][1]} for c in codes]})
    alerts.sort(key=lambda a: (not a["fraud"], -(a["amount_chf"] or 0)))
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
    decisions = [e for e in entries if e.get("action") in ("payment.decide", "request.rejected") and e.get("case_id")]
    risky = [dict(e, risk=[{"code": c, "label": RISKS[c][0], "kind": RISKS[c][1]} for c in risk_codes(e)])
             for e in decisions if e.get("action") == "payment.decide" and risk_codes(e)]
    return {**view,
            "agent": {**view["agent"], "card": d.card(seat), "badge": d.badge(seat),
                      "card_ok": bool(d.card(seat)) and identity.verify_doc(d.card(seat)),
                      "badge_ok": bool(d.badge(seat)) and identity.verify_doc(d.badge(seat)),
                      "revoked": [x for x in d.revoked_agents if d.cards.get(x, {}).get("seat") == seat],
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


@app.post("/api/clients/{cid}/agent/replace")
def replace(cid: str):
    with lock:
        s = state()
        m = mandate(s, cid)
        if m["revoked"]:
            raise HTTPException(409, "the client revoked its mandate; no agent can be hired for it")
        d = desk_of(s)
        seat = seat_of(cid)
        cap, actions = _current_settings(d, seat)
        if d.generations.get(seat) and d.active(seat):
            d.kill(cid, "replaced")
        d.hire(cid, manual_sha256=lessons.fingerprint(manual_text(s, seat)), max_single_chf=cap, actions=actions)
        save(s, d)
        return {"ok": True, "generation": d.generations[seat]}


def _current_settings(d, seat):
    b = d.badge(seat) if d.generations.get(seat) else None
    return (b["max_single_chf"], b["actions"]) if b else (10000.0, ACTIONS)


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


@app.post("/api/clients/{cid}/process")
def process(cid: str, inv: Text):
    with lock:
        s = state()
        m = mandate(s, cid)
        manual = manual_text(s, seat_of(cid))
    sup = load_json(DATA / "vendors.json").get(cid, [])
    case = {"case_id": f"LIVE-{len(LEDGER.entries) + 1}", "invoice_text": inv.invoice_text, "prior_payments": []}
    h = decide.harness(case, m, sup, manual, promoted=True)
    f = h["fields"]
    try:
        amount = round(float(f.get("total_chf")), 2)
    except (TypeError, ValueError):
        amount = None
    with lock:
        s = state()
        d = desk_of(s)
        res = d.request(cid, "payment.decide",
                        {"case_id": case["case_id"], "decision": h["decision"], "vendor": f.get("vendor_name"),
                         "iban": normalize_iban(f.get("vendor_iban")), "amount_chf": amount,
                         "executes_payment": h["decision"] == "pay"},
                        case_id=case["case_id"], decision=h["decision"], vendor=f.get("vendor_name"),
                        amount_chf=amount, reason=h.get("reason", ""), checks=[x["code"] for x in h["findings"]],
                        manual_sha256=lessons.fingerprint(manual), model=llm.model_name())
        save(s, d)
    seat = seat_of(cid)
    return {**h, "gate": res["gate"], "executed": res["ok"], "agent_did": d.agent_did(seat),
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
        LEDGER.append(actor=REVIEWER, action="manual.published", client_id=cid, seat=seat,
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
            ref = d.grant(cid, case_id, float(item["amount_chf"] or 0), r.note)
            res = d.request(cid, "payment.decide",
                            {"case_id": case_id, "decision": "pay", "vendor": item["vendor"], "iban": item["iban"],
                             "amount_chf": float(item["amount_chf"] or 0), "executes_payment": True, "grant_ref": ref},
                            case_id=case_id, decision="pay", vendor=item["vendor"], amount_chf=item["amount_chf"],
                            grant_ref=ref, reason=f"approved by human: {r.note}")
            if not res["ok"]:
                save(s, d)
                raise HTTPException(409, f"gate refused the payment: {res['gate']}")
            s["approved"].setdefault(cid, []).append(item)
            _write_approved_file(s, m, d)
        else:
            LEDGER.append(actor=REVIEWER, action="consent.denied", client_id=cid, case_id=case_id,
                          amount_chf=item.get("amount_chf"), note=r.note)
            if r.note.strip():
                s["pending_notes"].append({"client_id": cid, "note": lessons.mask(r.note, names(s))})
        item["status"] = "approved" if r.action == "approve" else "rejected"
        item["note"] = r.note
        _write(RESULTS / "queue.json", q)
        save(s, d)
        return {"ok": True}


def _write_approved_file(s, m, d):
    pays = [{"end_to_end_id": f"{i['case_id']}-A", "creditor": i["vendor"] or "", "iban": i["iban"],
             "amount_chf": float(i["amount_chf"]), "reference": i["case_id"]} for i in s["approved"][m["client_id"]]]
    xml = pain001.build(m, pays, f"{m['client_id']}-{TODAY:%Y%m%d}-A", datetime.now(), TODAY.isoformat())
    name = f"{m['client_id']}_pain001_approved.xml"
    res = d.request(m["client_id"], "payment.file", {"file_sha256": sha256(xml), "transactions": len(pays)},
                    file=f"payments/{name}", transactions=len(pays), validation_errors=pain001.validate(xml))
    if res["ok"]:
        path = RESULTS / "payments" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding="utf-8")


# --- evaluation, ledger, files -----------------------------------------------------------------

@app.get("/api/evaluation")
def evaluation():
    s = state()
    return {"metrics": _read(RESULTS / "metrics.json", {}), "manual": manual_text(s, FIRM_WIDE),
            "llm_mode": llm.mode(), "model": llm.model_name()}


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
