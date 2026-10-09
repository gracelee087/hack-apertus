"""Client mandate: the client company delegates payments to the fiduciary firm, signed with the client's key.

Stand-in for a verifiable credential (e.g. a swiyu SD-JWT VC). The signed terms are the scope
(categories), the limits, the validity window, the delegate (the firm's DID) and the revocation flag:
tampering with any of them, or a mandate that delegates to someone else, fails verification.
"""
from . import identity

SIGNED_FIELDS = (
    "client_id", "client_did", "delegate", "allowed_categories", "single_limit_chf",
    "monthly_limit_chf", "valid_from", "valid_until", "revoked",
)


def _terms(mandate: dict) -> dict:
    return {k: mandate[k] for k in SIGNED_FIELDS}


def sign(mandate: dict) -> str:
    """Return the client's signature over the mandate terms (demo: client key derived from its id)."""
    priv = identity.client_key(mandate["client_id"])
    return identity.sign_doc(_terms(mandate), priv, signer_field="client_did")["proof"]


def registered_did(client_id: str) -> str:
    """The client DID the firm recorded at onboarding (demo: derived from the client id)."""
    return identity.did_of(identity.client_key(client_id))


def verify(mandate: dict) -> bool:
    """Signed by the onboarded client it names, and delegating to our firm."""
    try:
        doc = {**_terms(mandate), "proof": mandate["signature"]}
    except KeyError:
        return False
    return (mandate["client_did"] == registered_did(mandate["client_id"])
            and mandate["delegate"] == identity.did_of(identity.firm_key())
            and identity.verify_doc(doc, signer_field="client_did"))
