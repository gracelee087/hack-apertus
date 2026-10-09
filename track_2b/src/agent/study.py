"""The study: is the frozen design as good on wider, unseen invoices, and where should the second look sit?

Two sets drawn from datagen.STUDY_UNIT with their own seeds, after the design was frozen:
  tune  (500)  - choose the second-look threshold by the rule below, nothing else
  final (1000) - run once with that threshold fixed; never used to choose anything
Both add two invoice layouts, invoices with two problems at once and invoices with an unreadable field.

Threshold rule (fixed before either set was run): among 0, 5, ..., 100 (100 = second look off), take the
threshold with the fewest wrong payments whose extra holds of legitimate invoices stay at or below 2% of the
legitimate invoices; on a tie, the higher threshold (fewer holds).

Everything uses the lessons of the original train split (results/manual_gen1.md) and the risk model exactly as
fitted by `eval`. Apertus responses go to their own cache (data/cache/study_responses.jsonl), so the reference
run's recordings stay as they are. The run stops at the first endpoint error (e.g. 402: credit used up) instead
of failing closed on every remaining invoice; recorded responses are kept, so a rerun resumes for free.
"""
import json
import math
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from . import datagen, evaluate, lessons, llm, risk
from .common import DATA, RESULTS, read_jsonl, write_jsonl

OUT = RESULTS / "study"
THRESHOLDS = list(range(0, 101, 5))
MAX_EXTRA_HOLDS = 0.02
STYLES = ["block", "line", "email", "qr"]
SETS = {"tune": (datagen.STUDY_TUNE_SEED, 5, "S"), "final": (datagen.STUDY_FINAL_SEED, 10, "F")}
FATAL = ("Apertus endpoint error", "Apertus endpoint unreachable", "ReplayMiss")


def data_path(name):
    return DATA / f"study_{name}.jsonl"


def generate():
    for name, (seed, scale, prefix) in SETS.items():
        cases = datagen.build(seed, datagen.study_plan(scale), prefix, styles=STYLES)[2]
        for c in cases:
            c["split"] = f"study_{name}"
        write_jsonl(data_path(name), cases)
        print(f"wrote {len(cases)} {name} invoices -> {data_path(name).name}")


def frozen_manual():
    """Manual generation 1 exactly as the reference run wrote it (no new distilling call)."""
    text = (RESULTS / "manual_gen1.md").read_text(encoding="utf-8").rstrip("\n")
    want = json.loads((RESULTS / "metrics.json").read_text(encoding="utf-8"))["lessons"]["manual_gen1_sha256"]
    assert lessons.fingerprint(text) == want, "results/manual_gen1.md is not the manual of the reference run"
    return text


def run_cases(cases):
    """evaluate_case for every invoice; stop everything at the first endpoint error."""
    mandates, suppliers, _ = evaluate.load()
    manual1, stop, out = frozen_manual(), threading.Event(), {}

    def one(c):
        if stop.is_set():
            return
        p = evaluate.evaluate_case(c, mandates, suppliers, manual1)
        if any(f in json.dumps(p) for f in FATAL):
            stop.set()
            print(f"\nSTOP at {c['case_id']}: {next(e for e in _errors(p) if any(f in e for f in FATAL))}", flush=True)
            return
        out[c["case_id"]] = p
        if len(out) % 50 == 0:
            print(f"  {len(out)} / {len(cases)}", flush=True)

    with ThreadPoolExecutor(max_workers=int(os.environ.get("LLM_CONCURRENCY", "4"))) as ex:
        list(ex.map(one, cases))
    if stop.is_set():
        sys.exit(f"stopped after {len(out)} of {len(cases)} invoices; recorded responses are kept, rerun to resume")
    return [out[c["case_id"]] for c in cases]


def _errors(p):
    for k, v in p.items():
        if isinstance(v, dict) and v.get("error"):
            yield str(v["error"])
    if p["fields"].get("error"):
        yield str(p["fields"]["error"])


def at(p, t):
    """Production decision with the second look at threshold t (100 = off): the promoted harness, then the score."""
    d = p["harness_gen1_promoted"]["decision"]
    return "escalate" if d == "pay" and t < 100 and p["risk_score"] >= t else d


def sweep(preds):
    legit = sum(p["label"] == "pay" for p in preds)
    rows = []
    for t in THRESHOLDS:
        wrong = [p for p in preds if p["label"] != "pay" and at(p, t) == "pay"]
        held = sum(p["label"] == "pay" and at(p, t) != "pay" for p in preds)
        rows.append({"threshold": t, "wrong_payments": len(wrong), "wrong_chf": round(sum(p["gold"]["total_chf"] for p in wrong), 2),
                     "legit_held": held, "legit": legit})
    off = rows[-1]["legit_held"]
    ok = [r for r in rows if r["legit_held"] - off <= MAX_EXTRA_HOLDS * legit]
    best = min(ok, key=lambda r: (r["wrong_payments"], -r["threshold"]))
    return rows, best["threshold"]


def wilson(k, n, z=1.96):
    if not n:
        return [None, None]
    p, d = k / n, 1 + z * z / n
    c, h = (p + z * z / (2 * n)) / d, z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(max(0, c - h), 4), round(min(1, c + h), 4)]


def report(preds, threshold):
    for p in preds:  # the production system of this study: the chosen threshold
        p["production"] = {"decision": at(p, threshold), "findings": [], "error": p["harness_gen1_promoted"].get("error")}
    systems = list(evaluate.SYSTEMS) + ["production"]
    nonpay, pay = sum(p["label"] != "pay" for p in preds), sum(p["label"] == "pay" for p in preds)
    out = {}
    for s in systems:
        m = evaluate.score(preds, s)
        m["wrong_payment_rate_ci95"] = wilson(m["wrong_payments"], nonpay)
        m["missed_payment_rate_ci95"] = wilson(round(m["missed_payment_rate"] * pay), pay)
        m["accuracy_ci95"] = wilson(round(m["accuracy"] * len(preds)), len(preds))
        out[s] = m
    by_layout = {}
    for p in preds:
        by_layout.setdefault(p.get("layout", "?"), []).append(p)
    return out, {k: evaluate.score(v, "production") | {"n": len(v)} for k, v in by_layout.items() if any(x["label"] != "pay" for x in v)}


def run(name):
    if name not in SETS:
        sys.exit("usage: python -m agent study [gen|tune|final]")
    if not data_path(name).exists():
        generate()
    if not os.environ.get("LLM_CACHE_PATH"):
        sys.exit("set LLM_CACHE_PATH=data/cache/study_responses.jsonl so the reference recordings stay untouched")
    OUT.mkdir(parents=True, exist_ok=True)
    cases = read_jsonl(data_path(name))
    layout = {c["case_id"]: c.get("layout") for c in cases}
    if name == "final":
        if (OUT / "final.json").exists() and not os.environ.get("STUDY_FORCE"):
            sys.exit("the final set has been run; it is run once (STUDY_FORCE=1 only to re-score from the recordings)")
        threshold = json.loads((OUT / "tune.json").read_text(encoding="utf-8"))["chosen_threshold"]
    print(f"LLM mode: {llm.mode()} | model: {llm.model_name()} | {name}: {len(cases)} invoices | cache {llm.CACHE_PATH}", flush=True)
    preds = run_cases(cases)
    for p in preds:
        p["layout"] = layout[p["case_id"]]
    rows, chosen = sweep(preds)
    if name == "tune":
        threshold = chosen
    metrics, by_layout = report(preds, threshold)
    write_jsonl(OUT / f"{name}_predictions.jsonl", preds)
    result = {"set": name, "n": len(preds), "seed": SETS[name][0], "model": llm.model_name(), "llm_mode": llm.mode(),
              "rule": __doc__.split("Threshold rule")[1].split("\n\n")[0].strip(),
              "chosen_threshold" if name == "tune" else "threshold_used": threshold,
              "threshold_sweep": rows, "metrics": metrics, "production_by_layout": by_layout,
              "extraction": evaluate.extraction_scores(preds)}
    (OUT / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    m = metrics["production"]
    print(f"\n{name}: threshold {threshold} | accuracy {m['accuracy']} | wrong payments {m['wrong_payments']} "
          f"(rate {m['wrong_payment_rate']}, 95% CI {m['wrong_payment_rate_ci95']}) | missed {m['missed_payment_rate']}", flush=True)
    for s, x in metrics.items():
        print(f"  {s:28} acc {x['accuracy']:.3f}  wrong {x['wrong_payments']:4}  CHF {x['wrong_payment_chf']:>12,.2f}  missed {x['missed_payment_rate']:.3f}")
    return result


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "gen":
        generate()
    else:
        run(cmd)
