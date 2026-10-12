# EURUSD day trading: the search and its one test

Written 2026-10-11, before any backtest was run. The user asked for the day league's idea «on EURUSD first, to
perfection, so it does give profit», with at least three take-profit levels. Tuning rules on history until they
show a profit proves nothing: with enough variants one always does. So the history is cut in two. The rules are
chosen on the first part, and the chosen one is run **once** on the second.

## Data and frame

- FXCM's public one-minute bid candles of EURUSD, made into 15-minute bars. **Design period:** 2021-01-01 to
  2024-12-31. **Hold-out:** 2025-01-01 to the last whole week before today. The hold-out is not looked at until the
  choice below is made and committed.
- The frame of DAY_LEAGUE.md: signal bars closing 09:00-17:00 Europe/Riga, entry at the next open, out at 18:00,
  the stop before the target inside a bar, round-trip cost 0.015 % of the price, no trade when the stop is beyond
  4 ATR(14) or the cost is over 0.25R (0.15R with F-COST), one signal per idea and day (per side for ASIA-SWEEP).

## The candidates (168)

- **Idea and stop (4):** OPEN-RANGE with its stop at the range's middle, or at its far side; BRK-15; ASIA-SWEEP.
- **Filters, each on or off (8 sets):** F-TREND -- only with the trend: long above, short below the EMA(200) of the
  15-minute closes; F-NEWS -- no entry from 30 minutes before to 30 minutes after a US or euro-area event that
  TradingView's economic calendar marks as of high importance; F-COST -- the cost at most 0.15R.
- **Exit (5; 6 for ASIA-SWEEP):** X1, X2, X3 -- everything at 1R, 2R or 3R; X3TP -- a third at each of 1R, 2R, 3R,
  the stop to the entry after TP1 and to TP1 after TP2; X3TP-HOLD -- the same thirds, the stop never moved;
  and, for ASIA-SWEEP, XOWN -- its own target, the range's other side.

## The choice and the verdict

- **Choice:** among the candidates with at least 150 design trades and a profit in at least 3 of the 4 design years,
  the one with the highest mean result per trade (R, after costs). If none qualifies, nothing is chosen and the
  answer is «no rule found».
- **Verdict:** the chosen candidate passes when, on the hold-out, its sum is above zero **and** each hold-out year
  (2025, 2026) is above zero. Reported beside it, for scale only: how the five next-best design candidates did on
  the hold-out, and the share of all 168 that made money on the design period.
- A pass goes live in the day league as the EURUSD rule with TP1-TP3 shown. A fail is reported as a fail; no second
  search on the same hold-out.

## Round 2 (2026-10-11, written before it was run): fewer trades, further targets

The user's proposal after the first result: take fewer trades and put the targets further away, so that less is paid
in spread and a hit pays more. In terms that can be tested: trade only when the stop is wide enough that the cost is
a small part of the risk, optionally only with the trend, and let the winners run further. Same data, same design
period, same frame; the hold-out is still unseen.

- **Candidates (120):** the 4 ideas x the cost at most 0.25R, 0.10R or 0.07R x F-TREND on or off x 5 exits: X3, X4,
  X5 (everything at 3R, 4R, 5R), XT (no target: out at the stop or at 18:00), X234 (a third at each of 2R, 3R, 4R,
  the stop to the entry after the first and to 2R after the second).
- **Choice:** among those with at least 100 design trades and a profit in at least 3 of the 4 design years, the
  highest mean result per trade. None qualifying: «no rule found».
- **Verdict:** as in round 1 -- the chosen one, once, on the hold-out: its sum above zero and each of 2025 and 2026
  above zero.

### Round 2, the choice (2026-10-11, before the hold-out was opened)

Design period: 18 of the 120 made money, 5 qualified. The highest mean among them: **ASIA-SWEEP C0.10 X5** -- the
Asia sweep, taken only when the cost is at most 0.10R, everything out at 5R or at 18:00: 149 trades in four years,
+12.3R, +0.083R a trade, 2021 down and 2022-2024 up. It is the one candidate run on the hold-out.

## Round 3 (2026-10-12, written before it was run): what sets a day like 12 October apart

The open-range trade of 12 October worked: EURUSD had been falling for weeks and broke down again. The user asked
to adapt the rule to such days. Only what is known at the entry can be used, so five conditions are tried on the
open-range rule as it now runs (stop at the range's middle, thirds at 1R-2R-3R, no time exit, 0.8 pips, 0.010 % a
night, one trade at a time): **T20** -- the break is with the 20-day move (the close against the close 20 trading
days before); **T5** -- with the 5-day move; **NARROW** -- London's first-hour range is no wider than the median of
the 20 days before; **EARLY** -- the break comes by 12:00 Riga; **PREV** -- the break is in the direction of the day
before (its close against the close a day earlier). Candidates: the rule alone, each condition, each pair (16).

- **Choice** on 2021-2024: at least 150 trades, a profit in 3 of the 4 years, t at least 2.5; the highest t.
  None: «no adaptation found», the hold-out stays shut for this rule.
- **Verdict:** the chosen one, once, on 2025-01-01 on (not run by the open-range rule so far): sum above zero and
  both years above zero.
