# Holdout: 42 fresh invoices (seed 2027), run once

| System | Accuracy | Wrong payments | Wrongly paid CHF | Missed-payment rate | Model failures |
|---|---|---|---|---|---|
| baseline | 0.571 | 14 of 25 | 42,075.89 | 0.000 | 0 |
| badge_only | 0.857 | 6 of 25 | 10,885.89 | 0.000 | 0 |
| harness_gen0 | 0.857 | 6 of 25 | 10,885.89 | 0.000 | 0 |
| harness_gen1 | 0.905 | 4 of 25 | 7,215.89 | 0.000 | 0 |
| harness_gen1_promoted | 1.000 | 0 of 25 | 0.00 | 0.000 | 0 |
| harness_gen1_promoted_grounded | 1.000 | 0 of 25 | 0.00 | 0.000 | 0 |
| harness_gen1_promoted_risk | 1.000 | 0 of 25 | 0.00 | 0.000 | 0 |

Risk score (frozen): fraud ROC AUC 0.9841 (unsupervised 0.8382), must-not-autopay ROC AUC 0.9835. Missed by production: []
