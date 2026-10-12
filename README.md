# Mandate Desk — mandate-bounded payment agents for Swiss fiduciaries

Hack Apertus · Track 2B (Own Project) · team hack-1 · built with Apertus v1.5 8B

Swiss fiduciaries pay supplier invoices for many small clients, each of which delegated a different scope
and limit. Mandate Desk gives each client its own payment agent and keeps it inside the client's signed
mandate: **Apertus** reads and reviews multilingual invoices (DE / FR / IT / EN), **code** enforces the
mandate and checks what Apertus read, **a person** decides the exceptions, and every step is **signed into
a ledger**.

| On 500 unseen synthetic invoices, run once | Apertus alone | Mandate Desk |
|---|---|---|
| Non-payable invoices paid (of 300) | 178 | **0** (95% CI 0–1.3%) |
| Legitimate invoices held for a person (of 200) | 2 | 7 |

Seventeen attacks on the agent's identity and authority were blocked in 561 of 561 attempts (scenarios we
wrote; not a security audit).

- **Technical report (PDF, 6 pages):** [track_2b/hack-1_Report.pdf](track_2b/hack-1_Report.pdf)
- **Demo video (1:49):** https://drive.google.com/file/d/1PivYnnCA00WErcqTeLPPWcmFxSrGoKiP/view?usp=sharing
- **Project, details and results:** [track_2b/](track_2b/README.md)

## Run it

```bash
make run    # from this directory: builds the Docker image, runs the evaluation, serves the app on http://localhost:8000
make eval   # evaluation only: prints the results table and exits
```

No API key is needed: without one the run replays the recorded Apertus responses and reproduces our
numbers. To call Apertus live, copy `track_2b/.env.example` to `track_2b/.env` and set `LLM_NAME`,
`LLM_BASE_URL` and `LLM_API_KEY` (any OpenAI-compatible endpoint serving Apertus).

**This is a demo:** the reviewer app has no login or roles. Run it only on a trusted machine.

## Layout

| Path | What it is |
|---|---|
| `track_2b/README.md` | Project overview, results and the challenge description |
| `track_2b/technical_report.md` | The technical report (source of the PDF) |
| `track_2b/Makefile`, `Dockerfile` | `make run` / `make eval` |
| `track_2b/src/` | Code |
| `track_2b/data/` | Synthetic data and recorded Apertus responses |
| `track_2b/results/` | Results of the reference run and the study |
| `track_2b/tests/` | Offline tests |

## License

All Hack Apertus projects are open-sourced; see the Terms & Conditions (6. What you build is open source):
https://hackapertus.ch/terms-and-conditions. Code: Apache-2.0 (`LICENSE`). Docs: CC-BY-4.0. Data:
CDLA-Permissive-2.0. All data is synthetic and fictional.
