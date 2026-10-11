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
