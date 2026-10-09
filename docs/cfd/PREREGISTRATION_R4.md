# CFD research, round 4 (forex): pre-registration

Written 2026-10-09, before any round-4 code or run. Rounds 1–3 tested seven ideas on daily prices and none passed.
The user asked to go on looking for forex signals and chose two ideas that bring in information the earlier rounds
did not have: who holds what (H8), and the time of day (H9). Both need data the project did not use before; only
access to it was checked before this was written (the files download and parse), no result was looked at.

## What is different from rounds 1–3

**The gate.** The user dropped the profit factor of 1.10: a strategy "just needs to be profitable when checking the
portfolio at the end of each month". No strategy wins every month, so this is read, here and before any result, as:

- out-of-sample net result after costs > 0 (a profit factor above 1.00);
- in-sample net result after costs > 0 (it did not work in one half only);
- out-of-sample, more than half of the calendar months that closed a trade end in profit;
- out-of-sample at least 100 trades.

The t statistic of the mean result is reported but is not part of the gate. These are the eighth and ninth
hypotheses on forex: a pass with a low t is a result that chance alone produces often, and the report says so in
those words.

**The result of a month** is the sum of the results in R (after costs) of the trades closed in it: every trade
risks the same amount, so the sum in R is the month's profit in units of that risk.

Everything not restated is as in rounds 1–3: ATR(14) Wilder on daily bars, a stop gapped through fills at the open,
the stop before any other exit within a bar, one open trade per instrument, the result in R after costs,
in-sample 2006-01-01…2016-12-31, out-of-sample from 2017-01-01.

## H8. COT-EXT: against the speculators at an extreme of their positions

**Data.** CFTC, Commitments of Traders, legacy report, futures only (the yearly archives
`https://www.cftc.gov/files/dea/history/deacot<year>.zip`), weekly. A report is of a Tuesday and is published on
the Friday of that week at 15:30 New York time; it is usable from the first daily bar after that Friday. A report
whose Friday is a US federal holiday is published later; to be safe every report is taken as usable from the
second daily bar after its Friday.

**Currencies and pairs** (CME contract → Yahoo daily pair, and whether the pair moves with the currency):
EUR `099741` → EURUSD (with), GBP `096742` → GBPUSD (with), AUD `232741` → AUDUSD (with), NZD `112741` → NZDUSD
(with), JPY `097741` → USDJPY (against), CAD `090741` → USDCAD (against), CHF `092741` → USDCHF (against).

**Measure.** net = (non-commercial long − non-commercial short) ÷ open interest. The index of a week is where its
net stands in the range of the last 156 weekly values including its own: 100 × (net − min) ÷ (max − min); no index
before 156 weeks of history or when max = min.

**Signal**, on the first bar a report is usable:
- index ≥ 95 (speculators at their longest in three years) → short the currency;
- index ≤ 5 → long the currency.

Short the currency is short the pair where the pair moves with it and long the pair where it moves against it.
No new trade while one is open in that pair.

**Entry** at the open of the bar after the signal bar.

**Stop** = entry ∓ 3.0 × ATR(14) of the signal bar. R is that distance.

**Exit**, the earliest of:
- the stop;
- the open of the bar after the first usable report whose index has come back to 50 or beyond (a short of the
  currency: index ≤ 50; a long: index ≥ 50);
- the open of the 130th bar after the entry (26 weeks).

**Costs:** round trip 0.015 % of the entry, financing 0.010 % per night.

**Gate:** as above, pooled over the seven pairs, no per-pair picking.

## H9. LDN-BO: the break of the Asian range in London's morning, flat by the evening

**Data.** Dukascopy hourly bid candles (`https://datafeed.dukascopy.com/datafeed/<PAIR>/<year>/<month-1>/
BID_candles_hour_1.bi5`), in UTC. An hour with no volume (the market closed) is not a bar. Hours are read in
London time (Europe/London, with its summer time).

**Pairs:** EURUSD, GBPUSD, USDJPY, AUDUSD, EURGBP, EURJPY, GBPJPY.

**Each London weekday** (Monday to Friday):
- the range is the highest high and the lowest low of the hourly bars that start from 00:00 to 07:00 London time
  (eight bars; a day with fewer than six of them is skipped);
- the day is skipped when the range's height is more than 0.6 × the daily ATR(14), the daily bars being the
  London calendar days before this one built from the same hourly bars (a narrow night range is the setup);
- in the bars that start from 08:00 to 11:00 London time, the first bar whose high is above the range's high is a
  long at the range's high, and the first whose low is below the range's low is a short at the range's low; the
  first one of the day is the day's only trade. A bar that opens beyond the level enters at its open.

**Stop** = the other side of the range. R is the distance from the entry to it.

**Within a bar** the worst is assumed: a bar that breaks both sides is a loss of 1R; an entry bar that also reaches
the stop is a loss of 1R.

**Exit**, the earliest of:
- the stop;
- the open of the bar that starts at 20:00 London time (or, with no such bar, the close of the last bar of that
  London day).

There is no target and no trade is held overnight.

**Costs:** round trip 0.015 % of the entry for EURUSD, GBPUSD, USDJPY and AUDUSD and 0.030 % for EURGBP, EURJPY
and GBPJPY; no financing.

**Gate:** as above, pooled over the seven pairs, no per-pair picking.

## Outputs

- `docs/cfd/BACKTEST_REPORT_R4.md`: per hypothesis, the in-sample and out-of-sample table (trades, win rate, mean
  R, t, profit factor, net R, the share of profitable months, the worst month, the longest run of losing months)
  and the gate table. For information only: per-pair and per-year results.
- A `round4` block merged into `docs/cfd/enabled.json`.

A hypothesis that fails is not traded. A hypothesis that passes is reported to the user with its t statistic
before anything goes live.
