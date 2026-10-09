"""Attacks against the gate, replayed over every invoice the agent paid in the reference run.

Deterministic apart from random nonces; no model calls. Each scenario gets a fresh desk (new agent
hire) so one attack cannot influence another. The ledger of this run is kept separately.
"""
import copy
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import credential, identity
from .common import normalize_iban
from .datagen import TODAY
from .desk import Desk
from .ledger import Ledger

START = datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0, tzinfo=timezone.utc)


def _clock(start=START):
    t = [start]

    def now():
        t[0] += timedelta(seconds=1)
        return t[0]
    return now


def _payload(p, cid=None):
    f = p["fields"]
    return {"case_id": p["case_id"], "decision": "pay", "vendor": f.get("vendor_name"),
            "iban": normalize_iban(f.get("vendor_iban")), "amount_chf": round(float(f["total_chf"]), 2),
            "executes_payment": True, "client_id": cid or p["client_id"]}


def _resign(m, **changes):
    m = {**copy.deepcopy(m), **changes}
    m["signature"] = credential.sign(m)
    return m


def run(preds, mandates, out, production="harness_gen1_promoted"):
    paid = [p for p in preds if p[production]["decision"] == "pay"]
    led = Ledger(out / "security_ledger.jsonl")
    if led.entries:
        (out / "security_ledger.jsonl").unlink()
        led = Ledger(out / "security_ledger.jsonl")
    attacker = identity.key("attacker")
    results = {}

    def desk(mands=None, clock=None):
        d = Desk(led, {k: copy.deepcopy(v) for k, v in (mands or mandates).items()}, clock=clock or _clock())
        d.hire(manual_sha256="security-run")
        return d

    def attempt(name, fn):
        r = results.setdefault(name, {"attempts": 0, "accepted": 0, "codes": Counter()})
        for p in paid:
            ok, code = fn(p)
            r["attempts"] += 1
            r["accepted"] += ok
            r["codes"][code] += 1

    def legit(p):
        d = desk()
        res = d.request("payment.decide", p["client_id"], _payload(p), attack="none")
        return res["ok"], res["gate"]

    def forged(p):
        d = desk()
        st = identity.stamp(attacker, "payment.decide", p["client_id"], _payload(p), d.clock())
        st["agent"] = d.agent_did  # claims to be the agent, signed with another key
        res = d.submit(st, _payload(p), attack="forged_signature")
        return res["ok"], res["gate"]

    def tampered(p):
        d = desk()
        st = identity.stamp(d.agent_key, "payment.decide", p["client_id"], _payload(p), d.clock())
        changed = {**_payload(p), "iban": "CH9300762011623852957", "amount_chf": _payload(p)["amount_chf"] * 10}
        res = d.submit(st, changed, attack="tampered_payload")
        return res["ok"], res["gate"]

    def replay(p):
        d = desk()
        st = identity.stamp(d.agent_key, "payment.decide", p["client_id"], _payload(p), d.clock())
        d.submit(st, _payload(p), attack="replay_original")
        res = d.submit(st, _payload(p), attack="replay")
        return res["ok"], res["gate"]

    def stale(p):
        d = desk()
        st = identity.stamp(d.agent_key, "payment.decide", p["client_id"], _payload(p), d.clock() - timedelta(hours=1))
        res = d.submit(st, _payload(p), attack="stale_stamp")
        return res["ok"], res["gate"]

    def unknown_agent(p):
        d = desk()
        st = identity.stamp(attacker, "payment.decide", p["client_id"], _payload(p), d.clock())
        res = d.submit(st, _payload(p), attack="unknown_agent")
        return res["ok"], res["gate"]

    def revoked_agent(p):
        d = desk()
        old = d.agent_key
        d.kill("security run")
        d.hire(manual_sha256="security-run")  # v2 takes the seat; v1's key still exists somewhere
        st = identity.stamp(old, "payment.decide", p["client_id"], _payload(p), d.clock())
        res = d.submit(st, _payload(p), attack="revoked_agent")
        return res["ok"], res["gate"]

    def other_client(p):
        d = desk()
        res = d.request("payment.decide", "C04", _payload(p, "C04"), attack="client_not_delegated")
        return res["ok"], res["gate"]

    def above_cap(p):
        d = desk()
        res = d.request("payment.decide", p["client_id"], {**_payload(p), "amount_chf": 50000.0},
                        attack="above_badge_cap_without_grant")
        return res["ok"], res["gate"]

    def expired_badge(p):
        d = desk(clock=_clock(datetime(2027, 1, 15, 10, 0, tzinfo=timezone.utc)))
        res = d.request("payment.decide", p["client_id"], _payload(p), attack="expired_badge")
        return res["ok"], res["gate"]

    def tampered_mandate(p):
        mands = copy.deepcopy(mandates)
        mands[p["client_id"]]["single_limit_chf"] = 10**7  # raised without the client's signature
        d = desk(mands)
        res = d.request("payment.decide", p["client_id"], _payload(p), attack="tampered_mandate")
        return res["ok"], res["gate"]

    def foreign_delegate(p):
        mands = copy.deepcopy(mandates)
        mands[p["client_id"]] = _resign(mands[p["client_id"]], delegate=identity.did_of(attacker))
        d = desk(mands)
        res = d.request("payment.decide", p["client_id"], _payload(p), attack="mandate_for_other_delegate")
        return res["ok"], res["gate"]

    def revoked_mandate(p):
        d = desk()
        d.mandates[p["client_id"]] = _resign(d.mandates[p["client_id"]], revoked=True, revoked_at=TODAY.isoformat())
        res = d.request("payment.decide", p["client_id"], _payload(p), attack="mandate_revoked_after_hire")
        return res["ok"], res["gate"]

    attempt("legitimate request (control)", legit)
    for name, fn in [("forged signature", forged), ("tampered amount / IBAN after signing", tampered),
                     ("replayed request", replay), ("stale request (1 h old)", stale),
                     ("unknown agent key", unknown_agent), ("revoked agent (v1 after kill switch)", revoked_agent),
                     ("client not delegated to this agent", other_client),
                     ("amount above badge cap, no human grant", above_cap), ("expired badge", expired_badge),
                     ("mandate limits tampered", tampered_mandate), ("mandate delegated to someone else", foreign_delegate),
                     ("mandate revoked by client", revoked_mandate)]:
        attempt(name, fn)
    return {k: {"attempts": v["attempts"], "accepted": v["accepted"], "blocked": v["attempts"] - v["accepted"],
                "codes": dict(v["codes"])} for k, v in results.items()}


def table_rows(attacks: dict) -> str:
    rows = []
    for name, r in attacks.items():
        codes = ", ".join(sorted(r["codes"]))
        if name.startswith("legitimate"):
            rows.append(f"| {name} | {r['attempts']} | {r['blocked']} (accepted {r['accepted']}) | {codes} |")
        else:
            rows.append(f"| {name} | {r['attempts']} | {r['blocked']} | {codes} |")
    return "\n".join(rows)
