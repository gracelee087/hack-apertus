"""Mock delegation credential for a client mandate.

Stand-in for a verifiable credential (e.g. a swiyu SD-JWT VC) that the client
issues to the agent. Here it is an HMAC over the canonical mandate terms with a
demo key, so tampering with scope or limits is detectable. Not production crypto.
"""
import hashlib
import hmac
import os

from .common import canonical

DEMO_KEY = os.environ.get("MANDATE_SIGNING_KEY", "demo-only-not-a-secret").encode()
SIGNED_FIELDS = (
    "client_id", "agent_id", "allowed_categories", "single_limit_chf",
    "monthly_limit_chf", "valid_from", "valid_until", "revoked",
)


def _payload(mandate: dict) -> str:
    return canonical({k: mandate[k] for k in SIGNED_FIELDS})


def sign(mandate: dict) -> str:
    return hmac.new(DEMO_KEY, _payload(mandate).encode(), hashlib.sha256).hexdigest()


def verify(mandate: dict) -> bool:
    return hmac.compare_digest(mandate.get("signature", ""), sign(mandate))
