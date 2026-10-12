# helmet / vest / mask: ppe_v2.pt vs ppe_v3B.pt on our val scenes (ppe_06_vest_off, ppe_21_far)

| item | ppe_v2.pt AP50 (P / R) | ppe_v3B.pt AP50 (P / R) | better |
|---|---|---|---|
| helmet | 0.943 (0.92 / 0.91) | 0.896 (0.87 / 0.79) | ppe_v2.pt |
| vest | 0.802 (0.54 / 0.96) | 0.903 (0.70 / 1.00) | ppe_v3B.pt |
| mask | - | - | no labels |

A difference under 0.02 AP50 counts as a tie. Labels: machine-made + reviewed - indicative only.
