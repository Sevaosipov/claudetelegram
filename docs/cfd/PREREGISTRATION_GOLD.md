# Gold only: the search and its one test

Written 2026-10-11, before any run on gold. The user asked for a strategy for gold only, traded as a CFD at IC
Markets (Raw Spread). The same five families that were fixed for EURUSD (PREREGISTRATION_EDGE_EURUSD.md: FADE,
PULLBACK, DONCHIAN, SESSION, WEEK -- 53 candidates, unchanged) are run on gold, so nothing is fitted to gold's chart.

- **Data:** Dukascopy's hourly XAUUSD bid candles, 2010 to last month. **Design:** 2010-2018. **Hold-out:**
  2019-01-01 on, never run. The frame is the EURUSD search's: signal bars closing 09:00-17:00 Europe/Riga, entry at
  the next open, one trade at a time.
- **Costs (IC Markets Raw, assumed):** $0.30 an ounce the round trip (about $0.20 of spread and $7 a 100-ounce
  lot); financing 0.020 % of the price a calendar night, either side.
- **Choice:** among the candidates with at least 80 design trades, a profit in at least 7 of the 9 design years and
  t of at least 2.5, the highest t. None: «no rule found», the hold-out stays shut.
- **Verdict:** the chosen one, once, on the hold-out: its sum above zero and at least 6 of the 8 years above zero.
  Gold more than tripled over the hold-out, so a rule that is only ever long passes it too easily: the longs' and
  the shorts' results are reported apart.

### Amendment A1 (2026-10-11, before any run on gold): the data

The user's MetaTrader 5 terminal at IC Markets now gives the bot its own history (mt5/BotBridge.mq5). Its hourly
gold bars are complete from 2016; before that it has one bar a day. So: **2016 onward from IC Markets' own feed** --
the broker the trades will be made at -- and **2010-2015 from Dukascopy**. The periods, candidates, costs, choice
and verdict are unchanged.

## The design result, and one rule to the hold-out (2026-10-12, before the hold-out was opened)

Design 2010-2018 (Dukascopy to 2015 without June 2015, IC Markets from 2016-01-18): 24 of 53 made money, **none
qualified** -- by the rule above the answer is «no rule found». The nearest: selling 09:00-17:00 every day (2,275
trades, +0.021R, t 2.2, the last two years down), and the PULLBACK family with RSI 25/75 -- all twelve of its
variants up in 7 of the 9 years, +0.25 to +0.40R a trade, but on 55-62 trades each (t at most 1.8).

Six trades a year cannot be judged by a paper league, so the pullback is put to the hold-out as **one rule**, with a
bar it cannot pass on gold's rise alone:

- **The rule:** with the trend (the hourly close against its EMA(480)), enter when RSI(14) closes under 25 in an
  uptrend or above 75 in a downtrend; stop 1 ADR; a third out at each of 2R, 3R, 4R, the stop to the entry after the
  first and to 2R after the second; out after 14 days at the latest. It is the family's first by t.
- **The baseline:** the same stop and exits, entered with the trend at the first watched hour it is free -- being
  with the trend without waiting for the pullback.
- **Verdict:** pass when, on 2019-01-01 to 2026-10-09, the rule's sum is above zero, at least 6 of the 8 years are
  above zero, **and** its mean result per trade is above the baseline's. Longs and shorts are reported apart.
  Whatever comes out, the hold-out is used.
