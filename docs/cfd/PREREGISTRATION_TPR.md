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

## Round 2 (2026-10-12, before the run): the structure shift of the «SK System» videos

The user sent a playlist on the same method (SK System). What it adds to the sheet: a sequence counts only after a
**structure shift** -- in a fall, price must first break the last lower high; that break's high is A, the entry is
in the «golden zone» of 0-A, the target is C. As rules, on the eight markets and periods above: a setup is valid
only if A is above the last swing high confirmed before point 0 (a swing high: a bar's high above the three bars
either side). Entry 50 % or 61.8 %; stop at point 0; target A, or C = the entry plus 0-A, or half at each;
one-hour or four-hour bars: **12 candidates**. Choice and verdict as above.

## Round 3 (2026-10-12, before the run): the English SK System course

A second playlist (LogicaTrading, eight short episodes; six could be read) gives two things not tested yet. **The
CCI check:** point 0 is right when CCI(20) is beyond 100 on its side (under -100 at a low, above +100 at a high)
and A when it is beyond 100 on the other. **Entries inside the 50-66.7 % zone with the stop at the zone's next
level:** entry 50 % with the stop at 66.7 %, entry 55.9 % with the stop at 61.8 %, entry 61.8 % with the stop at
66.7 %. Candidates, eight markets, 2012-2019: those three entry-and-stop pairs x target A or C x CCI on or off x
one-hour or four-hour bars (24), and the CCI check on round 1's least bad rule (entry 61.8 %, stop at point 0,
out at A) on both bar sizes (2): **26**. Choice and verdict as above.

## Round 4 (2026-10-12, before the run): the Tradebook's own numbers

The «Tradebook of SK-System» (S. Wenzel, 59 pages, for Stefan Kassing's system) names what the other sources left
open: the stop is a **fixed 20 pips** on the major currencies and metals; the entries are the four levels 50 %,
55.9 %, 61.8 %, 66.7 %; the target is the 161.8 % extension (C = the entry plus 1.618 x 0-A). Eight markets, one-hour
bars, 2012-2019: each of the four entries with the 20-pip stop ($2.00 on gold), out at C or at A: **8 candidates**.
Choice and verdict as above.
