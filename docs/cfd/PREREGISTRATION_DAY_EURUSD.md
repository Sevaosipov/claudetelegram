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
