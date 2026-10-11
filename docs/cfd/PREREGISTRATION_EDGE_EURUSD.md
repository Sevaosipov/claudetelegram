# EURUSD: a search across other kinds of entry

Written 2026-10-11, before any run. The three entries of the day league have no edge on EURUSD (528 variants,
REPORT_DAY_EURUSD.md, REPORT_SWING_EURUSD.md). The user asked to tune freely until something is profitable. Tuning
the same entries further can only fit the past, so this search changes the **kind** of entry instead, keeps the
number of candidates small, and sets the bar before looking.

## Data and frame

- EURUSD hourly bid bars as in the swing test. **Design:** 2012-2019. **Hold-out:** 2020-01-01 to 2026-10-02, not
  run by any rule so far.
- Entries at the open of the bar after a signal bar that closes 09:00-17:00 Europe/Riga, Monday to Friday (the
  hours the user watches), unless a family names its own hour. One trade at a time per candidate.
- Costs: round trip 0.015 % of the price, financing 0.010 % a calendar night. The stop before a target inside a bar.
- ADR = the mean high-low of the 14 days before. ATR = ATR(24) of the hourly bars. Trend = the close against the
  EMA(480) of the hourly closes (about 20 days).

## The families (53 candidates)

- **FADE (12):** an hourly close beyond the Bollinger band (20 bars; 2.0, 2.5 or 3.0 deviations) is faded. With or
  without «only toward the trend». Stop 1.5 or 3 ATR. Out when a bar closes back across the 20-bar mean, or after
  48 hours.
- **PULLBACK (24):** with the trend, enter when RSI(14) closes under 30 (above 70 for a short), or 25 / 75. Stop 0.5
  or 1 ADR. Target 2R, 3R, or thirds at 2R-3R-4R with the stop moved up. Longest hold 7 or 14 days.
- **DONCHIAN (6):** an hourly close above the high of the last 5, 10 or 20 days (120, 240, 480 bars), short below
  the low. Stop 1 ADR. Out at 3R, or when a bar closes beyond the opposite half-length channel. Longest hold 28 days.
- **SESSION (7):** short at 09:00, 10:00 or 11:00 Riga, out at 17:00 or 18:00 the same day; and long at 18:00, out at
  09:00 the next weekday. Stop 1 ADR. Every weekday.
- **WEEK (4):** at Monday 09:00 Riga with last week's move (close of Friday against close of the Friday before), or
  against it. Stop 1 or 2 ADR. Out Friday 17:00.

## The choice and the verdict

- **Choice:** among the candidates with at least 80 design trades, a profit in at least 6 of the 8 design years and
  a t-statistic of the mean result of at least 2.5 (53 candidates: 2.0 would be passed by chance), the highest
  t-statistic. None: «no rule found», and the hold-out stays shut.
- **Verdict:** the chosen one, once, on the hold-out: its sum above zero and at least 5 of the 7 years above zero.
