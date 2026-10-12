# TPR (the 0-A sequence of the user's indicator): a proper test across eight markets

Written 2026-10-12, before any run of it. The user put a TPR indicator on the chart and asked to move to that
strategy, test it properly and adjust it if needed. Two rounds on EURUSD alone found nothing (REPORT_ABC.md). One
market cannot tell a real pattern from chance; a pattern that is real should show in most markets with the same
settings. So the rule is tested on eight markets at once, and judged on all of them together.

## The rule (the indicator's, plus the sheet's entry)

- **Point 0:** the highest high or lowest low of the last 300 completed bars. **A:** the extreme since. Valid once
  price has retraced 38.2 % of 0-A without passing point 0.
- **Entry:** a limit at the 50 % or 61.8 % retracement, filled from the bar after the setup becomes valid.
- **Stop:** point 0 (the indicator's red line).
- **Target:** A; or the indicator's green line, 161.8 % of 0-A from point 0; or half at each, the stop to the entry
  after the first.
- **Adjustments allowed:** the move 0-A at least 8 ATR(14), or any size; only with the trend (the close against the
  EMA(200) of the same bars), or either way.
- **Bars:** one-hour or four-hour. 2 x 2 x 3 x 2 x 2 = **48 candidates**.

## Markets, costs, periods

- EURUSD, GBPUSD, AUDUSD, USDJPY, EURGBP, EURJPY, GBPJPY (FXCM's hourly archive; EURUSD continued from its minute
  candles) and XAUUSD (Dukascopy to 2015, IC Markets from 2016).
- Round trip, IC Markets Raw, assumed: 0.8 / 1.1 / 0.9 / 0.9 / 1.2 / 1.3 / 1.8 pips and $0.30 an ounce; financing
  0.010 % a night (gold 0.020 %). No trade when the cost is over 0.25R. One trade at a time a market and side.
- **Design:** 2012-2019. **Hold-out:** 2020-01-01 on (to 2024 for six pairs, to October 2026 for EURUSD and gold);
  no TPR/ABC rule has been run on it.

## The choice and the verdict

- **Choice,** on all markets' design trades together: at least 300 trades, 6 of the 8 years up, t at least 2.5, and
  at least 5 of the 8 markets up on their own; the highest t. None: «no rule found».
- **Verdict,** once, on the hold-out: the sum above zero, at least 5 of the 8 markets up, and at least 4 of the
  years 2020-2024 up.
- A pass becomes the bot's live rule (paper first). A fail is reported as a fail.
