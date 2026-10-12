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

## Round 2: fewer trades, further targets (2026-10-11)

The user's proposal, as 120 candidates (cost caps 0.25R / 0.10R / 0.07R, trend on or off, exits at 3R, 4R, 5R, no
target, or thirds at 2R-3R-4R). Design period: 18 made money, 5 qualified; chosen by the rule fixed beforehand:
**ASIA-SWEEP, cost at most 0.10R, out at 5R or 18:00** -- 149 trades, +12.3R, +0.083R a trade (2021 down, 2022-24 up).

**Hold-out, run once (2025-01-01 to 2026-10-02): fail.** 39 trades, -0.3R, 2025 +1.4R, 2026 -1.7R. For scale, its
four siblings with other exits: +4.2R, +2.2R, +1.5R, -1.1R, each with 2026 down.

Reading: being selective did what was hoped on costs -- the same idea went from -0.14R a trade to about zero -- but
about zero is all it is, on two trades a month. That is neither a profit nor day trading. Further targets did not
help: 3R, 4R, 5R and no target gave the same result within noise. The hold-out is now used; a third search would
need new data to be judged on.

## Round 3: conditions known at the entry (2026-10-12)

The open-range rule as it now runs (no time exit, thirds at 1R-2R-3R, 0.8 pips), 2021-2024: 937 trades, -0.117R a
trade, no year up. With each of five conditions and each pair of them (16 candidates): **all sixteen lost**. Trading
only with the 5-day or the 20-day move halves the loss (-0.06R to -0.09R a trade) and the best pair, with the 5-day
move and the day before, comes to -0.036R on 290 trades with one year of four up. A narrow range makes it worse.
«No adaptation found»; the 2025-2026 hold-out was not run for this rule.

12 October was a with-trend break (5-day and 20-day both down). Such days are the rule's better half, and still
lose a little on average.
