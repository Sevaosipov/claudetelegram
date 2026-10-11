# EURUSD swing trades: the same entries, held from hours to weeks

Written 2026-10-11, before any run. After the day-trading search failed (REPORT_DAY_EURUSD.md) the user proposed:
do not close a trade the same day -- let it run from a few hours to a few weeks, and take still fewer trades. This is
that proposal as a test. The entries are the day league's three ideas on hourly bars; what changes is how long a
trade lives, how wide its stop is, and that only one trade is open at a time.

## Data and frame

- EURUSD hourly bid bars: FXCM's public hourly archive 2012-2020, and hourly bars built from its one-minute candles
  2021 to 2026-10-02. **Design period:** 2012-2019. **Hold-out:** 2020-01-01 to 2026-10-02. The years 2021-2026 were
  used before, but only for rules that close every trade by 18:00; none of the rules below has been run on them.
- Signals on bars closing 09:00-17:00 Europe/Riga, Monday to Friday; entry at the next bar's open; **one trade at a
  time**: a signal while a trade is open is ignored.
- Costs: the round trip 0.015 % of the price, and financing 0.010 % of the price for every night held (calendar
  nights, so a weekend counts three). Inside a bar the stop comes before a target; a bar that opens beyond the stop
  leaves at its open.

## The candidates (240)

- **Entry (4):** OPEN-RANGE -- the first hourly close outside the bar of London's first hour (08:00-09:00
  Europe/London), stop at that bar's middle, or at its far side; BRK -- the league's break, retest and confirming
  bar on hourly bars, stop at the extreme since the break; ASIA-SWEEP -- a false break of the range of the six hours
  before 09:00, closed back inside within three bars, stop at the sweep's extreme.
- **Stop (2):** the entry's own, or widened to half of the average daily range (the mean high-low of the 14 days
  before) when its own is nearer.
- **F-TREND (2):** off, or only with the trend -- long above, short below the EMA(200) of the hourly closes.
- **Target (5):** everything at 3R, 5R or 8R; none; or a third at each of 2R, 3R, 4R with the stop to the entry
  after the first and to 2R after the second.
- **Longest hold (3):** 7, 14 or 28 days from the entry; then out at the next bar's open.

## The choice and the verdict

- **Choice:** among the candidates with at least 100 design trades and a profit in at least 6 of the 8 design years,
  the highest mean result per trade (R, after costs). None qualifying: «no rule found», and the hold-out stays shut.
- **Verdict:** the chosen one, once, on the hold-out: its sum above zero and at least 5 of the 7 years 2020-2026
  above zero.
