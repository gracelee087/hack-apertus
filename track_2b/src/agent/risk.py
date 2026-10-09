"""Risk score: a supervised fraud classifier next to the rules (the data-science layer).

Training data: 3,000 synthetic past payments of the firm's clients (data/history/payments.jsonl,
fixed seed). Fraud follows known supplier-fraud patterns (bank-detail swap, inflated amount, fake
supplier, pressure, amounts that do not add up, wrong addressee, duplicate, invalid IBAN), mixed and
overlapping, with look-alike legitimate payments and a few wrong labels.

Test data: the 78 test invoices as Apertus read them, scored against the same payment history.
Target A (main): fraud or not. Target B (comparison): anything that must not be paid automatically,
which the rules already catch, so its score mostly shows the model relearning the rules.

The score never changes a payment decision. It ranks the approval queue and explains itself.
"""
import json
import math
import random
import threading
from collections import Counter
from datetime import date

from . import badge
from .common import DATA, RESULTS, iban_valid, load_json, make_ch_iban, normalize_iban, read_jsonl, write_jsonl
from .datagen import TODAY

HISTORY = DATA / "history" / "payments.jsonl"
N_HISTORY = 3000
SEED = 7
FRAUD_CODES = {"E_IBAN_CHANGED", "R_IBAN_INVALID", "R_DUPLICATE", "E_VAT_MISMATCH", "E_WRONG_ADDRESSEE", "E_PRESSURE"}
FEATURES = ["amount_z", "iban_changed", "iban_invalid", "new_vendor", "round_amount", "vat_mismatch",
            "wrong_addressee", "urgency_terms", "short_due", "duplicate", "over_single_limit", "out_of_scope",
            "month_load"]
# pressure and secrecy words in the four invoice languages (counted in the invoice text)
URGENCY = ["urgent", "dringend", "sofort", "immédiat", "today", "heute", "aujourd'hui", "oggi", "same day",
           "do not call", "nicht anrufen", "ne pas appeler", "non telefonate", "confidential", "vertraulich",
           "confidentiel", "riservato", "debt collection", "betreibung", "poursuites", "esecuzione"]
TYPICAL = {"ingredients": (400, 2500), "utilities": (200, 1500), "rent": (1500, 6000), "insurance": (300, 2500),
           "materials": (200, 2000), "software": (100, 3000), "cloud": (300, 5000), "telecom": (80, 800),
           "consulting": (1500, 8000)}
FAKE_VENDORS = ["Alpen Consulting Partners GmbH", "Helvetia Solutions Sàrl", "Swiss Office Supply Center AG",
                "Lakeside Services Ltd", "Servizi Ticino Sagl", "Romandie Facility SA", "Euro Trade Hub GmbH"]


# --- training data --------------------------------------------------------------------------

def generate_history(n=N_HISTORY, seed=SEED) -> list:
    rng = random.Random(seed)
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    vendors = load_json(DATA / "vendors.json")
    base = {v["vendor_id"]: rng.uniform(*TYPICAL.get(v["category"], (200, 3000))) for vs in vendors.values() for v in vs}
    seen = {}  # each supplier's usual amount, from the legitimate TRAIN-split invoices only (never the test split)
    for c in read_jsonl(DATA / "cases.jsonl"):
        if c["split"] == "train" and c["reason_code"] == "OK":
            v = next((v for v in vendors[c["client_id"]] if badge.same_name(v["name"], c["gold"]["vendor"])), None)
            if v:
                seen.setdefault(v["vendor_id"], []).append(c["gold"]["total_chf"])
    base.update({k: _median(xs) for k, xs in seen.items()})
    kinds = ([("legit", None)] * 76 + [("control", c) for c in ("limit", "scope", "new_vendor", "month")] * 3
             + [("fraud", f) for f in ["iban_swap"] * 4 + ["inflated"] * 2 + ["fake_vendor"] * 2 + ["pressure"] * 2
                + ["vat_mismatch", "wrong_addressee", "duplicate", "invalid_iban"]])
    rows = []
    for i in range(n):
        cid = rng.choice(sorted(vendors))
        m, v = mandates[cid], rng.choice(vendors[cid])
        kind, sub = rng.choice(kinds)
        amount = base[v["vendor_id"]] * rng.lognormvariate(0, 0.25)
        r = {"row_id": f"H{i:04d}", "client_id": cid, "vendor": v["name"], "iban": v["iban"], "category": v["category"],
             "urgency_terms": 0, "days_to_due": rng.choice([30] * 6 + [rng.randint(10, 45)] * 3 + [rng.randint(3, 9)]),
             "addressee_ok": True, "duplicate": False, "vat_ok": True,
             "month_spent": round(rng.uniform(0, 0.7) * m["monthly_limit_chf"], 2), "kind": kind, "pattern": sub}
        if kind == "legit":  # look-alikes: genuine spikes, a real change of bank, a polite reminder
            u = rng.random()
            if u < 0.03:
                amount *= rng.uniform(2.5, 4)
            elif u < 0.05:
                r["iban"] = _new_iban(rng)
            elif u < 0.09:
                r["urgency_terms"] = 1
        elif kind == "control":
            if sub == "limit":
                amount = m["single_limit_chf"] * rng.uniform(1.05, 2.5)
            elif sub == "scope":
                r["category"] = rng.choice([c for c in TYPICAL if c not in m["allowed_categories"]])
                r["vendor"], r["iban"] = rng.choice(FAKE_VENDORS), _new_iban(rng)
            elif sub == "new_vendor":
                r["vendor"], r["iban"] = rng.choice(FAKE_VENDORS), _new_iban(rng)
            else:
                r["month_spent"] = round(m["monthly_limit_chf"] * rng.uniform(0.9, 1.1), 2)
        else:
            subtle = rng.random() < 0.15  # some fraud shows only its core signal
            if sub == "iban_swap":
                r["iban"] = _new_iban(rng)
                amount *= rng.uniform(1, 1.5)
                if not subtle:
                    r["urgency_terms"] = rng.choice([0, 1, 1, 2])
                    r["days_to_due"] = rng.choice([r["days_to_due"], rng.randint(2, 9)])
            elif sub == "inflated":
                amount *= rng.uniform(2, 6)
                if not subtle and rng.random() < 0.4:
                    amount = round(amount, -2)
            elif sub == "fake_vendor":
                r["vendor"], r["iban"] = rng.choice(FAKE_VENDORS), _new_iban(rng)
                if not subtle:
                    amount = round(amount, -2) if rng.random() < 0.6 else amount
                    r["urgency_terms"] = rng.choice([0, 1, 2])
            elif sub == "pressure":
                r["urgency_terms"] = rng.randint(1, 2) if subtle else rng.randint(2, 5)
                if not subtle:
                    r["days_to_due"] = rng.randint(1, 9) if rng.random() < 0.7 else r["days_to_due"]
                if rng.random() < 0.3:
                    r["iban"] = _new_iban(rng)
            elif sub == "vat_mismatch":
                r["vat_ok"] = False
            elif sub == "wrong_addressee":
                r["addressee_ok"] = False
            elif sub == "duplicate":
                r["duplicate"] = True
            elif sub == "invalid_iban":  # one digit off, so the checksum fails
                s = _new_iban(rng)
                r["iban"] = s[:-1] + str((int(s[-1]) + 1) % 10)
        r["total_chf"] = round(round(amount * 20) / 20, 2)
        if kind == "legit" and v["category"] == "rent" and rng.random() < 0.7:
            r["total_chf"] = float(round(amount, -2))  # rent is usually a round amount
        r["is_fraud"] = int(kind == "fraud")
        r["must_not_autopay"] = int(kind != "legit" or r["iban"] != v["iban"])
        if rng.random() < 0.02:  # a few wrong labels, as in any real case log
            r["is_fraud"] = 1 - r["is_fraud"]
        rows.append(r)
    return rows


def ensure_history() -> list:
    if not HISTORY.exists():
        HISTORY.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(HISTORY, generate_history())
    return read_jsonl(HISTORY)


# --- features ---------------------------------------------------------------------------------

def amount_baseline(history) -> dict:
    """(client, vendor) -> median and MAD of log amounts in the payment history (labels not used)."""
    groups = {}
    for r in history:
        groups.setdefault((r["client_id"], badge._fold(r["vendor"])), []).append(math.log(max(r["total_chf"], 1)))
    out = {}
    for k, xs in groups.items():
        if len(xs) >= 5:
            med = _median(xs)
            out[k] = (med, max(_median([abs(x - med) for x in xs]) * 1.4826, 0.05))
    return out


def features(r, mandate, suppliers, baseline) -> list:
    known = next((s for s in suppliers if badge.same_name(s["name"], r["vendor"])), None)
    iban, total = normalize_iban(r.get("iban")), r.get("total_chf") or 0.0
    med = baseline.get((r["client_id"], badge._fold(known["name"]))) if known else None
    z = max(-5.0, min(10.0, (math.log(max(total, 1)) - med[0]) / med[1])) if med else 0.0
    category = known["category"] if known else r.get("category")
    return [round(z, 4), int(bool(known) and iban_valid(iban) and iban != known["iban"]), int(not iban_valid(iban)),
            int(not known), int(total >= 500 and total % 100 == 0), int(not r.get("vat_ok", True)),
            int(not r.get("addressee_ok", True)), min(int(r.get("urgency_terms") or 0), 5),
            int(r.get("days_to_due") is not None and r["days_to_due"] < 10), int(bool(r.get("duplicate"))),
            int(total > mandate["single_limit_chf"]), int(category not in mandate["allowed_categories"]),
            round(min(2.0, (r.get("month_spent", 0) + total) / mandate["monthly_limit_chf"]), 4)]


def test_row(case, fields, mandate) -> dict:
    """The same raw record for a test invoice, from what Apertus read and the client's records."""
    text = case["invoice_text"].lower()
    num = lambda x: badge._num(x)
    net, vat, total = num(fields.get("net_chf")), num(fields.get("vat_chf")), num(fields.get("total_chf"))
    try:
        due = (date.fromisoformat(str(fields.get("due_date"))) - TODAY).days
    except ValueError:
        due = None
    inv = str(fields.get("invoice_no") or "").strip().upper()
    prior = case["prior_payments"]
    return {"client_id": case["client_id"], "vendor": fields.get("vendor_name") or "", "iban": fields.get("vendor_iban"),
            "category": fields.get("category"), "total_chf": total or 0.0,
            "vat_ok": None in (net, vat, total) or abs(net + vat - total) <= 0.05,
            "addressee_ok": badge.addressed_to(fields.get("addressee"), mandate["name"]),
            "urgency_terms": sum(text.count(w) for w in URGENCY), "days_to_due": due,
            "duplicate": bool(inv) and any(badge.same_name(p["vendor"], fields.get("vendor_name")) and
                                           p["invoice_no"].upper() == inv for p in prior),
            "month_spent": sum(p["amount_chf"] for p in prior)}


# --- models -----------------------------------------------------------------------------------

def standardize(X, stats=None):
    if stats is None:
        cols = list(zip(*X))
        stats = [(sum(c) / len(c), max(math.sqrt(sum((x - sum(c) / len(c)) ** 2 for x in c) / len(c)), 1e-6)) for c in cols]
    return [[(x - m) / s for x, (m, s) in zip(row, stats)] for row in X], stats


def fit_logistic(X, y, l2=0.1, iters=25):
    """L2-regularised logistic regression by Newton's method (IRLS). X is standardised."""
    d = len(X[0]) + 1
    w = [0.0] * d
    Xb = [[1.0] + row for row in X]
    for _ in range(iters):
        p = [_sigmoid(sum(a * b for a, b in zip(w, row))) for row in Xb]
        g = [sum((pi - yi) * row[j] for pi, yi, row in zip(p, y, Xb)) + (l2 * w[j] if j else 0) for j in range(d)]
        H = [[sum(pi * (1 - pi) * row[i] * row[j] for pi, row in zip(p, Xb)) + (l2 if i == j and i else 0)
              for j in range(d)] for i in range(d)]
        step = _solve(H, g)
        w = [wi - si for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    return w


def predict(w, X):
    return [_sigmoid(w[0] + sum(a * b for a, b in zip(w[1:], row))) for row in X]


def anomaly_scores(X_train, X):
    """Unsupervised baseline: how far each feature sits above its usual (median) value, summed."""
    stats = []
    for c in zip(*X_train):
        med = _median(c)
        spread = _median([abs(x - med) for x in c]) * 1.4826 or (math.sqrt(sum((x - sum(c) / len(c)) ** 2 for x in c) / len(c)) or 1.0)
        stats.append((med, spread))
    return [sum(max(0.0, (x - m) / s) for x, (m, s) in zip(row, stats)) for row in X]


# --- metrics ----------------------------------------------------------------------------------

def roc_auc(y, s):
    pos, neg = [b for a, b in zip(y, s) if a], [b for a, b in zip(y, s) if not a]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def average_precision(y, s):
    order = sorted(zip(s, y), key=lambda t: -t[0])
    hits, total, n_pos = 0, 0.0, sum(y)
    for k, (_, yi) in enumerate(order, 1):
        if yi:
            hits += 1
            total += hits / k
    return round(total / n_pos, 4) if n_pos else None


def precision_at(y, s, k):
    top = sorted(zip(s, y), key=lambda t: -t[0])[:k]
    return round(sum(yi for _, yi in top) / len(top), 4) if top else None


def report(y, s, threshold=None):
    out = {"n": len(y), "positives": sum(y), "roc_auc": roc_auc(y, s), "avg_precision": average_precision(y, s),
           "precision_at_10": precision_at(y, s, 10)}
    if threshold is not None:
        flagged = [yi for yi, si in zip(y, s) if si >= threshold]
        out["recall_at_0.5"] = round(sum(flagged) / max(sum(y), 1), 4)
        out["precision_at_0.5"] = round(sum(flagged) / len(flagged), 4) if flagged else None
    return out


# --- run --------------------------------------------------------------------------------------

def _training_data():
    history = ensure_history()
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    vendors = load_json(DATA / "vendors.json")
    baseline = amount_baseline(history)
    X = [features(r, mandates[r["client_id"]], vendors[r["client_id"]], baseline) for r in history]
    idx = list(range(len(X)))
    random.Random(SEED).shuffle(idx)
    cut = int(0.8 * len(idx))
    return history, mandates, vendors, baseline, X, idx[:cut], idx[cut:]


def train(history, X, tr, va, target):
    """Pick the L2 weight on the validation split, then refit on all rows."""
    y = [h[target] for h in history]
    Xtr, stats = standardize([X[i] for i in tr])
    Xva, _ = standardize([X[i] for i in va], stats)
    best, grid = None, []
    for l2 in (0.01, 0.1, 1.0, 10.0):
        auc = roc_auc([y[i] for i in va], predict(fit_logistic(Xtr, [y[i] for i in tr], l2), Xva))
        grid.append({"l2": l2, "val_roc_auc": auc})
        if best is None or auc > best[1] + 1e-9:
            best = (l2, auc)
    w_val = fit_logistic(Xtr, [y[i] for i in tr], best[0])
    s_val = predict(w_val, Xva)
    Xall, stats_all = standardize(X)
    return fit_logistic(Xall, y, best[0]), stats_all, best[0], report([y[i] for i in va], s_val, 0.5), grid, s_val, stats


def top_factors(w, z, n=3):
    """Only signals present on this invoice that push towards fraud (not "absence of a limit breach")."""
    contrib = sorted(((f, round(w[j + 1] * z[j], 3)) for j, f in enumerate(FEATURES) if z[j] > 0 and w[j + 1] > 0),
                     key=lambda t: -t[1])
    return [c for c in contrib if c[1] > 0][:n]


class Scorer:
    """The fraud model exactly as fitted in run(), scoring one invoice at a time (web app, second look)."""

    def __init__(self):
        history, self.mandates, self.vendors, self.baseline, X, tr, va = _training_data()
        self.w, self.stats = train(history, X, tr, va, "is_fraud")[:2]

    def score(self, case, fields):
        """case: client_id, invoice_text, prior_payments. Returns (score 0-100, top factors)."""
        m = self.mandates[case["client_id"]]
        z = standardize([features(test_row(case, fields, m), m, self.vendors[case["client_id"]], self.baseline)],
                        self.stats)[0][0]
        return round(100 * predict(self.w, [z])[0], 1), top_factors(self.w, z)


_scorer, _scorer_lock = None, threading.Lock()


def scorer() -> Scorer:
    global _scorer
    with _scorer_lock:
        if _scorer is None:
            _scorer = Scorer()
    return _scorer


def run(preds, cases_by_id, production="harness_gen1_promoted") -> dict:
    history, mandates, vendors, baseline, X, tr, va = _training_data()
    wA, statsA, l2A, valA, gridA, svA, stats_trA = train(history, X, tr, va, "is_fraud")
    wB, statsB, l2B, valB, gridB, svB, _ = train(history, X, tr, va, "must_not_autopay")
    yvA, yvB = [history[i]["is_fraud"] for i in va], [history[i]["must_not_autopay"] for i in va]
    svU = anomaly_scores([X[i] for i in tr], [X[i] for i in va])
    val_unsup = report(yvA, svU)

    rows, yA, yB, passed_rules, missed = [], [], [], [], []
    for p in preds:
        c = cases_by_id[p["case_id"]]
        m = mandates[p["client_id"]]
        rows.append(features(test_row(c, p["fields"], m), m, vendors[p["client_id"]], baseline))
        yA.append(int(p["reason_code"] in FRAUD_CODES))
        yB.append(int(p["label"] != "pay"))
        passed_rules.append(p["badge_only"]["decision"] == "pay")
        missed.append(p["label"] != "pay" and (p.get(production) or {}).get("decision") == "pay")
    sA = predict(wA, standardize(rows, statsA)[0])
    sB = predict(wB, standardize(rows, statsB)[0])
    sU = anomaly_scores(X, rows)

    sub = [i for i, ok in enumerate(passed_rules) if ok]  # what the rules let through: where a model can add something
    sub_rank = sorted(sub, key=lambda i: -sA[i])
    zA = standardize(rows, statsA)[0]
    scores = {}
    for i, p in enumerate(preds):
        amount = badge._num(p["fields"].get("total_chf")) or 0
        scores[p["case_id"]] = {"risk_score": round(100 * sA[i], 1), "expected_loss_chf": round(sA[i] * (amount or 0), 2),
                                "is_fraud": yA[i], "top_factors": top_factors(wA, zA[i])}
    out = {
        "train_rows": len(history), "train_fraud_rate": round(sum(h["is_fraud"] for h in history) / len(history), 4),
        "train_must_not_autopay_rate": round(sum(h["must_not_autopay"] for h in history) / len(history), 4),
        "features": FEATURES,
        "model_A": {"target": "fraud", "l2": l2A, "intercept": round(wA[0], 4),
                    "coefficients": {f: round(c, 4) for f, c in zip(FEATURES, wA[1:])}},
        "model_B": {"target": "must not be paid automatically", "l2": l2B},
        "validation": {"A_logistic": valA, "A_unsupervised": val_unsup, "B_logistic": valB},
        "test": {"A_logistic": report(yA, sA, 0.5), "A_unsupervised": report(yA, sU), "B_logistic": report(yB, sB, 0.5)},
        "beyond_rules": {"invoices_rules_let_through": len(sub), "fraud_among_them": sum(yA[i] for i in sub),
                         "roc_auc": roc_auc([yA[i] for i in sub], [sA[i] for i in sub]),
                         "missed_by_production": [{"case_id": preds[i]["case_id"], "reason_code": preds[i]["reason_code"],
                                                   "risk_score": round(100 * sA[i], 1),
                                                   "rank_among_passed": sub_rank.index(i) + 1 if i in sub else None}
                                                  for i, mm in enumerate(missed) if mm]},
        "scores": scores,
        "pipeline": pipeline(history, X, tr, va, rows, preds, yA, yB, sA, sU, sB, yvA, yvB, svA, svU, svB,
                             stats_trA, gridA, gridB),
    }
    (RESULTS / "risk_model.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def score_holdout(preds, cases_by_id, production="harness_gen1_promoted") -> dict:
    """Score invoices the model never saw with the final models of run(), unchanged (same data, same l2)."""
    saved = json.loads((RESULTS / "risk_model.json").read_text(encoding="utf-8"))
    history = ensure_history()
    mandates = {m["client_id"]: m for m in load_json(DATA / "mandates.json")}
    vendors = load_json(DATA / "vendors.json")
    baseline = amount_baseline(history)
    X = [features(r, mandates[r["client_id"]], vendors[r["client_id"]], baseline) for r in history]
    Xall, stats = standardize(X)
    wA = fit_logistic(Xall, [h["is_fraud"] for h in history], saved["model_A"]["l2"])
    wB = fit_logistic(Xall, [h["must_not_autopay"] for h in history], saved["model_B"]["l2"])
    assert [round(c, 4) for c in wA[1:]] == list(saved["model_A"]["coefficients"].values()), "risk model changed"
    rows, yA, yB, passed, missed = [], [], [], [], []
    for p in preds:
        m = mandates[p["client_id"]]
        rows.append(features(test_row(cases_by_id[p["case_id"]], p["fields"], m), m, vendors[p["client_id"]], baseline))
        yA.append(int(p["reason_code"] in FRAUD_CODES))
        yB.append(int(p["label"] != "pay"))
        passed.append(p["badge_only"]["decision"] == "pay")
        missed.append(p["label"] != "pay" and p[production]["decision"] == "pay")
    sA, sB = predict(wA, standardize(rows, stats)[0]), predict(wB, standardize(rows, stats)[0])
    sU = anomaly_scores(X, rows)
    sub = [i for i, ok in enumerate(passed) if ok]
    rank = sorted(sub, key=lambda i: -sA[i])
    return {"A_logistic": report(yA, sA, 0.5), "A_unsupervised": report(yA, sU), "B_logistic": report(yB, sB, 0.5),
            "confusion_A": [confusion(yA, sA, t) for t in (0.2, 0.5, 0.8)],
            "beyond_rules": {"invoices_rules_let_through": len(sub), "fraud_among_them": sum(yA[i] for i in sub),
                             "roc_auc": roc_auc([yA[i] for i in sub], [sA[i] for i in sub]) if 0 < sum(yA[i] for i in sub) < len(sub) else None,
                             "missed_by_production": [{"case_id": preds[i]["case_id"], "reason_code": preds[i]["reason_code"],
                                                       "risk_score": round(100 * sA[i], 1),
                                                       "rank_among_passed": rank.index(i) + 1 if i in sub else None}
                                                      for i, mm in enumerate(missed) if mm]},
            "scores": {p["case_id"]: round(100 * sA[i], 1) for i, p in enumerate(preds)}}


def roc_points(y, s, keep=60):
    """(false positive rate, true positive rate) at every score threshold, thinned for drawing."""
    P, N = sum(y), len(y) - sum(y)
    pts, tp, fp = [(0.0, 0.0)], 0, 0
    for _, yi in sorted(zip(s, y), key=lambda t: -t[0]):
        tp, fp = tp + yi, fp + (1 - yi)
        pts.append((round(fp / max(N, 1), 4), round(tp / max(P, 1), 4)))
    return _thin(pts, keep)


def pr_points(y, s, keep=60):
    """(recall, precision) going down the ranked list."""
    P, pts, tp = sum(y), [], 0
    for k, (_, yi) in enumerate(sorted(zip(s, y), key=lambda t: -t[0]), 1):
        tp += yi
        pts.append((round(tp / max(P, 1), 4), round(tp / k, 4)))
    return _thin(pts, keep)


def confusion(y, s, threshold):
    tp = sum(1 for yi, si in zip(y, s) if yi and si >= threshold)
    fp = sum(1 for yi, si in zip(y, s) if not yi and si >= threshold)
    fn, tn = sum(y) - tp, len(y) - sum(y) - fp
    prec, rec = (tp / (tp + fp) if tp + fp else None), tp / max(tp + fn, 1)
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else 0.0
    return {"threshold": threshold, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": None if prec is None else round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def histogram(y, s, bins=10):
    out = [{"from": b / bins, "to": (b + 1) / bins, "fraud": 0, "legit": 0} for b in range(bins)]
    for yi, si in zip(y, s):
        out[min(int(si * bins), bins - 1)]["fraud" if yi else "legit"] += 1
    return out


def pipeline(history, X, tr, va, rows, preds, yA, yB, sA, sU, sB, yvA, yvB, svA, svU, svB, stats_tr, gridA, gridB):
    """Every data-science step, as numbers for the data-analysis page."""
    pattern = Counter(f"{r['kind']}:{r['pattern'] or 'normal'}" for r in history)
    noise = sum(1 for r in history if r["is_fraud"] != int(r["kind"] == "fraud"))
    fraud_tr = [i for i in tr if history[i]["is_fraud"]]
    legit_tr = [i for i in tr if not history[i]["is_fraud"]]
    mean = lambda idx, j: round(sum(X[i][j] for i in idx) / max(len(idx), 1), 4)
    errors = [{"case_id": p["case_id"], "kind": "false negative" if yA[i] else "false positive",
               "reason_code": p["reason_code"], "risk_score": round(100 * sA[i], 1),
               "features": {f: rows[i][j] for j, f in enumerate(FEATURES) if rows[i][j]}}
              for i, p in enumerate(preds) if (sA[i] >= 0.5) != bool(yA[i])]
    curves = lambda yy, s3: {"A_logistic": {"roc": roc_points(yy[0], s3[0]), "pr": pr_points(yy[0], s3[0])},
                             "A_unsupervised": {"roc": roc_points(yy[0], s3[1]), "pr": pr_points(yy[0], s3[1])},
                             "B_logistic": {"roc": roc_points(yy[1], s3[2]), "pr": pr_points(yy[1], s3[2])}}
    return {
        "data": {"rows": len(history), "seed": SEED, "patterns": dict(pattern.most_common()),
                 "fraud": sum(r["is_fraud"] for r in history), "must_not_autopay": sum(r["must_not_autopay"] for r in history),
                 "label_noise": noise, "sample": [next(r for r in history if r["kind"] == "legit"),
                                                   next(r for r in history if r["kind"] == "fraud")]},
        "test_data": {"rows": len(preds), "fraud": sum(yA), "must_not_autopay": sum(yB),
                      "by_reason": dict(Counter(p["reason_code"] for p in preds).most_common())},
        "split": {"train": {"rows": len(tr), "fraud": sum(history[i]["is_fraud"] for i in tr)},
                  "validation": {"rows": len(va), "fraud": sum(yvA)},
                  "test": {"rows": len(preds), "fraud": sum(yA)}},
        "features": [{"name": f, "train_mean": round(m, 4), "train_std": round(s, 4),
                      "mean_fraud": mean(fraud_tr, j), "mean_legit": mean(legit_tr, j),
                      "nonzero_share": round(sum(1 for i in tr if X[i][j]) / len(tr), 4)}
                     for j, (f, (m, s)) in enumerate(zip(FEATURES, stats_tr))],
        "l2_search": {"A": gridA, "B": gridB},
        "curves": {"validation": curves((yvA, yvB), (svA, svU, svB)), "test": curves((yA, yB), (sA, sU, sB))},
        "confusion": {"validation": [confusion(yvA, svA, t) for t in (0.2, 0.5, 0.8)],
                      "test": [confusion(yA, sA, t) for t in (0.2, 0.5, 0.8)]},
        "histogram": {"validation": histogram(yvA, svA), "test": histogram(yA, sA)},
        "errors_at_0.5": errors,
    }


def _thin(pts, keep):
    if len(pts) <= keep:
        return pts
    step = (len(pts) - 1) / (keep - 1)
    return [pts[round(k * step)] for k in range(keep)]


def _new_iban(rng):
    return make_ch_iban("".join(rng.choice("0123456789") for _ in range(17)))


def _sigmoid(x):
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def _median(xs):
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _solve(A, b):
    """Gaussian elimination with partial pivoting (A is small: features + 1)."""
    n = len(b)
    M = [row[:] + [bi] for row, bi in zip(A, b)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        for r in range(c + 1, n):
            f = M[r][c] / M[c][c]
            for k in range(c, n + 1):
                M[r][k] -= f * M[c][k]
    x = [0.0] * n
    for r in range(n - 1, -1, -1):
        x[r] = (M[r][n] - sum(M[r][k] * x[k] for k in range(r + 1, n))) / M[r][r]
    return x
