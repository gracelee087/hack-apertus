"""The firm's payments desk at runtime: one agent seat per client, signed requests through the gate, ledger.

Each active client has its own seat (payments-c01, ...). A seat is filled by agent generations
v1, v2, ...; each generation has its own key, DID, entity card and a badge that covers only that
client. Every agent action is a stamped request; only requests that pass the gate take effect, and
rejected ones are recorded too. The kill switch revokes one seat's current generation; replacing it
hires the next generation, which inherits the seat's lessons.
"""
import secrets
from datetime import datetime, timezone

from . import credential, gate, identity

ACTIONS = ("payment.decide", "payment.file")
DEFAULT_CAP = 10000.0


def seat_of(client_id: str) -> str:
    return f"payments-{client_id.lower()}"


class Desk:
    def __init__(self, ledger, mandates: dict, clock=None, state: dict = None):
        self.ledger = ledger
        self.mandates = mandates  # client_id -> mandate (live view, so revocations apply at once)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        s = state or {}
        self.generations = s.get("generations", {})  # seat -> current generation
        self.revoked_agents = set(s.get("revoked_agents", []))
        self.seen_nonces = set(s.get("seen_nonces", []))
        self.grants = s.get("grants", {})
        self.cards, self.badges = s.get("cards", {}), s.get("badges", {})  # agent DID -> document
        self.last_stamp = s.get("last_stamp", {})  # seat -> last request

    def dump(self) -> dict:
        return {"generations": self.generations, "revoked_agents": sorted(self.revoked_agents),
                "seen_nonces": sorted(self.seen_nonces), "grants": self.grants, "cards": self.cards,
                "badges": self.badges, "last_stamp": self.last_stamp}

    # --- who is who ------------------------------------------------------------------------
    def seats(self) -> dict:
        return {seat_of(cid): cid for cid in self.mandates}

    def agent_key(self, seat: str):
        return identity.agent_key(seat, self.generations[seat])

    def agent_did(self, seat: str):
        return identity.did_of(self.agent_key(seat)) if self.generations.get(seat) else None

    def card(self, seat: str):
        return self.cards.get(self.agent_did(seat))

    def badge(self, seat: str):
        return self.badges.get(self.agent_did(seat))

    def active(self, seat: str) -> bool:
        return bool(self.card(seat)) and self.agent_did(seat) not in self.revoked_agents

    # --- opening the books, hiring, settings, kill switch -----------------------------------
    def open_books(self):
        """First entries of a ledger: who signs what, and every client mandate in force, in full."""
        firm = identity.did_of(identity.firm_key())
        self.ledger.append(actor=firm, action="ledger.opened", firm=identity.FIRM_NAME, firm_did=firm,
                           ledger_did=identity.did_of(identity.ledger_key()),
                           note="firm key signs agent cards and badges; ledger key signs every entry")
        for cid, m in self.mandates.items():
            self.ledger.append(actor=m["client_did"], action="mandate.registered", client_id=cid,
                               mandate={k: m[k] for k in credential.SIGNED_FIELDS}, client_signature=m["signature"],
                               signature_ok=credential.verify(m),
                               note=f"{m['name']} delegates payments to the firm"
                                    + (f" (revoked {m['revoked_at']})" if m["revoked"] else ""))

    def hire(self, client_id: str, manual_sha256: str, max_single_chf: float = DEFAULT_CAP, actions=ACTIONS,
             valid_from="2026-01-01", valid_until="2026-12-31"):
        """Issue the next generation for this client's seat: key -> DID, entity card, badge, manual."""
        seat = seat_of(client_id)
        self.generations[seat] = self.generations.get(seat, 0) + 1
        m, owner = self.mandates[client_id], identity.owner_of(client_id)
        duty = (f"Pays supplier invoices for {m['name']} within its signed mandate "
                f"({', '.join(m['allowed_categories'])}; up to CHF {m['single_limit_chf']:,.0f} per invoice and "
                f"CHF {m['monthly_limit_chf']:,.0f} per month) and writes its pain.001 payment file. "
                f"Holds everything else for {owner}.")
        card = identity.entity_card(seat, self.generations[seat], display_name=f"Payments agent for {m['name']}",
                                    duty=duty, owner=owner)
        self.cards[card["subject"]] = card
        self.ledger.append(actor=identity.did_of(identity.firm_key()), action="identity.issued", client_id=client_id,
                           seat=seat, generation=self.generations[seat], agent_did=card["subject"], entity_card=card,
                           issued_by=owner,
                           note=f"entity card for {card['name']}, role assigned and managed by {owner}")
        self._issue_badge(seat, client_id, max_single_chf, actions, valid_from, valid_until)
        self.ledger.append(actor=identity.did_of(identity.firm_key()), action="policy.changed", client_id=client_id,
                           seat=seat, generation=self.generations[seat], agent_did=card["subject"],
                           manual_sha256=manual_sha256, note="manual (incl. the seat's lessons) given to this generation")

    def _issue_badge(self, seat, client_id, max_single_chf, actions, valid_from="2026-01-01", valid_until="2026-12-31"):
        b = identity.agent_badge(seat, self.generations[seat], [client_id], valid_from, valid_until,
                                 actions=tuple(actions), max_single_chf=float(max_single_chf))
        self.badges[b["subject"]] = b
        self.ledger.append(actor=identity.did_of(identity.firm_key()), action="delegation.issued", client_id=client_id,
                           seat=seat, generation=self.generations[seat], agent_did=b["subject"], badge=b,
                           note=f"badge: {', '.join(b['actions']) or 'no actions'} for {client_id}, "
                                f"cap CHF {b['max_single_chf']:,.0f}")

    def update_badge(self, client_id: str, max_single_chf: float, actions):
        """Firm-side settings: re-issue the current generation's badge with a new cap / action list."""
        seat = seat_of(client_id)
        old = self.badge(seat)
        self._issue_badge(seat, client_id, max_single_chf, actions, old["valid_from"], old["valid_until"])

    def kill(self, client_id: str, reason: str = ""):
        seat = seat_of(client_id)
        self.revoked_agents.add(self.agent_did(seat))
        self.ledger.append(actor=identity.owner_of(client_id), action="agent.revoked", client_id=client_id, seat=seat,
                           generation=self.generations[seat], agent_did=self.agent_did(seat), note=reason)

    # --- human approval -------------------------------------------------------------------
    def grant(self, client_id: str, case_id: str, amount_chf: float, note: str = "") -> str:
        ref = f"G-{secrets.token_hex(4)}"
        self.grants[ref] = {"case_id": case_id, "amount_chf": float(amount_chf), "note": note}
        self.ledger.append(actor=identity.owner_of(client_id), action="consent.granted", client_id=client_id,
                           case_id=case_id, grant_ref=ref, amount_chf=amount_chf, note=note)
        return ref

    # --- agent requests -------------------------------------------------------------------
    def request(self, client_id: str, action: str, payload: dict, **ledger_fields) -> dict:
        """This client's agent signs the request; the gate decides; the ledger records either way."""
        seat = seat_of(client_id)
        payload = {**payload, "client_id": client_id}
        if not self.generations.get(seat):
            entry = self.ledger.append(actor="system", action="request.rejected", gate="G3_NO_AGENT_FOR_CLIENT",
                                       client_id=client_id, **ledger_fields)
            return {"ok": False, "gate": "G3_NO_AGENT_FOR_CLIENT", "ledger_seq": entry["seq"]}
        st = identity.stamp(self.agent_key(seat), action, client_id, payload, self.clock())
        return self.submit(st, payload, **ledger_fields)

    def submit(self, st: dict, payload: dict, **ledger_fields) -> dict:
        """Verify an already-stamped request (also used to replay attacks against the gate)."""
        ok, code = gate.check(st, payload, self.cards.get(st.get("agent")), self.badges.get(st.get("agent")),
                              self.mandates.get(st.get("client_id")), self.revoked_agents, self.seen_nonces,
                              self.grants, self.clock())
        self.last_stamp[seat_of(str(st.get("client_id")))] = {"stamp": st, "payload": payload}
        entry = self.ledger.append(actor=st.get("agent"), action=st.get("action") if ok else "request.rejected",
                                   gate=code, client_id=st.get("client_id"), nonce=st.get("nonce"),
                                   stamp_proof=str(st.get("proof", ""))[:16], **ledger_fields)
        return {"ok": ok, "gate": code, "ledger_seq": entry["seq"]}
