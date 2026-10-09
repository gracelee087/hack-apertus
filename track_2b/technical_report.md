# Technical report — Mandate Desk: mandate-bounded payment agents for Swiss fiduciaries

- **Track:** Track 2B — Mandate Desk
- **Event:** Online
- **Team:** hack-1 — Sohee Lee
- **Demo:** `link to video`

## 1. Summary

Around 12,900 Swiss firms do accounting, bookkeeping and tax work (NOGA 6920; secondary source, Valindex 2026-02),
most with 1–9 employees. A daily task is paying supplier invoices for many small clients, each of
which delegated a different scope and limit. Generic AI tools in this market (e.g. document capture in Swiss ERP products)
read invoices; they do not *execute* payments within a provable delegation. We built an agent that
does: Apertus reads multilingual invoices, a harness enforces each client's signed mandate in code,
Apertus reviews the remaining content against a firm manual that improves from human overrules,
and the output is an ISO 20022 pain.001.001.09 payment file per client plus a signed ledger of who
did what for whom. *Who* is provable: the agent has its own key and DID, a firm-signed ID card and
badge, and every request it makes is signed and checked by a gate against the client's signed
mandate before anything takes effect. On 78 synthetic test invoices in four languages, Apertus 8B alone paid
**33 of 45** invoices it should not have paid (CHF 138,781); with the harness, **7 of 45**
(CHF 26,771), 2 of 45 (CHF 9,290) once two learned lessons were promoted to hard checks, and 1 of 45
(CHF 4,640) once a risk score trained on past payments sends high-scoring payments to a human, while
still paying all 33 legitimate invoices. Sixteen kinds of attack on the agent's identity and
authority (forged or replayed requests, revoked agent, tampered mandate, reused human grant, …) were
blocked in 544 of 544 attempts (16 scenarios we wrote, each replayed for the 34 paid invoices; not a
security audit), with 34 of 34 legitimate requests accepted.

## 2. Architecture

```
invoice text ──► [Apertus: extract fields] ──► [Badge: mandate checks in code] ──fail──► reject / escalate
                                                       │pass
                                                       ▼
                                  [Apertus: review vs firm manual + lessons] ──► pay / escalate
                                                       │
           pay ──► pain.001 per client      escalate ──► human queue ──► approve / reject + note
                                                                                   │
                     every step ──► hash-chained ledger        note ──► mask ──► [Apertus: distil rule] ──► manual gen N+1

every agent action = request signed with the agent key ──► [Gate: G1 signature · G2 replay · G3 identity
                                                          · G4 badge · G5 client mandate] ──► executed + logged | refused + logged
```

| Component | File | Runs where |
|---|---|---|
| Synthetic data (mandates, suppliers, invoices, labels) | `src/agent/datagen.py` | build time, deterministic (seed 2026) |
| Apertus client with record/replay cache | `src/agent/llm.py` | runtime; calls one OpenAI-compatible endpoint |
| Badge (hard mandate checks) + client-signed mandate | `src/agent/badge.py`, `credential.py` | runtime, pure Python |
| Decision paths (baseline, harness) | `src/agent/decide.py` | runtime |
| Lesson layer | `src/agent/lessons.py` | runtime |
| Agent identity (Ed25519, did:key, entity card, badge, request stamps) | `src/agent/identity.py` | runtime |
| Gate (five checks) and desk (hire, kill switch, replace, human grants) | `src/agent/gate.py`, `desk.py` | runtime |
| Attack scenarios against the gate | `src/agent/security.py` | runtime, no model calls |
| Risk score: logistic regression on 3,000 synthetic past payments; a score of 50 or more sends a payment to a human (second look) | `src/agent/risk.py` | build time and runtime, pure Python, no new dependency |
| Ledger (hash chain + ledger-key signature), payment files | `src/agent/ledger.py`, `pain001.py` | runtime, pure Python |
| Reviewer web app: client portfolio dashboard (status, risk signals per client), per-client pages (decisions, approvals, agent settings, lessons, files, ledger) | `src/agent/app.py`, `static/index.html` | runtime, FastAPI on port 8000 |

### Target architecture (mandatory)

**a) On-premise**, with **b) air-gapped** operation possible. A fiduciary handles client bank
details, salaries and supplier relationships that it is bound by professional secrecy to protect;
the deployment keeps all of it on the firm's own infrastructure.

- **Runtime dependencies:** one container (Python 3.11, FastAPI, Uvicorn, cryptography) and one
  OpenAI-compatible endpoint serving Apertus 8B (e.g. vLLM on a single GPU in the firm's server
  room or at a Swiss hosting partner). No other network access: no payment provider, no external
  API. Payments leave the system only as a pain.001 file that the firm uploads to the client's bank.
- **Build-time dependencies:** the `python:3.11-slim` base image and three pip packages; Apertus
  weights from Hugging Face. After build, both can be mirrored internally, so the runtime can be air-gapped.
- **During development** we called Apertus through the Public AI inference API (an official
  Hugging Face inference provider) because the organiser's CSCS key was not yet at hand. This is a
  development convenience, not the target deployment: swapping it for CSCS or an on-prem vLLM is a
  change of `LLM_BASE_URL` only, and the recorded responses make the evaluation independent of it.

### Agent identity and verification

The harness answers *what* the agent may do; this layer answers *who* did it, and lets anyone check.
The concept follows the author's earlier "Agentic ID" design (know-your-agent: identity, badge,
stamp, ledger, lessons); the code here is new.

| Principal | Key | Signs |
|---|---|---|
| Client company | its own Ed25519 key (did:key, recorded at onboarding) | the mandate: scope, limits, validity, delegate = the firm |
| Fiduciary firm | firm key | each agent's entity card (display name, duty, operator, the named client manager who assigns its role and manages it, accountable head of payments, *is AI*) and badge (actions, its one client, cap, validity) |
| Agent: one seat per client, one key per generation | its own key → DID | every request: action, client, payload hash, nonce, time |
| Ledger | ledger key | every ledger entry (on top of the hash chain) |

The **gate** runs five checks on each request before it takes effect: G1 signature and payload
hash, G2 nonce never seen and request fresh, G3 firm-issued entity card and agent not revoked,
G4 badge covers this action, client, date and amount (a payment needs a positive, finite amount, a case
and an IBAN; above the badge cap only with a human grant for this client, case, IBAN and amount, and a
grant is used up by the payment it allows), G5 the client's mandate is validly signed, names our firm,
is in force, and the amount is within its single-payment limit unless a human granted this payment
(a higher badge cap set by the firm cannot lift the client's own limit).
Each client has its own agent seat, so one client's agent cannot act for another (G4) and client
data never mixes. The **kill switch** revokes one seat's current generation without touching the
others; **one-click deployment** (also used for replacement) checks the client's mandate, issues the card,
derives the badge from the mandate (cap = its single limit, validity = its period), fixes the manual,
self-tests against the gate (its own request passes, a forged one and a replay are refused; nothing is
executed) and only then puts the agent on duty, stopping at the first failing step; a client that revoked
its mandate stops at step one. **Replacing** an agent hires v2 with a new key and DID, while the seat's lessons carry over: lessons
learned on a client bind that client's seat and are shown to the other seats for reference only;
the lessons from the training review are firm-wide. Human approvals become grants with a reference in the ledger.
A payment decision and its payment file are one step: if the gate refuses the file, the decision is
voided in the ledger and the invoice waits for a human; a file that fails validation is never issued.
For invoices processed in the web app, this month's payments (for the duplicate and monthly-limit
checks) are read from the ledger, and the hard checks run again under a lock just before paying.
The ledger starts at issuance: it opens with the firm and ledger DIDs, then every client mandate
in full with the client's signature, then each agent generation's entity card and badge in full
(`identity.issued`, `delegation.issued`) and its manual fingerprint (`policy.changed`). An auditor
can therefore check from the ledger alone that every executed agent action was taken by an agent
the firm had issued and not yet revoked (`audit_identities`; reference run: 78 of 78).

The design follows control principles referenced in FINMA Guidance 08/2024 and the EU AI Act; this is
not a compliance assessment. FINMA Guidance 08/2024 asks for an inventory, documentation, monitoring and that responsibility stays with people
(entity card names the client manager who manages the agent and the accountable head of payments; their approvals, rejections and kill-switch use are ledger entries under their name); EU AI Act Art. 12 (logging) and Art. 14
(human oversight, ability to stop: queue, grants, kill switch) if the firm serves EU clients.

## 3. Use of Apertus

- **Model:** Apertus v1.5 8B (`swiss-ai/apertus-v1.5-8b` on the endpoint; weights: `swiss-ai/Apertus-v1.5-8B`)
- **How it is used:** inference, agent with structured JSON outputs (no fine-tuning)
- **Where it runs:** Public AI inference API (`https://api.publicai.co/v1`, OpenAI-compatible); temperature 0;
  440 recorded responses (308 for the evaluation run, 130 for the holdout, 2 from trying the live app),
  latency median 3.0 s / p90 7.4 s, cost well under USD 0.10
  (≈ 0.2 M tokens at USD 0.1 / 0.2 per M input / output tokens)
- **Four roles:** (1) field extraction from DE/FR/IT/EN invoices; (2) the *baseline*: the full
  decision in one prompt; (3) content review after the hard checks; (4) distilling masked
  reviewer notes into manual rules. All prompts are in `src/agent/prompts.py`.
- Every response is recorded with its exact request (`data/cache/apertus_responses.jsonl`), so judges
  without a key replay the same run. No other model is used, not even as a judge: the labels come
  from how the synthetic cases were built.

## 4. Data

All data is **synthetic and fictional** (`src/agent/datagen.py`, seed 2026), generated by
templates, not by a model, so labels are exact. No personal data. Licence: CDLA-Permissive-2.0.

- 5 client mandates (one revoked), 19 known suppliers, 120 invoices: DE 36 / FR 34 / EN 26 / IT 24.
- Two layouts per language, Swiss number formats (`CHF 1'234.50`, `1 234,50 CHF`), VAT 8.1 % or exempt.
- One planted fault per non-payable invoice. 12 case types: `OK` (pay, 50); reject: `R_REVOKED` 6,
  `R_SCOPE` 8, `R_IBAN_INVALID` 6, `R_DUPLICATE` 6; escalate: `E_LIMIT_SINGLE` 8, `E_LIMIT_MONTH` 6,
  `E_IBAN_CHANGED` 8, `E_NEW_VENDOR` 6, `E_PRESSURE` 6, `E_VAT_MISMATCH` 6, `E_WRONG_ADDRESSEE` 4.
- Stratified split: 42 train (used only to collect reviewer notes for lessons), 78 test.
- Each case carries the note a human reviewer would write when overruling the agent (with names,
  amounts and IBANs, which the lesson layer must mask).

## 5. Evaluation

**Task:** decide pay / escalate / reject for each test invoice. **Primary metric:** wrong-payment
rate, the share of non-payable invoices that the system paid on its own. Also: wrongly paid CHF,
missed-payment rate (payable invoices held back), accuracy, and for systems that build the
payment file, paid invoices whose file carries a wrong IBAN or amount. Model failures (unreachable
endpoint, unparsable reply) fail closed (escalate) and are counted.

**Baseline:** Apertus alone, given the *same* information and the *same* written policy and manual
(mandate, supplier list, this month's payments, invoice). This isolates what the harness adds.

Test split: 78 invoices, 33 payable, 45 not payable (28 escalate, 17 reject).

| Setup | Accuracy | Wrong-payment rate (n / 45) | Wrongly paid CHF | Missed-payment rate |
|---|---|---|---|---|
| Baseline: Apertus alone | 0.513 | 0.733 (33) | 138,781 | 0.030 |
| Apertus extraction + badge | 0.872 | 0.222 (10) | 34,521 | 0.000 |
| + review, manual gen 0 | 0.872 | 0.222 (10) | 34,521 | 0.000 |
| **+ review, manual gen 1 (lessons)** | **0.910** | **0.156 (7)** | **26,771** | **0.000** |
| + two lessons promoted to hard checks ✱ | 0.974 | 0.044 (2) | 9,290 | 0.000 |
| **+ risk-score second look (score ≥ 50 → human) ✱** | **0.987** | **0.022 (1)** | **4,640** | **0.000** |

✱ Added *after* we saw the test results above, so it is not a blind result. The two checks
(net + VAT = total; invoice addressed to the client) are exactly the gen-1 lessons that the
8B reviewer failed to apply; we wrote them as code, as a human would after signing off a lesson.
They caught 3 of 4 sum mismatches and 2 of 2 wrong-addressee invoices, and held back no legitimate
invoice. The remaining sum mismatch is the invoice whose total the model "corrected" during
extraction; the other remaining miss is one fraud-pressure invoice the reviewer judged routine.
The second look (last row) was added after seeing these results and after the holdout below: the risk
score gives that pressure invoice 99.9, so it goes to a human; no legitimate invoice scored 50 or more, on
the test set or on the holdout. Its gain on the test set is therefore not a blind result. The last row is the configuration the reviewer app runs and whose ledger and payment files
are in `results/`.

Correct-decision rate by case type (baseline → harness gen 1): every hard-rule type (revoked,
out-of-scope, invalid IBAN, duplicate, limits, changed IBAN, new supplier) goes from 0.00–0.40
(revoked: 1.00) to **1.00**; fraud-pressure 0.25 → 0.75; VAT mismatch 0.00 → 0.00; wrong addressee 0.00 → 0.00;
legitimate invoices 0.97 → 1.00. Full table: `results/summary.md`.

**Extraction** (Apertus, 78 invoices, 4 languages): IBAN 1.000, invoice no. 1.000, supplier 1.000,
total 0.987, addressee 0.936, valid JSON 1.000. One paid invoice (of 40 paid by the harness) carries a wrong
amount: on a VAT-mismatch invoice the model reported net + VAT instead of the stated total, silently
"correcting" the very inconsistency it should have flagged.

**What we learned.**
1. *Apertus alone fails open.* Given the full policy, it still paid 33 of 45 faulty invoices. Its
   reasons show why: it asserts facts it did not check ("known supplier (Casino Lounge Ceresio SA)",
   "IBAN matches on file" for an IBAN with a broken checksum) and never sums this month's payments.
   It is careful with legitimate invoices (missed only 1 of 33), so the problem is not caution but verification.
2. *Rules belong in code, reading belongs to the model.* Moving the mandate checks into the badge
   removes every error the rules can see, with zero missed payments. Apertus' part, reading four
   languages and Swiss number formats, was near perfect.
3. *The manual alone did nothing; lessons helped where the signal is in the words.* Manual gen 0
   already says "urgency, threats, secrecy → escalate", yet the reviewer paid all 4 pressure
   invoices. After 7 overruled training cases were turned into rules, it caught 3 of 4. It did not
   catch amounts that don't add up or the wrong addressee, even with an explicit rule: an 8B model
   asserts "correct totals" without doing the arithmetic. Promoting those two lessons to code
   (last row) closes most of the gap: soft control finds the rule, hard control enforces it.

**Agent verification.** Each attack below was replayed once for each of the 34 invoices the
production configuration paid, each time against a freshly hired agent (`src/agent/security.py`,
no model calls). The 544 attempts are 16 scenarios × 34 invoices: they show the gate stops the attacks
we wrote, not that the system is secure against attacks we did not think of:

| Scenario | Blocked | Caught by |
|---|---|---|
| legitimate request (control) | 0 / 34 (34 accepted) | — |
| forged signature in the agent's name | 34 / 34 | G1 |
| amount / IBAN changed after signing | 34 / 34 | G1 |
| replayed request · stale request (1 h) | 34 / 34 · 34 / 34 | G2 |
| unknown agent key · revoked agent v1 after kill switch | 34 / 34 · 34 / 34 | G3 |
| agent of one client acting for another · above cap without grant · expired badge | 34 / 34 each | G4 |
| human grant used a second time · human grant of another client · negative, zero or NaN amount | 34 / 34 each | G4 |
| mandate limits tampered · mandate naming another delegate · mandate revoked | 34 / 34 each | G5 |
| amount above the client's mandate limit, badge cap set too high | 34 / 34 | G5 |

In the reference run the gate accepted all 78 agent decisions for active clients' invoices plus the
4 payment files, and refused the 4 decisions for the client whose mandate was already revoked
(no agent is hired for a client that has revoked its mandate). The ledger's chain and signatures verify.

**Risk score (data-science layer).** Every invoice gets a fraud probability. It changes one thing: an
invoice the harness would pay goes to a human when the score is 50 or more (second look); rejections and
holds are never undone by it. The reviewer sees the score next to each held invoice; the queue itself is
ordered by fraud signal, then days to spare before the due date, then amount. Training data: 3,000 synthetic past payments (`data/history/`,
seed 7; 75% legitimate, the rest control cases and eight fraud patterns, 15% of fraud made subtle,
2% of labels flipped), generated independently of the 120 invoices except that each supplier's usual
amount is taken from the train split's legitimate invoices. Thirteen features (amount z-score against
that supplier's history, changed or invalid IBAN, new supplier, round amount, amounts that don't add up,
wrong addressee, urgency words in four languages, short due date, duplicate, limits, scope, month load).
L2-regularised logistic regression fitted by Newton/IRLS in pure Python; the L2 weight (0.01–10) is
chosen on a 20% validation split, then refitted on all 3,000 rows. Compared with an unsupervised baseline
(sum of robust deviations from the median) and with a second model for "must not be paid automatically".

| Model | ROC AUC validation | ROC AUC test (78) | Avg precision test | ROC AUC holdout (42) |
|---|---|---|---|---|
| fraud, logistic regression | 0.908 | 0.970 | 0.973 | 0.984 |
| fraud, unsupervised baseline | 0.860 | 0.878 | 0.770 | 0.838 |
| must not autopay, logistic regression | 0.996 | 0.962 | 0.980 | 0.984 |

On the 43 test invoices the rules let through, fraud ROC AUC is 0.952; the pressure-fraud invoice that
the rules and Apertus missed (T110) is scored 2nd highest of them, the amounts-don't-add-up invoice
(T111) is not caught because Apertus read totals that add up (an extraction limit shared with the rules).
The first version raised 4 false alarms on the test set because supplier amounts in the history were
random; anchoring them on train-split invoices removed them. That fix was made after seeing test
results, which is one reason for the holdout below. Every step (data, features, split, L2 search, ROC
and PR curves, confusion matrices, errors) is shown on the app's risk-model page.

**Fresh holdout.** Two parts of the design were changed after seeing test results: the two promoted
lessons and one fix in the risk score's amount feature. To check that the numbers
are not an artefact of that, we froze that design and drew a second test set once: 42 new invoices
from the same generator with another seed (2027; same case types in the same proportions, no shared
invoice text), run through all five systems with the lessons of the train split and the risk model exactly
as fitted (`python -m agent holdout`, 130 Apertus calls recorded live on 2026-10-09). The second look was
added after this run; its row below is a replay of the same recorded responses, and it changes no decision
on the holdout, so the holdout is blind evidence for the design up to the promoted lessons only:

| System | Accuracy | Wrong payments (of 25) | Wrongly paid CHF | Missed payments (of 17) |
|---|---|---|---|---|
| Apertus alone | 0.571 | 14 | 42,075.89 | 0 |
| extraction + badge | 0.857 | 6 | 10,885.89 | 0 |
| harness, manual gen 0 | 0.857 | 6 | 10,885.89 | 0 |
| harness, manual gen 1 | 0.905 | 4 | 7,215.89 | 0 |
| harness, gen 1 + promoted lessons | 1.000 | 0 | 0.00 | 0 |
| + risk-score second look (production) | 1.000 | 0 | 0.00 | 0 |

The frozen risk score: fraud ROC AUC 0.984 (unsupervised baseline 0.838), must-not-autopay 0.984; at 0.5,
10 of 13 fraud cases flagged with 1 false alarm. Field extraction was correct for all 42 invoices. The
holdout comes from the same generator, so it shows we did not tune on the test set; it does not show that
the system handles invoice types it has never seen, and with 42 cases (2 per rare type) a single case
moves the rates a lot.

Sanity check of the labels and rules: with gold fields instead of model extraction, the badge decides
all 54 hard-rule cases exactly as labelled and passes all other cases (`tests/test_core.py`).

## 6. Limitations

- Synthetic invoices are cleaner than scanned PDFs; we feed text, no OCR. Real invoices (QR-bill,
  scans) would need a document-reading step, and extraction accuracy would drop.
- 78 test cases: differences of one or two cases are noise. No confidence intervals.
- Identity uses real Ed25519 signatures and did:key, but the documents are a simple JSON format,
  not W3C Verifiable Credentials or swiyu SD-JWT VCs (swiyu planned launch 2026-12-01). Demo keys are
  derived from fixed seeds for reproducibility; in production the firm and ledger keys belong in an
  HSM, the agent key only in memory. Client DIDs are "registered at onboarding" by a demo rule.
  The ledger is signed but not anchored externally. The attack scenarios are ones we designed; they
  show the checks work, not that no other attack exists.
- pain.001 files are checked structurally (counts, control sum, IBAN checksums, positive amounts). An
  external review validated the four reference files against the official SIX pain.001.001.09 schema;
  the banks' own acceptance rules were not tested.
- The reviewer app is a demo without authentication or roles: anyone who can reach it can approve,
  change agent settings or use the kill switch. The ledger records who acted by role (the client manager),
  not by a verified login. The monthly limit is checked by the badge at decision time, not again by the gate.
- Lessons are learned from 42 training cases with simulated reviewer notes; the case types in
  train and test are the same, so this shows the mechanism, not generalisation to new fraud patterns.
- The baseline sees the whole policy in one prompt; a stronger prompted baseline (e.g. multi-step)
  could narrow the gap.

## 7. Reproducibility

`make run` from `track_2b/` (or the repo root) on a clean checkout: builds the image, replays the
recorded Apertus responses (no key needed), writes `results/`, prints the table above and serves the
reviewer app on port 8000. `make eval` prints and exits. `python -m agent holdout` (after `eval`) replays
the holdout. GitHub Actions runs the tests, builds the image and re-runs `eval` and `holdout` without a key
on every push; it fails if `metrics.json`, `risk_model.json` or `holdout.json` differ from the committed files. Data generation is deterministic (seed 2026);
Apertus was called with temperature 0. Hardware: any machine with Docker; no GPU needed for replay.
Commit: ⟨pending⟩.

## 8. Next steps

Mandates as swiyu verifiable credentials and keys in an HSM; QR-bill and PDF input; a sign-off workflow in the
app for promoting lessons to hard checks (done by hand here); a larger test set drawn once after freezing the final configuration (1,000 new invoices, with
other invoice layouts, combined errors and unreadable fields), to measure the promoted checks and the
second look blind; pilot with one Treuhand office on historical, anonymised
payment runs; integration as a layer on top of existing Swiss ERP/accounting software rather than a replacement.

## License

Creative Commons Attribution 4.0 (CC-BY-4.0). All HackApertus projects are open-sourced.

## References

- Apertus model family: https://huggingface.co/swiss-ai
- ISO 20022 pain.001.001.09 (customer credit transfer initiation)
- FINMA Guidance 08/2024 on governance and risk management when using AI
- Valindex (2026-02), count of Swiss firms in NOGA 6920 — secondary source
