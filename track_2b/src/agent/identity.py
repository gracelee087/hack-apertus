"""Agent identity: Ed25519 keys, did:key identifiers, signed documents and request stamps.

Who holds which key:
  firm    - the fiduciary firm; signs entity cards and agent badges
  client  - each client company; signs the mandate it gives the firm
  agent   - one key pair per seat *generation*; signs every action request (stamp)
  ledger  - signs every ledger entry

Demo keys are derived from fixed seeds so the reference run is reproducible and verifiable.
In production the firm and ledger keys live in an HSM / vault and the agent key only in memory.
"""
import base64
import os
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .common import canonical, sha256

SEED = os.environ.get("DEMO_KEY_SEED", "track2b-demo-only")
FIRM_NAME = "Muster Treuhand AG"
RESPONSIBLE_HUMAN = "Head of payments (human reviewer)"
OWNER_TITLE = "Client manager (Mandatsleiter/in)"
OWNERS = {  # client -> the person at the firm who assigns the agent its role and manages it (fictional)
    "C01": "Anna Keller", "C02": "Giulia Rossi", "C03": "Claire Dubois", "C04": "Martin Huber", "C05": "Daniel Meier",
}


def owner_of(client_id: str) -> str:
    return OWNERS.get(client_id, RESPONSIBLE_HUMAN)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _unb58(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def key(name: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(sha256(f"{SEED}:{name}")))


def did_of(priv: Ed25519PrivateKey) -> str:
    raw = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return "did:key:z" + _b58(b"\xed\x01" + raw)  # multicodec ed25519-pub


def public_key(did: str) -> Ed25519PublicKey:
    if not did.startswith("did:key:z"):
        raise ValueError("unsupported DID")
    raw = _unb58(did[len("did:key:z"):])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise ValueError("not an Ed25519 did:key")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign_doc(doc: dict, priv: Ed25519PrivateKey, signer_field: str = "issuer") -> dict:
    """Return doc with the signer's DID and an Ed25519 signature over everything else."""
    body = {k: v for k, v in doc.items() if k != "proof"}
    body[signer_field] = did_of(priv)
    return {**body, "proof": _b64(priv.sign(canonical(body).encode()))}


def verify_doc(doc: dict, signer_field: str = "issuer") -> bool:
    try:
        body = {k: v for k, v in doc.items() if k != "proof"}
        public_key(body[signer_field]).verify(_unb64(doc["proof"]), canonical(body).encode())
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


# --- principals -------------------------------------------------------------------------

def firm_key():
    return key("firm")


def ledger_key():
    return key("ledger")


def client_key(client_id: str):
    return key(f"client:{client_id}")


def agent_key(seat: str, generation: int):
    return key(f"agent:{seat}:v{generation}")


def entity_card(seat: str, generation: int, display_name: str = "", duty: str = "", owner: str = "") -> dict:
    """The agent's ID card: who it is, what job it has, and which humans answer for it. Signed by the firm."""
    return sign_doc({"type": "EntityCard", "subject": did_of(agent_key(seat, generation)), "seat": seat,
                     "generation": generation, "name": f"{seat}.v{generation}", "display_name": display_name,
                     "duty": duty, "operator": FIRM_NAME,
                     "owner": {"name": owner or RESPONSIBLE_HUMAN, "title": OWNER_TITLE,
                               "role": "assigns the agent its role, approves what it holds back, can switch it off"},
                     "responsible_human": RESPONSIBLE_HUMAN, "is_ai": True}, firm_key())


def agent_badge(seat: str, generation: int, clients: list, valid_from: str, valid_until: str,
                actions=("payment.decide", "payment.file"), max_single_chf: float = 10000) -> dict:
    """What the firm lets this agent generation do, for which clients, up to which amount."""
    return sign_doc({"type": "AgentBadge", "subject": did_of(agent_key(seat, generation)), "seat": seat,
                     "generation": generation, "clients": sorted(clients), "actions": list(actions),
                     "max_single_chf": max_single_chf, "valid_from": valid_from, "valid_until": valid_until},
                    firm_key())


def stamp(priv: Ed25519PrivateKey, action: str, client_id: str, payload: dict, now: datetime = None) -> dict:
    """A signed, single-use request: this agent asks to perform this action on this payload."""
    now = now or datetime.now(timezone.utc)
    return sign_doc({"type": "Stamp", "action": action, "client_id": client_id,
                     "payload_sha256": sha256(canonical(payload)), "nonce": secrets.token_hex(12),
                     "ts": now.isoformat(timespec="seconds")}, priv, signer_field="agent")


def fresh(ts: str, now: datetime, window=timedelta(minutes=5)) -> bool:
    try:
        return abs(now - datetime.fromisoformat(ts)) <= window
    except (TypeError, ValueError):
        return False
