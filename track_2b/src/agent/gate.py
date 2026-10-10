"""The gate: five checks every agent request must pass before anything is executed or recorded as done.

G1 proof     - the stamp is signed by the agent DID it names, over exactly this payload
G2 replay    - the nonce was never seen and the stamp is fresh
G3 identity  - the agent holds a firm-signed entity card and is not on the revocation list
G4 badge     - the firm-signed badge covers this agent, action, client, date and amount
               (a payment needs a positive amount, case and IBAN; above the badge cap it needs a human grant
               for this client, case, IBAN and amount, and a grant is used up by the payment it allows)
G5 mandate   - the client-signed mandate delegates to our firm, is in force and not revoked, and the amount is
               within its single-payment limit unless a human granted this payment
"""
import math
from datetime import datetime

from . import credential, identity
from .common import canonical, normalize_iban, sha256


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _grant_ok(grant, payload, client_id) -> bool:
    return bool(grant) and not grant.get("used") and grant.get("client_id") == client_id         and grant.get("case_id") == payload.get("case_id")         and normalize_iban(grant.get("iban")) == normalize_iban(payload.get("iban"))         and _positive(payload.get("amount_chf")) and abs(grant["amount_chf"] - payload["amount_chf"]) < 0.01


def check(stamp: dict, payload: dict, card: dict, badge: dict, mandate: dict, revoked_agents: set,
          seen_nonces: set, grants: dict, now: datetime):
    """Return (ok, code). On success the nonce is consumed."""
    firm = identity.did_of(identity.firm_key())
    agent = stamp.get("agent")

    # G1 proof
    if not identity.verify_doc(stamp, signer_field="agent"):
        return False, "G1_SIGNATURE_INVALID"
    if stamp.get("payload_sha256") != sha256(canonical(payload)):
        return False, "G1_PAYLOAD_TAMPERED"
    # G2 replay
    if stamp.get("nonce") in seen_nonces:
        return False, "G2_REPLAY"
    if not identity.fresh(stamp.get("ts"), now):
        return False, "G2_STALE"
    # G3 identity
    if not (card and identity.verify_doc(card) and card["issuer"] == firm and card["subject"] == agent):
        return False, "G3_NO_VALID_ENTITY_CARD"
    if agent in revoked_agents:
        return False, "G3_AGENT_REVOKED"
    # G4 badge
    if not (badge and identity.verify_doc(badge) and badge["issuer"] == firm and badge["subject"] == agent):
        return False, "G4_NO_VALID_BADGE"
    today = now.date().isoformat()
    if not badge["valid_from"] <= today <= badge["valid_until"]:
        return False, "G4_BADGE_EXPIRED"
    if stamp["action"] not in badge["actions"]:
        return False, "G4_ACTION_NOT_ALLOWED"
    if stamp["client_id"] not in badge["clients"] or payload.get("client_id") != stamp["client_id"]:
        return False, "G4_CLIENT_NOT_COVERED"
    pays, ref = payload.get("executes_payment"), payload.get("grant_ref")
    decision = payload.get("decision")
    if stamp["action"] == "payment.decide" and decision is not None and (pays is True) != (decision == "pay"):
        return False, "G4_DECISION_INCONSISTENT"  # a "pay" must go through the amount checks below, and only a "pay" pays
    if pays and not (_positive(payload.get("amount_chf")) and payload.get("case_id") and payload.get("iban")):
        return False, "G4_PAYMENT_INCOMPLETE"
    if ref is not None and not _grant_ok(grants.get(ref), payload, stamp["client_id"]):
        return False, "G4_GRANT_INVALID"
    if pays and ref is None and payload["amount_chf"] > badge["max_single_chf"]:
        return False, "G4_ABOVE_BADGE_CAP_NO_GRANT"
    # G5 mandate
    if not (mandate and mandate["client_id"] == stamp["client_id"] and credential.verify(mandate)):
        return False, "G5_MANDATE_INVALID"
    if mandate["revoked"]:
        return False, "G5_MANDATE_REVOKED"
    if not mandate["valid_from"] <= today <= mandate["valid_until"]:
        return False, "G5_MANDATE_EXPIRED"
    if pays and ref is None and payload["amount_chf"] > mandate["single_limit_chf"]:
        return False, "G5_ABOVE_MANDATE_LIMIT"

    seen_nonces.add(stamp["nonce"])
    if ref is not None:
        grants[ref]["used"] = True
    return True, "OK"
