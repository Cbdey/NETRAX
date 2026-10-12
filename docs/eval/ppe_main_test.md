# helmet / vest / mask: ppe_v2.pt vs ppe_v3B.pt on our test scenes (ppe_02_fullkit_dim, ppe_17_two_in_line, ppe_22_backlit)

| item | ppe_v2.pt AP50 (P / R) | ppe_v3B.pt AP50 (P / R) | better |
|---|---|---|---|
| helmet | 0.993 (1.00 / 0.96) | 0.988 (0.95 / 0.95) | tie (keep main) |
| vest | 0.995 (0.99 / 1.00) | 0.991 (0.98 / 0.98) | tie (keep main) |
| mask | - | - | no labels |

A difference under 0.02 AP50 counts as a tie. Labels: machine-made + reviewed - indicative only.
