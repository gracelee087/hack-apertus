"""Reviewer web app: agent identity and gate, approval queue, live invoices, mandates, lessons, ledger."""
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import credential, decide, identity, lessons, llm, pain001
from .common import DATA, RESULTS, load_json, normalize_iban, sha256
from .datagen import SEAT, TODAY
from .desk import Desk
from .ledger import Ledger, audit_identities, ledger_did, verify

app = FastAPI(title="Treuhand payment agent")
lock = threading.Lock()
LEDGER = Ledger(RESULTS / "ledger.jsonl")
STATE = RESULTS / "state.json"
REVIEWER = identity.RESPONSIBLE_HUMAN


def _read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def state():
    s = _read(STATE, None)
    if s is None:
        info = _read(RESULTS / "metrics.json", {}).get("lessons", {})
        s = {"mandates": load_json(DATA / "mandates.json"), "pending_notes": [], "manual_generation": 1,
             "lessons": [{"seat": SEAT, "rule": r, "from_manual_generation": 1} for r in info.get("rules", [])],
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


def manual_text(s, seat=SEAT):
    """Lessons from this seat's lineage bind; lessons from other seats are reference only."""
    binding = [x["rule"] for x in s["lessons"] if x["seat"] == seat]
    reference = [f"[{x['seat']}] {x['rule']}" for x in s["lessons"] if x["seat"] != seat]
    return lessons.manual_with_lessons(binding, s["manual_generation"], reference)


def names(s):
    sup = load_json(DATA / "vendors.json")
    return [m["name"] for m in s["mandates"]] + [v["name"] for vs in sup.values() for v in vs]


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""


class Invoice(BaseModel):
    client_id: str
    invoice_text: str


class Note(BaseModel):
    note: str = ""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/summary")
def summary():
    s = state()
    return {"metrics": _read(RESULTS / "metrics.json", {}), "manual": manual_text(s),
            "manual_sha256": lessons.fingerprint(manual_text(s)), "llm_mode": llm.mode(), "model": llm.model_name()}


# --- agent identity, kill switch, attacks ------------------------------------------------

@app.get("/api/agent")
def agent():
    s = state()
    d = desk_of(s)
    rejected = [e for e in LEDGER.entries if e.get("action") == "request.rejected"]
    return {"seat": SEAT, "generation": d.generation, "did": d.agent_did, "active": d.active,
            "card": d.card, "badge": d.badge, "card_ok": bool(d.card) and identity.verify_doc(d.card),
            "badge_ok": bool(d.badge) and identity.verify_doc(d.badge),
            "firm_did": identity.did_of(identity.firm_key()), "ledger_did": ledger_did(),
            "revoked_agents": sorted(d.revoked_agents), "grants": len(d.grants),
            "requests_rejected": len(rejected), "last_rejections": rejected[-5:]}


@app.post("/api/agent/kill")
def kill(n: Note):
    with lock:
        s = state()
        d = desk_of(s)
        if not d.active:
            raise HTTPException(409, "agent already revoked")
        d.kill(n.note)
        if n.note.strip():
            s["pending_notes"].append(lessons.mask(n.note, names(s)))
        save(s, d)
        return {"ok": True, "revoked": d.agent_did}


@app.post("/api/agent/replace")
def replace():
    with lock:
        s = state()
        d = desk_of(s)
        if d.active:
            d.kill("replaced")
        d.hire(manual_sha256=lessons.fingerprint(manual_text(s)))
        save(s, d)
        return {"ok": True, "generation": d.generation, "did": d.agent_did}


@app.post("/api/attack/{kind}")
def attack(kind: str):
    """Demo buttons: resend the last request, or forge a request in the agent's name."""
    with lock:
        s = state()
        d = desk_of(s)
        if kind == "replay":
            if not d.last_stamp:
                raise HTTPException(400, "no request to replay yet")
            res = d.submit(d.last_stamp["stamp"], d.last_stamp["payload"], attack="replay")
        elif kind == "forged":
            payload = {"case_id": "FORGED", "decision": "pay", "vendor": "Attacker GmbH",
                       "iban": "CH9300762011623852957", "amount_chf": 9500.0, "executes_payment": True,
                       "client_id": "C01"}
            st = identity.stamp(identity.key("attacker"), "payment.decide", "C01", payload)
            st["agent"] = d.agent_did
            res = d.submit(st, payload, attack="forged_signature")
        else:
            raise HTTPException(404, "unknown attack")
        save(s, d)
        return res


# --- queue ---------------------------------------------------------------------------------

@app.get("/api/queue")
def queue():
    return _read(RESULTS / "queue.json", [])


@app.post("/api/queue/{case_id}")
def review(case_id: str, r: Review):
    if r.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    with lock:
        q, s = queue(), state()
        item = next((i for i in q if i["case_id"] == case_id), None)
        if item is None or item["status"] != "open":
            raise HTTPException(404, "no open item")
        m = mandate(s, item["client_id"])
        d = desk_of(s)
        if r.action == "approve":
            ref = d.grant(case_id, float(item["amount_chf"] or 0), r.note)
            res = d.request("payment.decide", m["client_id"],
                            {"case_id": case_id, "decision": "pay", "vendor": item["vendor"], "iban": item["iban"],
                             "amount_chf": float(item["amount_chf"] or 0), "executes_payment": True, "grant_ref": ref},
                            case_id=case_id, decision="pay", amount_chf=item["amount_chf"], grant_ref=ref)
            if not res["ok"]:
                save(s, d)
                raise HTTPException(409, f"gate refused the payment: {res['gate']}")
            s["approved"].setdefault(m["client_id"], []).append(item)
            _write_approved_file(s, m, d)
        else:
            LEDGER.append(actor=REVIEWER, action="consent.denied", client_id=m["client_id"], case_id=case_id,
                          amount_chf=item.get("amount_chf"), note=r.note)
            if r.note.strip():
                s["pending_notes"].append(lessons.mask(r.note, names(s)))
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
    res = d.request("payment.file", m["client_id"], {"file_sha256": sha256(xml), "transactions": len(pays)},
                    file=f"payments/{name}", transactions=len(pays), validation_errors=pain001.validate(xml))
    if res["ok"]:
        path = RESULTS / "payments" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(xml, encoding="utf-8")


# --- mandates, live processing -------------------------------------------------------------

@app.get("/api/mandates")
def mandates():
    s = state()
    return [dict(m, signature_ok=credential.verify(m)) for m in s["mandates"]]


@app.post("/api/mandates/{cid}/revoke")
def revoke(cid: str):
    with lock:
        s = state()
        m = mandate(s, cid)
        m["revoked"], m["revoked_at"] = True, datetime.now().date().isoformat()
        m["signature"] = credential.sign(m)  # the client re-issues its signed mandate as revoked
        LEDGER.append(actor=f"client:{m['client_did']}", action="mandate.revoked", client_id=cid)
        save(s)
        return {"ok": True}


@app.post("/api/process")
def process(inv: Invoice):
    with lock:
        s = state()
        m = mandate(s, inv.client_id)
        d = desk_of(s)
    sup = load_json(DATA / "vendors.json").get(inv.client_id, [])
    case = {"case_id": f"LIVE-{len(LEDGER.entries) + 1}", "invoice_text": inv.invoice_text, "prior_payments": []}
    h = decide.harness(case, m, sup, manual_text(s), promoted=True)
    f = h["fields"]
    try:
        amount = round(float(f.get("total_chf")), 2)
    except (TypeError, ValueError):
        amount = None
    with lock:
        res = d.request("payment.decide", m["client_id"],
                        {"case_id": case["case_id"], "decision": h["decision"], "vendor": f.get("vendor_name"),
                         "iban": normalize_iban(f.get("vendor_iban")), "amount_chf": amount,
                         "executes_payment": h["decision"] == "pay"},
                        case_id=case["case_id"], decision=h["decision"], vendor=f.get("vendor_name"),
                        amount_chf=amount, reason=h.get("reason", ""), checks=[x["code"] for x in h["findings"]],
                        manual_sha256=lessons.fingerprint(manual_text(s)), model=llm.model_name())
        save(s, d)
    return {**h, "gate": res["gate"], "executed": res["ok"], "agent_did": d.agent_did, "generation": d.generation}


# --- lessons, ledger, files ----------------------------------------------------------------

@app.get("/api/lessons")
def lesson_state():
    s = state()
    return {"generation": s["manual_generation"], "lessons": s["lessons"], "pending_notes": s["pending_notes"],
            "seat": SEAT, "train": _read(RESULTS / "metrics.json", {}).get("lessons", {})}


@app.post("/api/lessons/publish")
def publish():
    with lock:
        s = state()
        if not s["pending_notes"]:
            raise HTTPException(400, "no pending reviewer notes")
        new_rules = lessons.distill(s["pending_notes"])
        s["manual_generation"] += 1
        s["lessons"] = (s["lessons"] + [{"seat": SEAT, "rule": r, "from_manual_generation": s["manual_generation"]}
                                        for r in new_rules])[-10:]
        s["pending_notes"] = []
        save(s)
        sha = lessons.fingerprint(manual_text(s))
        LEDGER.append(actor=REVIEWER, action="manual.published", seat=SEAT, generation=s["manual_generation"],
                      manual_sha256=sha)
        return {"generation": s["manual_generation"], "lessons": s["lessons"], "manual_sha256": sha}


@app.get("/api/ledger")
def ledger():
    ok, bad = verify(LEDGER.entries)
    return {"verifies": ok, "first_bad": bad, "ledger_did": ledger_did(), "audit": audit_identities(LEDGER.entries),
            "entries": LEDGER.entries[-200:]}


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
