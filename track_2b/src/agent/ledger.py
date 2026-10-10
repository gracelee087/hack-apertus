"""Append-only audit ledger: hash-chained and signed with the ledger key.

Who acted (agent DID or human), for which client, under which manual, with which gate result.
"""
import base64
import json
import threading
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature

from . import identity
from .common import canonical, sha256

GENESIS = "0" * 64


class Ledger:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.key = identity.ledger_key()
        self.entries = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                self.entries = [json.loads(line) for line in f if line.strip()]

    def append(self, **fields) -> dict:
        with self.lock:
            prev = self.entries[-1]["hash"] if self.entries else GENESIS
            entry = {"seq": len(self.entries) + 1, "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     **fields, "prev": prev}
            entry["hash"] = sha256(canonical(entry))
            entry["sig"] = base64.urlsafe_b64encode(self.key.sign(entry["hash"].encode())).decode()
            self.entries.append(entry)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry


def ledger_did() -> str:
    return identity.did_of(identity.ledger_key())


def audit_identities(entries: list) -> dict:
    """An auditor's check from the ledger alone: every agent action was taken by an agent whose entity
    card the firm issued (signature verified) earlier in the ledger and which was not revoked at that time."""
    firm = next((e["firm_did"] for e in entries if e.get("action") == "ledger.opened"), None)
    issued, revoked, problems, actions = {}, set(), [], 0
    for e in entries:
        a, actor = e.get("action"), e.get("actor", "")
        if a == "identity.issued":
            card = e.get("entity_card") or {}
            if identity.verify_doc(card) and card.get("issuer") == firm and card.get("subject") == e.get("agent_did"):
                issued[card["subject"]] = card["name"]
            else:
                problems.append((e["seq"], "card not issued by the firm"))
        elif a == "agent.revoked":
            revoked.add(e.get("agent_did"))
        elif "gate" in e and e.get("gate") == "OK":
            actions += 1
            st, pl = (e.get("request") or {}).get("stamp") or {}, (e.get("request") or {}).get("payload")
            if actor not in issued:
                problems.append((e["seq"], "action by an agent never issued"))
            elif actor in revoked:
                problems.append((e["seq"], "action by a revoked agent"))
            elif not (isinstance(pl, dict) and identity.verify_doc(st, signer_field="agent")
                      and st.get("agent") == actor and st.get("action") == a and st.get("client_id") == e.get("client_id")
                      and pl.get("client_id") == e.get("client_id") and st.get("payload_sha256") == sha256(canonical(pl))):
                problems.append((e["seq"], "action not backed by the agent's signed request"))
            elif any(e.get(k) != v for k, v in pl.items()):
                problems.append((e["seq"], "ledger entry differs from the signed request"))
    return {"firm_did": firm, "agents_issued": issued, "agents_revoked": sorted(revoked),
            "agent_actions": actions, "problems": problems, "ok": bool(firm) and not problems}


def verify(entries: list):
    """Return (ok, index of first bad entry or None): chain, hashes and ledger signatures."""
    pub = identity.public_key(ledger_did())
    prev = GENESIS
    for i, e in enumerate(entries):
        body = {k: v for k, v in e.items() if k not in ("hash", "sig")}
        if e.get("prev") != prev or sha256(canonical(body)) != e.get("hash"):
            return False, i
        try:
            pub.verify(base64.urlsafe_b64decode(e.get("sig", "")), e["hash"].encode())
        except (InvalidSignature, ValueError, TypeError):
            return False, i
        prev = e["hash"]
    return True, None
