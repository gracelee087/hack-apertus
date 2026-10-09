"""Reviewer web app: approval queue, live invoice processing, mandates (kill switch), lessons, ledger."""
import json
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import credential, decide, lessons, llm, pain001, prompts
from .common import DATA, RESULTS, load_json
from .datagen import AGENT_ID, TODAY
from .ledger import Ledger, verify

app = FastAPI(title="Treuhand payment agent")
lock = threading.Lock()
LEDGER = Ledger(RESULTS / "ledger.jsonl")
STATE = RESULTS / "state.json"
REVIEWER = "human:reviewer"


def _read(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def state():
    s = _read(STATE, None)
    if s is None:
        info = _read(RESULTS / "metrics.json", {}).get("lessons", {})
        s = {"mandates": load_json(DATA / "mandates.json"), "pending_notes": [], "generation": 1,
             "rules": info.get("rules", []), "approved": {}}
        _write(STATE, s)
    return s


def mandate(s, cid):
    m = next((m for m in s["mandates"] if m["client_id"] == cid), None)
    if m is None:
        raise HTTPException(404, "unknown client")
    return m


def manual_text(s):
    return lessons.manual_with_lessons(s["rules"], s["generation"])


def names(s):
    sup = load_json(DATA / "vendors.json")
    return [m["name"] for m in s["mandates"]] + [v["name"] for vs in sup.values() for v in vs]


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""


class Invoice(BaseModel):
    client_id: str
    invoice_text: str


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/api/summary")
def summary():
    s = state()
    return {"metrics": _read(RESULTS / "metrics.json", {}), "manual": manual_text(s),
            "manual_sha256": lessons.fingerprint(manual_text(s)), "llm_mode": llm.mode(), "model": llm.model_name()}


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
        if r.action == "approve" and m["revoked"]:
            raise HTTPException(409, "mandate revoked: cannot approve")
        item["status"] = "approved" if r.action == "approve" else "rejected"
        item["note"] = r.note
        entry = LEDGER.append(actor=REVIEWER, action=r.action, client_id=item["client_id"], case_id=case_id,
                              amount_chf=item.get("amount_chf"), note=r.note)
        if r.action == "approve":
            s["approved"].setdefault(item["client_id"], []).append(item)
            _write_approved_file(s, m)
        elif r.note.strip():
            s["pending_notes"].append(lessons.mask(r.note, names(s)))
        _write(RESULTS / "queue.json", q)
        _write(STATE, s)
        return {"ok": True, "ledger_seq": entry["seq"]}


def _write_approved_file(s, m):
    pays = [{"end_to_end_id": f"{i['case_id']}-A", "creditor": i["vendor"] or "", "iban": i["iban"],
             "amount_chf": float(i["amount_chf"]), "reference": i["case_id"]} for i in s["approved"][m["client_id"]]]
    xml = pain001.build(m, pays, f"{m['client_id']}-{TODAY:%Y%m%d}-A", datetime.now(), TODAY.isoformat())
    path = RESULTS / "payments" / f"{m['client_id']}_pain001_approved.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(xml, encoding="utf-8")
    LEDGER.append(actor=AGENT_ID, action="payment_file_created", client_id=m["client_id"],
                  file=f"payments/{path.name}", transactions=len(pays), validation_errors=pain001.validate(xml))


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
        m["signature"] = credential.sign(m)  # the client re-issues the credential as revoked
        _write(STATE, s)
        LEDGER.append(actor=f"client:{cid}", action="mandate_revoked", client_id=cid)
        return {"ok": True}


@app.post("/api/process")
def process(inv: Invoice):
    s = state()
    m = mandate(s, inv.client_id)
    sup = load_json(DATA / "vendors.json").get(inv.client_id, [])
    case = {"case_id": f"LIVE-{len(LEDGER.entries) + 1}", "invoice_text": inv.invoice_text, "prior_payments": []}
    h = decide.harness(case, m, sup, manual_text(s), promoted=True)
    LEDGER.append(actor=AGENT_ID, action=h["decision"], client_id=m["client_id"], case_id=case["case_id"],
                  vendor=h["fields"].get("vendor_name"), amount_chf=h["fields"].get("total_chf"),
                  reason=h.get("reason", ""), checks=[f["code"] for f in h["findings"]],
                  manual_sha256=lessons.fingerprint(manual_text(s)), model=llm.model_name())
    return h


@app.get("/api/lessons")
def lesson_state():
    s = state()
    return {"generation": s["generation"], "rules": s["rules"], "pending_notes": s["pending_notes"],
            "train": _read(RESULTS / "metrics.json", {}).get("lessons", {})}


@app.post("/api/lessons/publish")
def publish():
    with lock:
        s = state()
        if not s["pending_notes"]:
            raise HTTPException(400, "no pending reviewer notes")
        new_rules = lessons.distill(s["pending_notes"])
        s["rules"] = (s["rules"] + new_rules)[-8:]
        s["generation"] += 1
        s["pending_notes"] = []
        _write(STATE, s)
        sha = lessons.fingerprint(manual_text(s))
        LEDGER.append(actor=REVIEWER, action="manual_published", generation=s["generation"], manual_sha256=sha)
        return {"generation": s["generation"], "rules": s["rules"], "manual_sha256": sha}


@app.get("/api/ledger")
def ledger():
    ok, bad = verify(LEDGER.entries)
    return {"verifies": ok, "first_bad": bad, "entries": LEDGER.entries[-200:]}


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
