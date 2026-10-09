"""End-to-end run: lessons from the train split, evaluation on the test split, payment files, ledger."""
import json
import os
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import badge, decide, identity, lessons, llm, pain001, prompts, security
from .common import DATA, RESULTS, load_json, normalize_iban, read_jsonl, sha256, write_jsonl
from .datagen import TODAY
from .desk import Desk
from .ledger import Ledger, audit_identities, verify

SYSTEMS = {
    "baseline": "Apertus alone (policy + manual in prompt)",
    "badge_only": "Apertus extraction + badge (hard checks), no content review",
    "harness_gen0": "Apertus extraction + badge + review with manual gen 0",
    "harness_gen1": "Apertus extraction + badge + review with manual gen 1 (lessons)",
    "harness_gen1_promoted": "as gen 1, plus two lessons promoted to hard checks (sum, addressee); added after test results",
}
PRODUCTION = "harness_gen1_promoted"  # the configuration whose ledger, payment files and queue are written


def load():
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    suppliers = load_json(DATA / "vendors.json")
    cases = read_jsonl(DATA / "cases.jsonl")
    return mandates, suppliers, cases


def pmap(fn, items):
    with ThreadPoolExecutor(max_workers=int(os.environ.get("LLM_CONCURRENCY", "4"))) as ex:
        return list(ex.map(fn, items))


def learn_lessons(train, mandates, suppliers):
    """Run gen 0 on the train split; reviewer notes on every overruled case become lessons."""
    def run(c):
        m = mandates[c["client_id"]]
        return decide.harness(c, m, suppliers.get(c["client_id"], []), prompts.MANUAL_GEN0)
    outs = pmap(run, train)
    names = [m["name"] for m in mandates.values()] + [c["gold"]["vendor"] for c in train] + \
            [s["name"] for ss in suppliers.values() for s in ss]
    overruled = [(c, o) for c, o in zip(train, outs) if o["decision"] != c["label"]]
    masked = [lessons.mask(c["reviewer_note"], names) for c, _ in overruled]
    rules = lessons.distill(masked)
    manual1 = lessons.manual_with_lessons(rules, 1)
    info = {"train_cases": len(train), "overruled": len(overruled),
            "overruled_by_code": dict(Counter(c["reason_code"] for c, _ in overruled)),
            "masked_notes": masked, "rules": rules,
            "manual_gen0_sha256": lessons.fingerprint(prompts.MANUAL_GEN0),
            "manual_gen1_sha256": lessons.fingerprint(manual1)}
    return manual1, info


def evaluate_case(c, mandates, suppliers, manual1):
    m, sup = mandates[c["client_id"]], suppliers.get(c["client_id"], [])
    base = decide.baseline(c, m, sup)
    fields = decide.extract(c)
    h0 = decide.harness(c, m, sup, prompts.MANUAL_GEN0, fields=fields)
    h1 = decide.harness(c, m, sup, manual1, fields=fields)
    h2 = decide.harness(c, m, sup, manual1, fields=fields, promoted=True)

    def view(h, decision):
        v = {"decision": decision, "reason": h.get("reason", ""), "findings": h["findings"]}
        err = fields.get("error") or (h.get("review") or {}).get("error")
        if err:
            v["error"] = err
        return v
    return {"case_id": c["case_id"], "client_id": c["client_id"], "lang": c["lang"], "label": c["label"],
            "reason_code": c["reason_code"], "gold": c["gold"], "fields": fields, "baseline": base,
            "badge_only": view({"findings": h0["findings"]}, h0["badge_only"]),
            "harness_gen0": view(h0, h0["decision"]), "harness_gen1": view(h1, h1["decision"]),
            "harness_gen1_promoted": view(h2, h2["decision"])}


def score(preds, system):
    n = len(preds)
    pay = [p for p in preds if p["label"] == "pay"]
    nonpay = [p for p in preds if p["label"] != "pay"]
    d = lambda p: p[system]["decision"]
    wrong = [p for p in nonpay if d(p) == "pay"]
    return {
        "accuracy": round(sum(d(p) == p["label"] for p in preds) / n, 3),
        "wrong_payment_rate": round(len(wrong) / len(nonpay), 3),
        "wrong_payments": len(wrong),
        "wrong_payment_chf": round(sum(p["gold"]["total_chf"] for p in wrong), 2),
        "missed_payment_rate": round(sum(d(p) != "pay" for p in pay) / len(pay), 3),
        "reject_recall": round(sum(d(p) == "reject" for p in preds if p["label"] == "reject") /
                               max(1, sum(p["label"] == "reject" for p in preds)), 3),
        "model_failures": sum(1 for p in preds if p[system].get("error")),
        # a paid invoice is only right if the payment file carries the right account and amount
        "paid_with_wrong_details": None if system == "baseline" else sum(
            1 for p in preds if d(p) == "pay" and not _details_ok(p)),
        "by_reason_code": {code: round(sum(d(p) == p["label"] for p in grp) / len(grp), 2)
                           for code, grp in sorted(group(preds).items())},
    }


def _details_ok(p):
    f, g = p["fields"], p["gold"]
    try:
        amount_ok = abs(float(f.get("total_chf")) - g["total_chf"]) < 0.01
    except (TypeError, ValueError):
        amount_ok = False
    return amount_ok and normalize_iban(f.get("vendor_iban")) == normalize_iban(g["iban"])


def group(preds):
    g = defaultdict(list)
    for p in preds:
        g[p["reason_code"]].append(p)
    return g


def extraction_scores(preds):
    ok = defaultdict(int)
    for p in preds:
        f, g = p["fields"], p["gold"]
        ok["iban"] += normalize_iban(f.get("vendor_iban")) == normalize_iban(g["iban"])
        try:
            ok["total_chf"] += abs(float(f.get("total_chf")) - g["total_chf"]) < 0.01
        except (TypeError, ValueError):
            pass
        ok["invoice_no"] += str(f.get("invoice_no", "")).strip().upper() == g["invoice_no"].upper()
        ok["vendor"] += badge.same_name(f.get("vendor_name"), g["vendor"])
        ok["addressee"] += badge.same_name(f.get("addressee"), g["addressee"])
        ok["parsed"] += "error" not in f
    return {k: round(v / len(preds), 3) for k, v in ok.items()}


def write_outputs(test, preds, mandates, manual1, lesson_info, out):
    """Production view of the gen-1 run: ledger, payment files, approval queue."""
    ledger_path = out / "ledger.jsonl"
    for stale in (ledger_path, out / "state.json"):  # a new run starts a fresh ledger and web-app state
        if stale.exists():
            stale.unlink()
    led = Ledger(ledger_path)
    desk = Desk(led, mandates, clock=world_clock())
    desk.open_books()
    led.append(actor=identity.RESPONSIBLE_HUMAN, action="manual.published", generation=0,
               manual_sha256=lesson_info["manual_gen0_sha256"])
    led.append(actor=identity.RESPONSIBLE_HUMAN, action="manual.published", generation=1,
               manual_sha256=lesson_info["manual_gen1_sha256"], based_on_overrules=lesson_info["overruled"])
    desk.hire(manual_sha256=lesson_info["manual_gen1_sha256"])
    by_case = {c["case_id"]: c for c in test}
    payments, queue, gate_codes = defaultdict(list), [], Counter()
    for p in preds:
        c, m, h = by_case[p["case_id"]], mandates[p["client_id"]], p[PRODUCTION]
        f = p["fields"]
        payload = {"case_id": c["case_id"], "decision": h["decision"], "vendor": f.get("vendor_name"),
                   "iban": normalize_iban(f.get("vendor_iban")), "amount_chf": _num(f.get("total_chf")),
                   "executes_payment": h["decision"] == "pay"}
        res = desk.request("payment.decide", m["client_id"], payload, case_id=c["case_id"], decision=h["decision"],
                           vendor=f.get("vendor_name"), iban=payload["iban"], amount_chf=payload["amount_chf"],
                           reason=h.get("reason", ""), checks=[x["code"] for x in h.get("findings", [])],
                           manual_sha256=lesson_info["manual_gen1_sha256"], model=llm.model_name())
        gate_codes[res["gate"]] += 1
        if not res["ok"]:
            continue
        if h["decision"] == "pay":
            payments[m["client_id"]].append({"end_to_end_id": c["case_id"], "creditor": f.get("vendor_name", ""),
                                             "iban": f.get("vendor_iban"), "amount_chf": float(f["total_chf"]),
                                             "reference": f.get("invoice_no", "")})
        elif h["decision"] == "escalate":
            queue.append({"case_id": c["case_id"], "client_id": m["client_id"], "client": m["name"],
                          "vendor": f.get("vendor_name"), "amount_chf": f.get("total_chf"),
                          "iban": payload["iban"],
                          "reason": h.get("reason", ""), "invoice_text": c["invoice_text"], "status": "open"})
    pay_dir = out / "payments"
    shutil.rmtree(pay_dir, ignore_errors=True)
    pay_dir.mkdir(parents=True)
    files = {}
    for cid, pays in sorted(payments.items()):
        xml = pain001.build(mandates[cid], pays, f"{cid}-{TODAY:%Y%m%d}-01",
                            datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0), TODAY.isoformat())
        info = {"transactions": len(pays), "total_chf": round(sum(p["amount_chf"] for p in pays), 2),
                "validation_errors": pain001.validate(xml)}
        res = desk.request("payment.file", cid, {"file_sha256": sha256(xml), **info},
                           file=f"payments/{cid}_pain001.xml", transactions=info["transactions"],
                           total_chf=info["total_chf"])
        gate_codes[res["gate"]] += 1
        if res["ok"]:
            (pay_dir / f"{cid}_pain001.xml").write_text(xml, encoding="utf-8")
            files[cid] = info
    (out / "desk.json").write_text(json.dumps(desk.dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "queue.json").write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "manual_gen1.md").write_text(manual1 + "\n", encoding="utf-8")
    return files, verify(led.entries)[0], dict(gate_codes), audit_identities(led.entries)


def world_clock(start=None):
    """Deterministic clock for the reference run: the synthetic world's date, one second per request."""
    t = [start or datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0, tzinfo=timezone.utc)]

    def now():
        t[0] += timedelta(seconds=1)
        return t[0]
    return now


def _num(x):
    try:
        return round(float(x), 2)
    except (TypeError, ValueError):
        return None


def summary_md(metrics, extraction, lesson_info, files, ledger_ok, n_test, gate_codes, attacks, audit):
    rows = "\n".join(
        f"| {name} | {SYSTEMS[name]} | {m['accuracy']:.3f} | {m['wrong_payment_rate']:.3f} "
        f"({m['wrong_payments']}) | {m['wrong_payment_chf']:,.2f} | {m['missed_payment_rate']:.3f} | {m['model_failures']} |"
        for name, m in metrics.items())
    codes = sorted(next(iter(metrics.values()))["by_reason_code"])
    code_rows = "\n".join(f"| {code} | " + " | ".join(f"{metrics[s]['by_reason_code'][code]:.2f}" for s in metrics) + " |"
                          for code in codes)
    rules = "\n".join(f"- {r}" for r in lesson_info["rules"]) or "- (none)"
    return f"""# Evaluation summary

Model: `{llm.model_name()}` | test cases: {n_test} | LLM mode: {llm.mode()}

| System | What it is | Accuracy | Wrong-payment rate (n) | Wrongly paid CHF | Missed-payment rate | Model failures |
|---|---|---|---|---|---|---|
{rows}

Wrong-payment rate = share of invoices that must not be paid automatically (escalate or reject) that the system paid.
Missed-payment rate = share of payable invoices the system did not pay.

## Correct decision rate by case type

| Case type | {' | '.join(metrics)} |
|---|{'---|' * len(metrics)}
{code_rows}

## Extraction accuracy (Apertus, test split)

{json.dumps(extraction)}

## Lessons learned on the train split

Overruled cases: {lesson_info['overruled']} of {lesson_info['train_cases']} ({lesson_info['overruled_by_code']}).
Rules distilled by Apertus into manual generation 1 (sha256 `{lesson_info['manual_gen1_sha256'][:16]}…`):

{rules}

## Outputs

Payment files (pain.001.001.09): {json.dumps(files)}
Ledger (hash chain + ledger-key signatures) verifies: {ledger_ok}
Gate results for the reference run's agent requests: {json.dumps(gate_codes)}
Identity audit from the ledger alone: {audit["agent_actions"]} agent actions, all by agents the firm issued and had not revoked: {audit["ok"]} (agents issued: {json.dumps(audit["agents_issued"])})

## Agent verification: attacks against the gate

Every request is signed by the agent's key and must pass the gate's five checks.
Each attack is tried once per paid invoice of the reference run.

| Scenario | Attempts | Blocked | Caught by |
|---|---|---|---|
{security.table_rows(attacks)}
"""


def run():
    mandates, suppliers, cases = load()
    train = [c for c in cases if c["split"] == "train"]
    test = [c for c in cases if c["split"] == "test"]
    out = RESULTS
    out.mkdir(parents=True, exist_ok=True)
    print(f"LLM mode: {llm.mode()} | model: {llm.model_name()} | train {len(train)} | test {len(test)}", flush=True)
    if llm.mode() == "replay" and not llm.CACHE_PATH.exists():
        print("WARNING: no recorded Apertus responses found; every model call fails closed (escalate).", flush=True)

    manual1, lesson_info = learn_lessons(train, mandates, suppliers)
    print(f"lessons: {lesson_info['overruled']} overruled train cases -> {len(lesson_info['rules'])} rules", flush=True)
    preds = pmap(lambda c: evaluate_case(c, mandates, suppliers, manual1), test)
    write_jsonl(out / "predictions.jsonl", preds)

    metrics = {s: score(preds, s) for s in SYSTEMS}
    extraction = extraction_scores(preds)
    files, ledger_ok, gate_codes, audit = write_outputs(test, preds, mandates, manual1, lesson_info, out)
    attacks = security.run(preds, mandates, out)
    report = {"model": llm.model_name(), "llm_mode": llm.mode(), "n_test": len(test), "metrics": metrics,
              "extraction": extraction, "lessons": lesson_info, "payment_files": files, "ledger_verifies": ledger_ok,
              "gate_reference_run": gate_codes, "identity_audit": audit, "attacks": attacks}
    (out / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = summary_md(metrics, extraction, lesson_info, files, ledger_ok, len(test), gate_codes, attacks, audit)
    (out / "summary.md").write_text(md, encoding="utf-8")
    print(md, flush=True)
    return report
