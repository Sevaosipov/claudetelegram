# EURUSD day trading: the result of the search (2026-10-11)

Plan: PREREGISTRATION_DAY_EURUSD.md. Data: 1,973,574 one-minute FXCM candles, 2021-01-03 to 2026-10-02, as
132,382 fifteen-minute bars. Design period 2021-2024: 4,245 setups, 706 high-importance calendar events.

**Verdict: no rule found.** Of the 168 candidates, 2 made any money on the design period and none met the choice rule
(150 trades, 3 of 4 years up). Nothing was chosen, so the hold-out (2025-2026) was not run and is still unseen.

| idea (no filter, three take-profits) | trades | net R / trade | cost R / trade | before cost |
|---|---|---|---|---|
| OPEN-RANGE, stop at the middle | 891 | -0.174 | 0.132 | -0.042 |
| OPEN-RANGE, stop at the far side | 766 | -0.151 | 0.091 | -0.060 |
| BRK-15 | 644 | -0.154 | 0.149 | -0.005 |
| ASIA-SWEEP | 670 | -0.138 | 0.148 | +0.010 |

Before costs the three ideas are a coin toss on EURUSD (within ±0.06R a trade); the spread then takes 0.09-0.15R
from every trade. No filter (trend, news, cost) and no exit (1R, 2R, 3R, thirds with or without a moved stop)
changes that: the best candidate with 200 trades made +0.017R a trade with two losing years of four. The news
filter did not help measurably.

What this means: on a 15-minute chart the cost is 10-15 % of the risk on every trade, so a rule needs a real edge of
that size just to break even, and these patterns have none.
