# Mandate-bounded payment agent for Swiss fiduciaries (working title)

Swiss *Treuhand* firms pay supplier invoices on behalf of many small client companies.
This prototype lets **Apertus** do that work — read invoices in German, French, Italian and
English — while a **harness** keeps it inside what each client actually delegated:

1. **Apertus extracts** supplier, IBAN, amounts, invoice number and addressee from the invoice text.
2. **Badge (hard checks, in code)** enforces the client's signed mandate: revoked or expired
   mandate, out-of-scope expense, invalid or changed IBAN, unknown supplier, duplicate,
   single-payment and monthly limits. The model cannot talk its way past these.
3. **Apertus reviews** the invoices that pass, against the firm manual (fraud signals, amounts
   that don't add up, invoices addressed to someone else).
4. **Lessons**: when a human overrules the agent, the note is masked, Apertus distils it into a
   rule, and the next manual generation is fingerprinted in the ledger. No weights change.
5. **Agent identity and gate**: each client gets its own agent seat; every agent generation has its
   own Ed25519 key and DID, a firm-signed ID card (display name, duty, and the named client manager who assigns its role and manages it) and a badge for that one client; clients sign their mandates. Every agent action is a signed, single-use request that
   a gate checks (signature, replay, identity, badge, mandate) before it takes effect. Kill switch,
   replacement by the next generation, and human grants for amounts above the badge cap.
6. **Outputs**: one ISO 20022 `pain.001.001.09` file per client for its bank, an approval queue
   for everything the agent may not pay alone, and a hash-chained, signed audit ledger.

The reviewer app opens on a client portfolio dashboard (status and risk signals per client) with a
page per client (decisions, approvals, agent settings, lessons, payment files, ledger).

Results (78 synthetic test invoices, Apertus v1.5 8B): Apertus alone paid 33 of 45 invoices it
must not pay (CHF 138,781); with the harness 7 (CHF 26,771), and 2 (CHF 9,290) with two lessons
promoted to hard checks, with no legitimate payment missed. Twelve attack types on the agent's
identity and authority: 420 / 420 blocked, 35 / 35 legitimate requests accepted.
A risk score (logistic regression trained on 3,000 synthetic past payments, pure Python) ranks the
approval queue without changing any decision: ROC AUC 0.954 for fraud on the 78 test invoices
(unsupervised baseline 0.832), and it ranks 3rd the pressure-fraud invoice the rules and Apertus missed.
See `results/summary.md` and `technical_report.md`.

## Run

```bash
make run          # builds the image, runs the evaluation, serves the reviewer app on http://localhost:8000
make eval         # evaluation only: prints the results table and exits
```

No API key is needed: without `LLM_API_KEY` the run **replays the recorded Apertus responses**
in `data/cache/apertus_responses.jsonl` and reproduces our numbers. To call Apertus live, copy
`.env.example` to `.env` and set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_NAME` (any
OpenAI-compatible endpoint serving Apertus, e.g. CSCS or your own vLLM).

Without Docker: `pip install -r requirements.txt && PYTHONPATH=src python -m agent all`.
Tests: `pip install pytest && python -m pytest tests`.

| Path | Content |
|---|---|
| `src/agent/` | datagen, llm client (record/replay), badge, decide, lessons, risk (score model), identity, gate, desk, security, ledger, pain001, evaluate, web app |
| `data/` | synthetic mandates, suppliers, 120 invoices (labels + reviewer notes), recorded Apertus responses, 3,000 past payments for the risk model (`history/`) |
| `results/` | metrics, predictions, payment files, ledger from the reference run |
| `tests/` | offline tests for the deterministic parts |

All data is synthetic and fictional. Code: Apache-2.0. Docs: CC-BY-4.0. Data: CDLA-Permissive-2.0.

---

# Track 2 B: Own Project

Bring your own idea and build a working Apertus prototype that tackles a problem you care about — any domain, any use case. The project must be new, started within the hackathon period.

Submissions must use the Apertus model family.
For Track 2 this means that submitted solutions must be built with Apertus. Other open-weights models can be used to support development, e.g. as automatic judges during evaluation. Their role must be clearly described in the submission report.

💬 In case you have questions, join the conversation on [Discord](https://discord.gg/hack-apertus) or send an email to “hello@hackapertus.ch”

---

## 🔧 Resources & Tools

Check our resources & tools page for detailed information:
https://hackapertus.notion.site/resources-tools

| Models           | URL                                      |
|------------------|------------------------------------------|
| Apertus v1.5 8B  | [huggingface.co/swiss-ai/Apertus-v1.5-8B](https://huggingface.co/swiss-ai/Apertus-v1.5-8B)  |
| Apertus v1.5 70B | [huggingface.co/swiss-ai/Apertus-v1.5-70B](https://huggingface.co/swiss-ai/Apertus-v1.5-70B) |


## Target architecture (mandatory)

Whatever you build in Track 2B must be deployable in one of these three architectures:

- **a) On-premise** — on the organisation's own infrastructure, under its own administration.
- **b) Air-gapped** — with no external network connection at runtime.
- **c) Sovereign Swiss cloud** — on a cloud platform operated in Switzerland, under Swiss jurisdiction, with Swiss data residency.

---

## Data

The `data/` directory must not exceed 100 MB.

---

## 📦 Submission Requirements & Deliverables

❗️ Submissions are not handled on Devpost but via this URL only:
http://hackapertus.ch/online-hack/submissions

The submission must: 
1. follow the template repo and include all prerequisite files and definitions
2. follow the specified input/output formats
3. run in a Docker container, launched with `make run` from the root of the project
4. run end-to-end when judges try to run it

### Git repo (URL)
- Create your repo from this template (**Use this template**) and work in the `track_2b/` challenge directory. Delete the other track challenge directories.
- Keep `track_2b/` as it is: don't rename it or move its files.
- Set the repo to PUBLIC (Settings --> Collaborators --> Manage Visibility)
- Submit the URL of YOUR Git repo.

### Technical Report (pdf)
- Update [technical_report.md](technical_report.md) in this repository with all the details for your submission.
- Upload a pdf of your technical report to this directory, named as `TeamName_Report.pdf`.
- Format: pdf, max. 6 pages
- Submit the pdf of the technical report.

### Demo video (URL)
- Max. 2 min demo video of your prototype

### Dataset (URL) - optional, depending on your project
Submitted datasets must comply with our guidelines for responsibly sourced datasets.

- Create a user account on Hugging Face
- Clone our dataset template on Hugging Face: https://huggingface.co/datasets/HackApertus/online_hack_template
- Complete the dataset card with all required information
- Upload your dataset. It should consist of the following components:
    - evaluation dataset (i.e. individual test cases)
    - model response dataset (i.e. the model response to each test case)
    - metadata file (i.e. additional information about each test case; where relevant, this file must contain instance-level licensing information) 
- Make sure your dataset access control is set to PUBLIC
- Provide the URL of _your_ data set


---

## ⚖️ Judging Criteria

1. Purposeful use of AI
2. Technical rigour
3. Value, cost & scalability
4. Sovereign deployability
5. Implementation feasibility

Judges use a Scale 0–5 per dimension.

---

## Support

**Licensing requirements**
Please check our Terms & Conditions (6. What you build is open source):
https://hackapertus.ch/terms-and-conditions

## FAQ
💡 https://hackapertus.ch/faq

## Contact
💬 In case you have questions, join the conversation on Discord or send an email to “hello@hackapertus.ch”
