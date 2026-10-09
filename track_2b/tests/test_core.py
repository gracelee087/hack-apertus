"""Offline tests for the deterministic parts: badge, IBAN, credential, ledger, payment file, masking."""
import copy
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import badge, credential, datagen, identity, lessons, pain001, risk  # noqa: E402
from agent.desk import Desk, seat_of  # noqa: E402
from agent.common import DATA, iban_valid, load_json, make_ch_iban  # noqa: E402
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


def _desk(tmp_path, open_books=False):
    d = Desk(Ledger(tmp_path / "l.jsonl"), copy.deepcopy(BY_ID),
             clock=lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    if open_books:
        d.open_books()
    for cid in ("C01", "C02", "C03", "C05"):
        d.hire(cid, manual_sha256="x")
    return d


def test_gate_accepts_legit_and_blocks_replay(tmp_path):
    d = _desk(tmp_path)
    payload = {"case_id": "T1", "amount_chf": 100.0, "iban": "CH9300762011623852957", "executes_payment": True,
               "client_id": "C01"}
    st = identity.stamp(d.agent_key(seat_of("C01")), "payment.decide", "C01", payload, d.clock())
    assert d.submit(st, payload)["gate"] == "OK"
    assert d.submit(st, payload)["gate"] == "G2_REPLAY"
    assert verify(d.ledger.entries) == (True, None)


def test_agents_are_isolated_per_client(tmp_path):
    d = _desk(tmp_path)
    payload = {"case_id": "T1", "amount_chf": 100.0, "client_id": "C02"}
    st = identity.stamp(d.agent_key(seat_of("C01")), "payment.decide", "C02", payload, d.clock())
    assert d.submit(st, payload)["gate"] == "G4_CLIENT_NOT_COVERED"
    assert d.request("C04", "payment.decide", {"case_id": "T9"})["gate"] == "G3_NO_AGENT_FOR_CLIENT"


def test_kill_switch_grant_and_settings(tmp_path):
    d = _desk(tmp_path)
    big = {"case_id": "T2", "amount_chf": 25000.0, "iban": "CH9300762011623852957", "executes_payment": True}
    assert d.request("C05", "payment.decide", big)["gate"] == "G4_ABOVE_BADGE_CAP_NO_GRANT"
    ref = d.grant("C05", "T2", 25000.0, big["iban"], "checked with client")
    assert d.request("C05", "payment.decide", {**big, "grant_ref": ref})["gate"] == "OK"
    d.update_badge("C05", 30000, ["payment.decide"])  # firm raises the cap, removes file creation
    # a higher badge cap cannot lift the client's own limit (C05: CHF 10,000)
    assert d.request("C05", "payment.decide", {**big, "case_id": "T4"})["gate"] == "G5_ABOVE_MANDATE_LIMIT"
    assert d.request("C05", "payment.decide", {**big, "case_id": "T4", "amount_chf": 9000.0})["gate"] == "OK"
    assert d.request("C05", "payment.file", {"x": 1})["gate"] == "G4_ACTION_NOT_ALLOWED"
    d.kill("C01", "test")
    assert d.request("C01", "payment.decide", {"case_id": "T3", "amount_chf": 1.0})["gate"] == "G3_AGENT_REVOKED"
    assert d.request("C02", "payment.decide", {"case_id": "T3", "amount_chf": 1.0})["gate"] == "OK"  # others unaffected
    d.hire("C01", manual_sha256="x")
    assert d.generations[seat_of("C01")] == 2
    assert d.request("C01", "payment.decide", {"case_id": "T3", "amount_chf": 1.0})["gate"] == "OK"


def test_one_click_onboarding(tmp_path):
    d = Desk(Ledger(tmp_path / "l.jsonl"), copy.deepcopy(BY_ID),
             clock=lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc))
    d.open_books()
    steps = d.onboard("C02", manual_sha256="x" * 64)
    assert [s["step"] for s in steps] == ["mandate", "entity_card", "badge", "manual", "self_test", "on_duty"]
    assert all(s["ok"] for s in steps) and d.active(seat_of("C02"))
    b = d.badge(seat_of("C02"))  # the badge follows the client's mandate
    assert b["max_single_chf"] == BY_ID["C02"]["single_limit_chf"] and b["valid_until"] == BY_ID["C02"]["valid_until"]
    assert steps[4]["detail"] == {"signed request": "OK", "forged signature": "G1_SIGNATURE_INVALID",
                                  "replayed request": "G2_REPLAY"}
    assert d.request("C02", "payment.decide", {"case_id": "T1", "amount_chf": 10.0})["gate"] == "OK"  # nonce unused
    d.onboard("C02", manual_sha256="x" * 64)  # replacement: v1 is revoked, v2 takes the seat
    assert d.generations[seat_of("C02")] == 2 and len(d.revoked_agents) == 1
    stopped = d.onboard("C04", manual_sha256="x" * 64)  # C04 revoked its mandate
    assert [s["ok"] for s in stopped] == [False] and not d.generations.get(seat_of("C04"))
    assert audit_identities(d.ledger.entries)["ok"]


def test_grant_is_bound_to_client_case_iban_and_used_once(tmp_path):
    d = _desk(tmp_path)
    pay = {"case_id": "T7", "amount_chf": 25000.0, "iban": "CH9300762011623852957", "executes_payment": True}
    other = d.grant("C02", "T7", 25000.0, pay["iban"])  # same case and amount, but another client's grant
    assert d.request("C05", "payment.decide", {**pay, "grant_ref": other})["gate"] == "G4_GRANT_INVALID"
    ref = d.grant("C05", "T7", 25000.0, "CH5604835012345678009")  # approved for another account
    assert d.request("C05", "payment.decide", {**pay, "grant_ref": ref})["gate"] == "G4_GRANT_INVALID"
    ref = d.grant("C05", "T7", 25000.0, pay["iban"])
    assert d.request("C05", "payment.decide", {**pay, "grant_ref": ref})["gate"] == "OK"
    assert d.request("C05", "payment.decide", {**pay, "grant_ref": ref})["gate"] == "G4_GRANT_INVALID"  # used up


def test_payment_needs_a_positive_amount():
    assert badge._num("nan") is None and badge._num("inf") is None and badge._num("12.5") == 12.5
    case = next(c for c in CASES if c["reason_code"] == "OK")
    for total in (0, -50):
        verdict, findings = badge.check(BY_ID[case["client_id"]], VENDORS[case["client_id"]], case["prior_payments"],
                                        {**gold_fields(case), "total_chf": total})
        assert verdict == "escalate" and "EXTRACTION_INCOMPLETE" in [f["code"] for f in findings]


def test_gate_refuses_invalid_amounts(tmp_path):
    d = _desk(tmp_path)
    for amount in (-100.0, 0.0, float("nan"), None, True):
        pay = {"case_id": "T8", "amount_chf": amount, "iban": "CH9300762011623852957", "executes_payment": True}
        assert d.request("C01", "payment.decide", pay)["gate"] == "G4_PAYMENT_INCOMPLETE", amount


def test_live_flow_files_the_payment_and_holds_the_duplicate(tmp_path, monkeypatch):
    from agent import app
    monkeypatch.setattr(app, "RESULTS", tmp_path)
    monkeypatch.setattr(app, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(app, "LEDGER", Ledger(tmp_path / "ledger.jsonl"))
    d = Desk(app.LEDGER, copy.deepcopy(BY_ID))
    d.hire("C01", manual_sha256="x")
    (tmp_path / "desk.json").write_text(__import__("json").dumps(d.dump()), encoding="utf-8")
    v = VENDORS["C01"][0]
    fields = {"vendor_name": v["name"], "vendor_iban": v["iban"], "invoice_no": "R-1", "total_chf": 400.0,
              "addressee": BY_ID["C01"]["name"], "category": v["category"]}
    monkeypatch.setattr(app.decide, "harness", lambda case, *a, **k: {  # Apertus read the invoice and found no issue
        "fields": dict(fields), "badge": "pass", "findings": [], "decision": "pay", "reason": "ok"})

    first = app.process("C01", app.Text(invoice_text="x"))
    assert first["executed"] and first["file"] == "C01_pain001_live.xml"
    assert pain001.validate((tmp_path / "payments" / first["file"]).read_text(encoding="utf-8")) == []
    again = app.process("C01", app.Text(invoice_text="x"))  # the same invoice: this month's payments come from the ledger
    assert again["decision"] == "reject" and not again["executed"] and "R_DUPLICATE" in [f["code"] for f in again["findings"]]

    fields["invoice_no"] = "R-2"
    s = app.state()
    d = app.desk_of(s)
    d.update_badge("C01", 10000, ["payment.decide"])  # no payment.file: the decision cannot stand without its file
    app.save(s, d)
    held = app.process("C01", app.Text(invoice_text="x"))
    assert not held["executed"] and held["queued"] and held["gate"] == "G4_ACTION_NOT_ALLOWED"
    assert len(app.prior_payments("C01")) == 1  # the voided decision does not count as paid

    s = app.state()
    d = app.desk_of(s)
    d.update_badge("C01", 10000, ["payment.decide", "payment.file"])
    app.save(s, d)
    assert app.review(held["case_id"], app.Review(action="approve", note="checked"))["ok"]
    assert (tmp_path / "payments" / "C01_pain001_approved.xml").exists()
    assert [i["status"] for i in app.queue_items()] == ["approved"]
    assert len(app.prior_payments("C01")) == 2  # approved after a refused file: the second decision counts as paid


def test_second_look_holds_a_high_risk_payment():
    from agent import decide
    paid = {"decision": "pay", "findings": [], "reason": "ok"}
    held = decide.second_look(paid, 80.0, [("urgency_terms", 1.2)])
    assert held["decision"] == "escalate" and held["findings"][-1]["code"] == "RISK_SCORE" and held["risk_score"] == 80.0
    assert decide.second_look(paid, 20.0)["decision"] == "pay"  # low score: paid as before
    rejected = {"decision": "reject", "findings": [{"code": "R_SCOPE"}], "reason": "scope"}
    assert decide.second_look(rejected, 99.0)["decision"] == "reject"  # never changes a hold or a rejection
    pressure = next(c for c in CASES if c["reason_code"] == "E_PRESSURE")
    ok = next(c for c in CASES if c["reason_code"] == "OK")
    s = risk.scorer()
    assert s.score(pressure, gold_fields(pressure))[0] > s.score(ok, gold_fields(ok))[0]


def test_identity_is_recorded_from_issuance(tmp_path):
    d = _desk(tmp_path, open_books=True)
    actions = [e["action"] for e in d.ledger.entries]
    assert actions[:1] == ["ledger.opened"] and actions.count("mandate.registered") == 5
    assert actions.count("identity.issued") == 4 and actions[-3:] == ["identity.issued", "delegation.issued", "policy.changed"]
    d.request("C01", "payment.decide", {"case_id": "T1", "amount_chf": 10.0})
    audit = audit_identities(d.ledger.entries)
    assert audit["ok"] and audit["agent_actions"] == 1 and len(audit["agents_issued"]) == 4
    card = d.card(seat_of("C01"))  # the card names the job and the human who manages the agent
    assert card["owner"]["name"] == identity.owner_of("C01") and "Bäckerei Steiner" in card["duty"]
    did = d.agent_did(seat_of("C01"))
    d.kill("C01", "t")
    d.ledger.entries.append({"seq": 99, "actor": did, "gate": "OK", "action": "payment.decide"})
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


def test_risk_score_learns_fraud_patterns():
    rows = risk.generate_history(n=600, seed=1)
    assert rows == risk.generate_history(n=600, seed=1)  # fixed seed, same data
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    vendors = load_json(DATA / "vendors.json")
    base = risk.amount_baseline(rows)
    X = [risk.features(r, mandates[r["client_id"]], vendors[r["client_id"]], base) for r in rows]
    y = [r["is_fraud"] for r in rows]
    Xs, _ = risk.standardize(X)
    w = risk.fit_logistic(Xs, y, 1.0)
    assert risk.roc_auc(y, risk.predict(w, Xs)) > 0.85
    assert w[1 + risk.FEATURES.index("iban_changed")] > 0 and w[1 + risk.FEATURES.index("urgency_terms")] > 0


def test_reference_run_voids_payments_whose_file_is_not_issued(tmp_path, monkeypatch):
    from agent import evaluate
    from agent.common import read_jsonl
    preds = read_jsonl(Path(__file__).resolve().parents[1] / "results" / "predictions.jsonl")
    test = [c for c in CASES if c["split"] == "test"]
    info = {"manual_gen0_sha256": "a", "manual_gen1_sha256": "b", "overruled": 0}
    monkeypatch.setattr(evaluate.pain001, "validate", lambda xml: ["injected failure"])
    files, ledger_ok, _, _ = evaluate.write_outputs(test, preds, copy.deepcopy(BY_ID), "manual", info, tmp_path)
    led = read_jsonl(tmp_path / "ledger.jsonl")
    paid = {e["case_id"] for e in led if e.get("action") == "payment.decide" and e.get("gate") == "OK" and e.get("decision") == "pay"}
    voided = {e["case_id"] for e in led if e.get("action") == "payment.voided"}
    queued = {i["case_id"] for i in __import__("json").loads((tmp_path / "queue.json").read_text(encoding="utf-8"))
              if "FILE_INVALID" in i["checks"]}
    assert files == {} and ledger_ok and not list((tmp_path / "payments").iterdir())
    assert paid and paid == voided == queued  # no file, no payment: every decision is voided and back with a human
