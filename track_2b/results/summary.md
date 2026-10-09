# Evaluation summary

Model: `swiss-ai/apertus-v1.5-8b` | test cases: 78 | LLM mode: replay

| System | What it is | Accuracy | Wrong-payment rate (n) | Wrongly paid CHF | Missed-payment rate | Model failures |
|---|---|---|---|---|---|---|
| baseline | Apertus alone (policy + manual in prompt) | 0.513 | 0.733 (33) | 138,781.23 | 0.030 | 0 |
| badge_only | Apertus extraction + badge (hard checks), no content review | 0.872 | 0.222 (10) | 34,521.22 | 0.000 | 0 |
| harness_gen0 | Apertus extraction + badge + review with manual gen 0 | 0.872 | 0.222 (10) | 34,521.22 | 0.000 | 0 |
| harness_gen1 | Apertus extraction + badge + review with manual gen 1 (lessons) | 0.910 | 0.156 (7) | 26,771.22 | 0.000 | 0 |
| harness_gen1_promoted | as gen 1, plus two lessons promoted to hard checks (sum, addressee); added after test results | 0.974 | 0.044 (2) | 9,290.40 | 0.000 | 0 |

Wrong-payment rate = share of invoices that must not be paid automatically (escalate or reject) that the system paid.
Missed-payment rate = share of payable invoices the system did not pay.

## Correct decision rate by case type

| Case type | baseline | badge_only | harness_gen0 | harness_gen1 | harness_gen1_promoted |
|---|---|---|---|---|---|
| E_IBAN_CHANGED | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| E_LIMIT_MONTH | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| E_LIMIT_SINGLE | 0.20 | 1.00 | 1.00 | 1.00 | 1.00 |
| E_NEW_VENDOR | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| E_PRESSURE | 0.25 | 0.00 | 0.00 | 0.75 | 0.75 |
| E_VAT_MISMATCH | 0.00 | 0.00 | 0.00 | 0.00 | 0.75 |
| E_WRONG_ADDRESSEE | 0.00 | 0.00 | 0.00 | 0.00 | 1.00 |
| OK | 0.97 | 1.00 | 1.00 | 1.00 | 1.00 |
| R_DUPLICATE | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| R_IBAN_INVALID | 0.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| R_REVOKED | 1.00 | 1.00 | 1.00 | 1.00 | 1.00 |
| R_SCOPE | 0.40 | 1.00 | 1.00 | 1.00 | 1.00 |

## Extraction accuracy (Apertus, test split)

{"iban": 1.0, "total_chf": 0.987, "invoice_no": 1.0, "vendor": 1.0, "addressee": 0.936, "parsed": 1.0}

## Lessons learned on the train split

Overruled cases: 7 of 42 ({'OK': 1, 'E_PRESSURE': 2, 'E_VAT_MISMATCH': 2, 'E_WRONG_ADDRESSEE': 2}).
Rules distilled by Apertus into manual generation 1 (sha256 `a62b583e11c0d832…`):

- Always verify the invoice addressee matches the client before processing any payment.
- Never pay an invoice that contains mathematically inconsistent amounts or totals.
- Flag and escalate any invoice demanding same-day payment or threatening collection without client confirmation.
- Do not hold routine invoices from known suppliers with matching bank details and correct totals.
- Reject any invoice that does not align with the client's delegated payment authority or expected vendor relationships.

## Outputs

Payment files (pain.001.001.09): {"C01": {"transactions": 11, "total_chf": 13830.0, "validation_errors": []}, "C02": {"transactions": 8, "total_chf": 9130.0, "validation_errors": []}, "C03": {"transactions": 10, "total_chf": 24140.0, "validation_errors": []}, "C05": {"transactions": 6, "total_chf": 18250.0, "validation_errors": []}}
Ledger hash chain verifies: True
