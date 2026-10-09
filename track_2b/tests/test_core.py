"""Offline tests for the deterministic parts: badge, IBAN, credential, ledger, payment file, masking."""
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import badge, credential, datagen, identity, lessons, pain001  # noqa: E402
from agent.desk import Desk  # noqa: E402
from agent.common import iban_valid, make_ch_iban  # noqa: E402
from agent.ledger import Ledger, audit_identities, verify  # noqa: E402

MANDATES, VENDORS, CASES = datagen.build()
BY_ID = {m["client_id"]: m for m in MANDATES}


def gold_fields(case):
    g = case["gold"]
    return {"vendor_name": g["vendor"], "vendor_iban": g["iban"], "invoice_no": g["invoice_no"],
            "addressee": g["addressee"], "total_chf": g["total_chf"], "category": g["category"]}


def test_iban():
    good = make_ch_iban("12345678901234567")
    assert iban_valid(good) and iban_valid(" ".join([good[:4], good[4:]]))
    assert not iban_valid(good[:-1] + str((int(good[-1]) + 1) % 10))


def test_badge_matches_labels_for_hard_cases():
    """With correct fields, the badge decides every hard-rule case exactly as labelled."""
    hard = [c for c in CASES if c["reason_code"][:2] in ("R_", "E_")
            and c["reason_code"] not in ("E_PRESSURE", "E_VAT_MISMATCH", "E_WRONG_ADDRESSEE")]
    for c in hard:
        verdict, findings = badge.check(BY_ID[c["client_id"]], VENDORS.get(c["client_id"], []),
                                        c["prior_payments"], gold_fields(c))
        assert verdict == c["label"], (c["case_id"], c["reason_code"], findings)
        assert c["reason_code"] in [f["code"] for f in findings]


def test_badge_passes_clean_cases():
    for c in CASES:
        if c["reason_code"] in ("OK", "E_PRESSURE", "E_VAT_MISMATCH", "E_WRONG_ADDRESSEE"):
            verdict, findings = badge.check(BY_ID[c["client_id"]], VENDORS[c["client_id"]],
                                            c["prior_payments"], gold_fields(c))
            assert verdict == "pass", (c["case_id"], findings)


def test_promoted_checks():
    m = BY_ID["C03"]
    ok = {"net_chf": 100.0, "vat_chf": 8.1, "total_chf": 108.1, "addressee": "Cabinet Favre Sárl, 1003 Lausanne"}
    assert badge.promoted_checks(m, ok) == []
    codes = [f["code"] for f in badge.promoted_checks(m, dict(ok, total_chf=120.0, addressee="Velo Gerber AG"))]
    assert codes == ["E_VAT_MISMATCH", "E_WRONG_ADDRESSEE"]


def test_tampered_credential_is_rejected():
    m = copy.deepcopy(BY_ID["C01"])
    m["single_limit_chf"] = 10**6
    assert not credential.verify(m)
    c = next(c for c in CASES if c["client_id"] == "C01" and c["reason_code"] == "OK")
    verdict, findings = badge.check(m, VENDORS["C01"], c["prior_payments"], gold_fields(c))
    assert verdict == "reject" and "CREDENTIAL_INVALID" in [f["code"] for f in findings]


def test_did_and_signed_documents():
    k = identity.key("t")
    did = identity.did_of(k)
    assert did.startswith("did:key:z6Mk")  # standard prefix for Ed25519 did:key
    assert identity.public_key(did).public_bytes_raw() == k.public_key().public_bytes_raw()
    doc = identity.sign_doc({"type": "X", "n": 1}, k)
    assert identity.verify_doc(doc) and not identity.verify_doc({**doc, "n": 2})


def _desk(tmp_path):
    d = Desk(Ledger(tmp_path / "l.jsonl"), copy.deepcopy(BY_ID),
             clock=lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    d.hire(manual_sha256="x")
    return d


def test_gate_accepts_legit_and_blocks_replay(tmp_path):
    d = _desk(tmp_path)
    payload = {"case_id": "T1", "amount_chf": 100.0, "executes_payment": True, "client_id": "C01"}
    st = identity.stamp(d.agent_key, "payment.decide", "C01", payload, d.clock())
    assert d.submit(st, payload)["gate"] == "OK"
    assert d.submit(st, payload)["gate"] == "G2_REPLAY"
    assert verify(d.ledger.entries) == (True, None)


def test_kill_switch_and_grant(tmp_path):
    d = _desk(tmp_path)
    big = {"case_id": "T2", "amount_chf": 25000.0, "executes_payment": True}
    assert d.request("payment.decide", "C05", big)["gate"] == "G4_ABOVE_BADGE_CAP_NO_GRANT"
    ref = d.grant("T2", 25000.0, "checked with client")
    assert d.request("payment.decide", "C05", {**big, "grant_ref": ref})["gate"] == "OK"
    d.kill("test")
    assert d.request("payment.decide", "C01", {"case_id": "T3", "amount_chf": 1.0})["gate"] == "G3_AGENT_REVOKED"
    d.hire(manual_sha256="x")
    assert d.generation == 2
    assert d.request("payment.decide", "C01", {"case_id": "T3", "amount_chf": 1.0})["gate"] == "OK"


def test_identity_is_recorded_from_issuance(tmp_path):
    d = Desk(Ledger(tmp_path / "l.jsonl"), copy.deepcopy(BY_ID),
             clock=lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    d.open_books()
    d.hire(manual_sha256="x")
    actions = [e["action"] for e in d.ledger.entries]
    assert actions[:1] == ["ledger.opened"] and actions.count("mandate.registered") == 5
    assert actions[-3:] == ["identity.issued", "delegation.issued", "policy.changed"]
    d.request("payment.decide", "C01", {"case_id": "T1", "amount_chf": 10.0})
    audit = audit_identities(d.ledger.entries)
    assert audit["ok"] and audit["agent_actions"] == 1
    d.kill("t")
    d.ledger.entries.append({"seq": 99, "actor": d.agent_did, "gate": "OK", "action": "payment.decide"})
    assert not audit_identities(d.ledger.entries)["ok"]  # an action recorded after revocation is flagged


def test_ledger_detects_tampering(tmp_path):
    led = Ledger(tmp_path / "l.jsonl")
    for i in range(3):
        led.append(actor="a", action="pay", amount_chf=i)
    assert verify(led.entries) == (True, None)
    led.entries[1]["amount_chf"] = 999
    assert verify(led.entries) == (False, 1)


def test_pain001_valid_and_detects_bad_sum():
    m = BY_ID["C01"]
    pays = [{"end_to_end_id": "E1", "creditor": "X AG", "iban": VENDORS["C01"][0]["iban"], "amount_chf": 10.5,
             "reference": "R1"},
            {"end_to_end_id": "E2", "creditor": "Y AG", "iban": VENDORS["C01"][1]["iban"], "amount_chf": 20.25,
             "reference": "R2"}]
    xml = pain001.build(m, pays, "MSG1", datetime(2026, 10, 9), "2026-10-09")
    assert pain001.validate(xml) == []
    assert pain001.validate(xml.replace("<CtrlSum>30.75</CtrlSum>", "<CtrlSum>99.00</CtrlSum>", 1))


def test_mask_removes_identifiers():
    note = "Invoice R-2026-1234 from Mühle Rüti AG for CHF 1'234.50 to CH86 7434 3949 3341 8924 5"
    out = lessons.mask(note, ["Mühle Rüti AG"])
    for secret in ("1234", "Mühle", "1'234", "CH86"):
        assert secret not in out, out
