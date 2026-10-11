# The session rule at IC Markets' costs: one rule, one test

Written 2026-10-11, before the runs below. The user will trade CFDs at IC Markets on a Raw Spread account. Of 581
variants tested on EURUSD only one family was above zero: selling during European hours, a drift the literature on
currencies describes (a currency tends to weaken in its own trading hours). At 0.8 pips it made +0.023R a trade on
2012-2019 (t = 2.2), under the bar of 2.5 set for a search of 53. It is now tested as a single rule.

- **The rule (SESSION):** every weekday, short at 11:00 Europe/Riga, out at 18:00; stop 1 ADR (the mean high-low of
  the 14 days before) from the entry. No target.
- **Costs (IC Markets Raw, round trip):** EURUSD 0.8 pips; GBPUSD 1.1 pips (assumed: about 0.4 spread and the $7 a
  lot). No financing: nothing is held overnight.
- **Step 1, a second market:** GBPUSD 2012-2019, never run by any rule here. The drift should be there too if it
  is real.
- **Step 2, the hold-out, once:** EURUSD 2020-01-01 to 2026-10-02 and GBPUSD 2020-2024.
- **Verdict:** the rule passes when EURUSD's hold-out sum is above zero with at least 5 of its 7 years above zero,
  **and** GBPUSD is above zero in both of its periods. A pass puts it in the day league as its EURUSD rule, on
  paper; anything else is a fail, reported as such.
