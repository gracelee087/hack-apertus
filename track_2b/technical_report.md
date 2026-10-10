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
(CHF 26,771), and 0 of 45 after later additions (two lessons promoted to hard checks, verification that
what Apertus read is written in the invoice, a risk-score second look), still paying all 33 legitimate
invoices. Because those additions came after seeing results, we tested on fresh, wider synthetic sets, each
run once (four layouts, two problems at once, unreadable fields). On 1,000 invoices the then design decided
to pay 29 of 600 it should not have (4.8%), all invoices with no invoice number, which Apertus had filled in;
after we made Apertus quote where it read each value (checked in code), a new set of 500 had **0 of 300
(95% CI 0–1.3%)** against 178 for Apertus alone, with 3.5% of legitimate invoices held for a human. The gain
comes from splitting the work: code makes the decision wherever a rule can be written, Apertus reads and
judges what no rule can, and what Apertus read is checked against its own quotes from the invoice. Sixteen kinds of attack on the agent's identity and authority (forged or replayed requests,
revoked agent, tampered mandate, reused human grant, …) were blocked in 528 of 528 attempts (16 scenarios
we wrote, each replayed for the 33 paid invoices; not a security audit), with 33 of 33 legitimate requests
accepted.

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
| Verification: IBAN and total written in the invoice; Apertus quotes where it read IBAN, invoice number and total (checked in code); same supplier + amount = possible duplicate | `src/agent/badge.py`, `decide.py` | runtime |
| Risk score: logistic regression on 3,000 synthetic past payments; a score of 95 or more sends a payment to a human (second look) | `src/agent/risk.py` | build time and runtime, pure Python, no new dependency |
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
A payment decision and its payment file are one step: the file is written before it is recorded; if
it fails validation, cannot be written or the gate refuses it, the decision is voided in the ledger and
the invoice waits for a human (in the reference run as in the web app). Extraction must be complete and
typed: without supplier, IBAN, a positive numeric total, invoice number, net amount and VAT the invoice
goes to a human, since the duplicate and sum checks cannot run otherwise.
For invoices processed in the web app, this month's payments (for the duplicate and monthly-limit
checks) are read from the ledger, and the hard checks run again under a lock just before paying; a human
approval is checked for duplicates too. The demo runs on one business date: the synthetic world's date
(2026-10-09), advancing with real time from start; badge, gate and month totals all use it.
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
  (≈ 0.2 M tokens at USD 0.1 / 0.2 per M input / output tokens); the reference cache now holds 501 responses
  including 56 citation calls; plus 7,158 for the 2,000-invoice study (`data/cache/study_responses.jsonl`,
  about USD 0.50)
- **Five roles:** (1) field extraction from DE/FR/IT/EN invoices; (2) the *baseline*: the full
  decision in one prompt; (3) content review after the hard checks; (4) for payment candidates, quoting
  where it read the IBAN, invoice number and total, which code then checks; (5) distilling masked
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
rate, the share of non-payable invoices that the system decided to pay on its own, without a human. "Paid"
here means that decision: no money moves in any evaluation; in operation it would put the invoice into the
pain.001 file handed to the bank, and the system never sends payments itself. Also: wrongly paid CHF,
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
| + verification (written in the invoice, quoted, possible duplicate) ✱ | 0.987 | 0.022 (1) | 4,650 | 0.000 |
| **+ risk-score second look (score ≥ 95 → human) ✱** | **1.000** | **0.000 (0)** | **0** | **0.000** |

✱ Added *after* we saw the test results above, so it is not a blind result. The two checks
(net + VAT = total; invoice addressed to the client) are exactly the gen-1 lessons that the
8B reviewer failed to apply; we wrote them as code, as a human would after signing off a lesson.
They caught 3 of 4 sum mismatches and 2 of 2 wrong-addressee invoices, and held back no legitimate
invoice. The remaining sum mismatch (T111) is the invoice whose total the model "corrected" during
extraction; verification, added during the study (below), holds it because that total is not written
on the invoice. The other remaining miss (T110) is a fraud-pressure invoice the reviewer judged
routine; the risk score gives it 99.9, so the second look sends it to a human. Neither addition held a
legitimate invoice on the test set or the holdout. None of the three late additions is a blind result here;
the 1,000-invoice study below is. The last row is the configuration the reviewer app runs and whose ledger
and payment files are in `results/`.

Correct-decision rate by case type (baseline → harness gen 1): every hard-rule type (revoked,
out-of-scope, invalid IBAN, duplicate, limits, changed IBAN, new supplier) goes from 0.00–0.40
(revoked: 1.00) to **1.00**; fraud-pressure 0.25 → 0.75; VAT mismatch 0.00 → 0.00; wrong addressee 0.00 → 0.00;
legitimate invoices 0.97 → 1.00. Full table: `results/summary.md`.

**Extraction** (Apertus, 78 invoices, 4 languages): IBAN 1.000, invoice no. 1.000, supplier 1.000,
total 0.987, addressee 0.936, valid JSON 1.000. Before verification one paid invoice carried a wrong
amount: on a VAT-mismatch invoice the model reported net + VAT instead of the stated total, silently
"correcting" the very inconsistency it should have flagged. Verification now holds it.

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

**Agent verification.** Each attack below was replayed once for each of the 33 invoices the
production configuration paid, each time against a freshly hired agent (`src/agent/security.py`,
no model calls). The 528 attempts are 16 scenarios × 33 invoices: they show the gate stops the attacks
we wrote, not that the system is secure against attacks we did not think of:

| Scenario | Blocked | Caught by |
|---|---|---|
| legitimate request (control) | 0 / 33 (33 accepted) | — |
| forged signature in the agent's name | 33 / 33 | G1 |
| amount / IBAN changed after signing | 33 / 33 | G1 |
| replayed request · stale request (1 h) | 33 / 33 · 33 / 33 | G2 |
| unknown agent key · revoked agent v1 after kill switch | 33 / 33 · 33 / 33 | G3 |
| agent of one client acting for another · above cap without grant · expired badge | 33 / 33 each | G4 |
| human grant used a second time · human grant of another client · negative, zero or NaN amount | 33 / 33 each | G4 |
| mandate limits tampered · mandate naming another delegate · mandate revoked | 33 / 33 each | G5 |
| amount above the client's mandate limit, badge cap set too high | 33 / 33 | G5 |

In the reference run the gate accepted all 78 agent decisions for active clients' invoices plus the
4 payment files, and refused the 4 decisions for the client whose mandate was already revoked
(no agent is hired for a client that has revoked its mandate). The ledger's chain and signatures verify.

**Risk score (data-science layer).** Every invoice gets a fraud probability. It changes one thing: an
invoice the harness would pay goes to a human when the score is 95 or more (second look; threshold chosen
on the study's tune set by a fixed rule, below); rejections and
holds are never undone by it. The reviewer sees the score next to each held invoice; the queue itself is
ordered by fraud signal, then days to spare before the due date, then amount. Training data: 3,000 synthetic past payments (`data/history/`,
seed 7; 75% legitimate, the rest control cases and eight fraud patterns, 15% of fraud made subtle,
2% of labels flipped), generated independently of the 120 invoices except that each supplier's usual
amount is taken from the train split's legitimate invoices. Thirteen features (amount z-score against
that supplier's history, changed or invalid IBAN, new supplier, round amount, amounts that don't add up,
wrong addressee, urgency words in four languages, short due date, duplicate, limits, scope, month load).
L2-regularised logistic regression fitted by Newton/IRLS in pure Python; the L2 weight (0.01–10) is
chosen on a 20% validation split (the amount baseline for that choice is computed from the 80% training split
only), then refitted on all 3,000 rows. Compared with an unsupervised baseline
(sum of robust deviations from the median) and with a second model for "must not be paid automatically".

| Model | ROC AUC validation | ROC AUC test (78) | Avg precision test | ROC AUC holdout (42) |
|---|---|---|---|---|
| fraud, logistic regression | 0.908 | 0.970 | 0.973 | 0.984 |
| fraud, unsupervised baseline | 0.860 | 0.878 | 0.770 | 0.838 |
| must not autopay, logistic regression | 0.996 | 0.962 | 0.980 | 0.984 |

On the 43 test invoices the rules let through, fraud ROC AUC is 0.952; the pressure-fraud invoice that
the rules and Apertus missed (T110) is scored 2nd highest of them, the amounts-don't-add-up invoice
(T111) is not caught because Apertus read totals that add up (verification holds it instead).
The first version raised 4 false alarms on the test set because supplier amounts in the history were
random; anchoring them on train-split invoices removed them. That fix was made after seeing test
results, which is one reason for the holdout below. Every step (data, features, split, L2 search, ROC
and PR curves, confusion matrices, errors) is shown on the app's risk-model page.

**Fresh holdout.** Two parts of the design were changed after seeing test results: the two promoted
lessons and one fix in the risk score's amount feature. To check that the numbers
are not an artefact of that, we froze that design and drew a second test set once: 42 new invoices
from the same generator with another seed (2027; same case types in the same proportions, no shared
invoice text), run through all five systems with the lessons of the train split and the risk model exactly
as fitted (`python -m agent holdout`, 130 Apertus calls recorded live on 2026-10-09). Verification
and the second look were added after this run; their rows below replay the same recorded responses and
change no decision on the holdout, so the holdout is blind evidence for the design up to the promoted
lessons only:

| System | Accuracy | Wrong payments (of 25) | Wrongly paid CHF | Missed payments (of 17) |
|---|---|---|---|---|
| Apertus alone | 0.571 | 14 | 42,075.89 | 0 |
| extraction + badge | 0.857 | 6 | 10,885.89 | 0 |
| harness, manual gen 0 | 0.857 | 6 | 10,885.89 | 0 |
| harness, manual gen 1 | 0.905 | 4 | 7,215.89 | 0 |
| harness, gen 1 + promoted lessons | 1.000 | 0 | 0.00 | 0 |
| + verification | 1.000 | 0 | 0.00 | 0 |
| + risk-score second look at 95 (production) | 1.000 | 0 | 0.00 | 0 |

The frozen risk score: fraud ROC AUC 0.984 (unsupervised baseline 0.838), must-not-autopay 0.984; at 0.5,
10 of 13 fraud cases flagged with 1 false alarm. Field extraction was correct for all 42 invoices. The
holdout comes from the same generator, so it shows we did not tune on the test set; it does not show that
the system handles invoice types it has never seen, and with 42 cases (2 per rare type) a single case
moves the rates a lot.

**Study: three fresh sets, each run once.** The 78 test invoices are narrow, so we drew wider sets from
the generator with their own seeds (`python -m agent study`, `data/study_*.jsonl`): four layouts (two before
plus an e-mail and a Swiss QR-bill payment part), the eleven earlier case types, four combinations of two
problems on one invoice (e.g. changed IBAN + pressure) and three unreadable-field types (no invoice number,
no IBAN in the text, total illegible); per 100 invoices 40 payable, 45 escalate, 15 reject. Each step was
committed before the set it was tested on was run, and each set's results were looked at once:

1. **Tune (500, seed 3031)** chooses the second-look threshold, by a rule committed before it ran: among
   0, 5, …, 100, the fewest wrong payments while holding at most 2% more legitimate invoices, ties to the
   higher threshold. It also showed Apertus filling gaps (16 of 20 illegible totals computed from net + VAT
   and paid), so we added the first verification check: the IBAN and total read must be written in the invoice.
2. **Final (1,000, seed 3032)**, blind for that design: Apertus alone decided to pay 337 of 600 invoices it
   should not have (CHF 1,150,174); the production harness **29 (4.8%, 95% CI 3.4–6.9%; CHF 70,500)**, every
   one an invoice *without an invoice number*: Apertus filled the field with the payment reference (12) or
   the heading word "RECHNUNG"/"INVOICE" (16), so the completeness check saw a number. Every other
   non-payable type, combined cases included, was never paid. Legitimate invoices held: 5.2%.
3. We then made verification general instead of adding a label list: for every payment candidate Apertus
   **quotes the exact words where it read the IBAN, invoice number and total** (and the payment reference),
   and code checks that each quote is in the invoice and contains the value, that the invoice number has a
   digit and is not the reference (`badge.cited`); and the same supplier with the same amount already paid
   this month counts as a possible duplicate. Re-scored from the recordings, tune and final drop to 0 wrong
   payments (not blind), and the threshold rule now picks **95** (every threshold from 15 up ties at 0).
4. **Confirm (500, seed 3033)**, blind for the final design (generated and committed before; its results
   were never scored before this run). A first run with the earlier design was stopped at invoice 264, before
   any scoring, when we decided on step 3 from the final set's failures; the responses it recorded (Apertus
   alone, extraction, review) were reused, so the cache holds confirm calls from two sessions:

| System | What changed | Who decides | Accuracy | Wrong payments (of 300) | Wrongly paid CHF | Missed payments (of 200) |
|---|---|---|---|---|---|---|
| Apertus alone | all rules in the prompt, Apertus decides | Apertus | 0.576 | 178 (59.3%) | 642,157 | 1.0% |
| extraction + badge | Apertus only reads the fields; mandate, limits and account are checked in code | code | 0.760 | 89 | 226,007 | 2.5% |
| harness, manual gen 0 | Apertus reviews the content of invoices that passed the code checks | code, then Apertus | 0.762 | 88 | 225,217 | 2.5% |
| harness, manual gen 1 | Apertus writes manual rules from invoices a human corrected | rules by Apertus, applied by Apertus | 0.804 | 67 | 172,827 | 2.5% |
| + promoted lessons | two of those rules (VAT mismatch, wrong addressee) move into code | rules by Apertus, applied by code | 0.876 | 31 | 67,960 | 2.5% |
| + verification (written, quoted, duplicate) | Apertus quotes where it read IBAN, invoice number and total; code checks the quotes | Apertus and code together | 0.986 | 0 | 0 | 3.5% |
| **+ second look at 95 (production)** | a high risk score sends a payment to a human | statistical model | **0.986** | **0 (95% CI 0–1.3%)** | **0** | **3.5% (CI 1.7–7.1%)** |

What it shows. (1) Each layer lowers the wrong payments, from 178 to 0, on invoices the design never saw.
(2) The 7 legitimate invoices held are reading errors caught before money moves: 5 IBANs Apertus misread
(typically dropping the lone last digit of `… 7658 7`), 2 quotes that did not match; most in the one-line
layout (7.8% held) and none in the e-mail layout. No paid invoice carried a wrong IBAN or amount (5 did
with the promoted lessons alone). (3) The second look adds nothing here; verification does the work. The
honest reading is that the final set measured the design before the invoice-number gap was found (4.8%)
and the confirm set measures it after (0 of 300); both are synthetic and from one generator family.

**Who stopped what (confirm set, production design).** Each non-payable invoice is counted at the first
layer that held it. Code makes every decision except one; Apertus reads every invoice, and the code
checks act on what it read.

| Layer | Who | Non-payable held (of 300) | Legitimate held (of 200) | What it handles |
|---|---|---|---|---|
| Hard checks (`badge.check`) | code, on fields Apertus extracted | 185 (62%) | 0 | revoked or expired mandate, scope, single/monthly limit, IBAN checksum, IBAN differs from the registered one, new supplier, paid invoice number, missing field |
| Promoted lessons (`badge.promoted_checks`) | code; the rules were written by Apertus from reviewer notes | 39 (13%) | 0 | VAT does not add up, invoice not addressed to the client |
| Verification (`badge.grounding`, `badge.cited`) | Apertus quotes, code checks the quotes | 56 (19%) | 7 | no invoice number, total illegible, value not in the invoice, same supplier and amount already paid |
| Content review (`decide.review`) | Apertus decides (pay or human only) | 20 (7%) | 0 | fraud signals such as pressure wording, against the manual and lessons |
| Second look (risk score ≥ 95) | logistic regression | 0 | 0 | unusual payment patterns (1 of 600 on the final set) |

Apertus alone, given the same policy, manual, mandate and supplier list in one prompt, paid 178 of these
300. Splitting the work moved that to 0: Apertus reads, quotes and judges content, and code holds the
decision wherever a rule can be written. The final set gives the same split (63 / 12 / 18 / 6%).
In total the study recorded 7,158 Apertus responses (`data/cache/study_responses.jsonl`, about USD 0.50).

Sanity check of the labels and rules: with gold fields instead of model extraction, the badge decides
all 54 hard-rule cases exactly as labelled and passes all other cases (`tests/test_core.py`).

## 6. Limitations

- Synthetic invoices are cleaner than scanned PDFs; we feed text, no OCR. Real invoices (QR-bill,
  scans) would need a document-reading step, and extraction accuracy would drop.
- 78 test cases are too few for confidence intervals; the study sets have them (Wilson 95%), but all sets
  come from one generator family, as text. Real invoices vary far more; the 0 of 300 is evidence for the
  mechanism, not a rate to expect in production. The verification checks were each added after a set
  showed the gap they close; only the confirm set tests the final design blind.
- The 8B model misreads some IBANs (about 6% of extractions in the study); verification holds those
  invoices rather than paying, which costs reviewer time (3.5–4.3% of legitimate invoices held).
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
the holdout. `LLM_CACHE_PATH=data/cache/study_responses.jsonl python -m agent study tune` replays the study's
tune set (`STUDY_FORCE=1 … study final|confirm` the others) into `results/study/`; the final set's blind
results before the last change are kept in `results/study/final_blind.json`. GitHub Actions runs the tests, builds the image and re-runs `eval` and `holdout` without a key
on every push; it fails if `metrics.json`, `risk_model.json` or `holdout.json` differ from the committed files. Data generation is deterministic (seed 2026);
Apertus was called with temperature 0. Hardware: any machine with Docker; no GPU needed for replay.
Commit: ⟨pending⟩.

## 8. Next steps

Mandates as swiyu verifiable credentials and keys in an HSM; QR-bill and PDF input; a sign-off workflow in the
app for promoting lessons to hard checks (done by hand here); structured inputs where they exist
(QR-bill data, e-invoices) so the model reads less; supplier profiles and per-field confidence to cut the
held legitimate invoices; pilot with one Treuhand office on historical, anonymised
payment runs; integration as a layer on top of existing Swiss ERP/accounting software rather than a replacement.

## License

Creative Commons Attribution 4.0 (CC-BY-4.0). All HackApertus projects are open-sourced.

## References

- Apertus model family: https://huggingface.co/swiss-ai
- ISO 20022 pain.001.001.09 (customer credit transfer initiation)
- FINMA Guidance 08/2024 on governance and risk management when using AI
- Valindex (2026-02), count of Swiss firms in NOGA 6920 — secondary source
