"""The payments desk at runtime: agent seat and generations, signed requests through the gate, ledger.

Every agent action (record a decision, create a payment file) is a stamped request. Only requests
that pass the gate take effect; rejected requests are recorded too. The kill switch puts the
current agent generation on the revocation list; replacing it hires the next generation with a new
key, DID, entity card and badge, and the seat's lessons carry over.
"""
import secrets
from datetime import datetime, timezone

from . import gate, identity
from .common import canonical, sha256
from .datagen import SEAT


class Desk:
    def __init__(self, ledger, mandates: dict, clock=None, state: dict = None):
        self.ledger = ledger
        self.mandates = mandates  # client_id -> mandate (live view, so revocations apply at once)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        s = state or {}
        self.generation = s.get("generation", 0)
        self.revoked_agents = set(s.get("revoked_agents", []))
        self.seen_nonces = set(s.get("seen_nonces", []))
        self.grants = s.get("grants", {})
        self.cards, self.badges = s.get("cards", {}), s.get("badges", {})  # agent DID -> document
        self.last_stamp = s.get("last_stamp")

    def dump(self) -> dict:
        return {"generation": self.generation, "revoked_agents": sorted(self.revoked_agents),
                "seen_nonces": sorted(self.seen_nonces), "grants": self.grants, "cards": self.cards,
                "badges": self.badges, "last_stamp": self.last_stamp}

    # --- hiring, kill switch, replacement -------------------------------------------------
    def hire(self, manual_sha256: str, valid_from="2026-01-01", valid_until="2026-12-31"):
        self.generation += 1
        clients = [cid for cid, m in self.mandates.items() if not m["revoked"]]
        card = identity.entity_card(SEAT, self.generation)
        self.cards[card["subject"]] = card
        self.badges[card["subject"]] = identity.agent_badge(SEAT, self.generation, clients, valid_from, valid_until)
        self.ledger.append(actor=f"firm:{identity.FIRM_NAME}", action="agent.hired", seat=SEAT,
                           generation=self.generation, agent_did=self.agent_did,
                           card_sha256=sha256(canonical(self.card)), badge_sha256=sha256(canonical(self.badge)),
                           clients=clients, max_single_chf=self.badge["max_single_chf"],
                           manual_sha256=manual_sha256, responsible_human=identity.RESPONSIBLE_HUMAN)

    def kill(self, reason: str = ""):
        self.revoked_agents.add(self.agent_did)
        self.ledger.append(actor=identity.RESPONSIBLE_HUMAN, action="agent.revoked", seat=SEAT,
                           generation=self.generation, agent_did=self.agent_did, note=reason)

    @property
    def agent_did(self) -> str:
        return identity.did_of(self.agent_key) if self.generation else None

    @property
    def card(self):
        return self.cards.get(self.agent_did)

    @property
    def badge(self):
        return self.badges.get(self.agent_did)

    @property
    def agent_key(self):
        return identity.agent_key(SEAT, self.generation)

    @property
    def active(self) -> bool:
        return bool(self.card) and self.agent_did not in self.revoked_agents

    # --- human approval -------------------------------------------------------------------
    def grant(self, case_id: str, amount_chf: float, note: str = "") -> str:
        ref = f"G-{secrets.token_hex(4)}"
        self.grants[ref] = {"case_id": case_id, "amount_chf": float(amount_chf), "note": note}
        self.ledger.append(actor=identity.RESPONSIBLE_HUMAN, action="consent.granted", case_id=case_id,
                           grant_ref=ref, amount_chf=amount_chf, note=note)
        return ref

    # --- agent requests -------------------------------------------------------------------
    def request(self, action: str, client_id: str, payload: dict, **ledger_fields) -> dict:
        """The current agent signs the request; the gate decides; the ledger records either way."""
        payload = {**payload, "client_id": client_id}
        now = self.clock()
        st = identity.stamp(self.agent_key, action, client_id, payload, now)
        return self.submit(st, payload, **ledger_fields)

    def submit(self, st: dict, payload: dict, **ledger_fields) -> dict:
        """Verify an already-stamped request (also used to replay attacks against the gate)."""
        ok, code = gate.check(st, payload, self.cards.get(st.get("agent")), self.badges.get(st.get("agent")),
                              self.mandates.get(st.get("client_id")), self.revoked_agents, self.seen_nonces,
                              self.grants, self.clock())
        self.last_stamp = {"stamp": st, "payload": payload}
        entry = self.ledger.append(actor=st.get("agent"), action=st.get("action") if ok else "request.rejected",
                                   gate=code, client_id=st.get("client_id"), nonce=st.get("nonce"),
                                   stamp_proof=str(st.get("proof", ""))[:16], **ledger_fields)
        return {"ok": ok, "gate": code, "ledger_seq": entry["seq"]}
