"""The gate: five checks every agent request must pass before anything is executed or recorded as done.

G1 proof     - the stamp is signed by the agent DID it names, over exactly this payload
G2 replay    - the nonce was never seen and the stamp is fresh
G3 identity  - the agent holds a firm-signed entity card and is not on the revocation list
G4 badge     - the firm-signed badge covers this agent, action, client, date and amount
               (an amount above the badge cap needs a human grant for this exact case)
G5 mandate   - the client-signed mandate delegates to our firm, is in force and not revoked
"""
from datetime import datetime

from . import credential, identity
from .common import canonical, sha256


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
    amount = payload.get("amount_chf") or 0
    if payload.get("executes_payment") and amount > badge["max_single_chf"]:
        grant = grants.get(payload.get("grant_ref"))
        if not (grant and grant["case_id"] == payload.get("case_id") and abs(grant["amount_chf"] - amount) < 0.01):
            return False, "G4_ABOVE_BADGE_CAP_NO_GRANT"
    # G5 mandate
    if not (mandate and mandate["client_id"] == stamp["client_id"] and credential.verify(mandate)):
        return False, "G5_MANDATE_INVALID"
    if mandate["revoked"]:
        return False, "G5_MANDATE_REVOKED"
    if not mandate["valid_from"] <= today <= mandate["valid_until"]:
        return False, "G5_MANDATE_EXPIRED"

    seen_nonces.add(stamp["nonce"])
    return True, "OK"
