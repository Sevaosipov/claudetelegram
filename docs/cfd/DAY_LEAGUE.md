# The day league: three intraday ideas run forward on paper for four weeks

Written 2026-10-11, before any run. The user wants to day trade CFDs and can watch the screen from 09:00 to 18:00
(Europe/Riga). Nothing intraday has been tested by this project; the nine daily forex ideas of rounds 1-4 failed. So
three mechanical ideas are run forward on paper, each trade sent to Telegram as it is made, and judged after four
weeks. The bot places no order.

## The frame (every idea)

- **Bars:** Yahoo's 15-minute bars, completed ones only. A pass runs after every bar, Monday to Friday.
- **Markets:** EURUSD, GBPUSD, USDJPY, XAUUSD (Yahoo's gold future), BTCUSD, ETHUSD.
- **Hours (Europe/Riga):** a signal bar closes between 09:00 and 17:00; every trade is out at 18:00.
- **Entry:** the open of the bar after the signal bar.
- **Stop and target:** fixed prices, named by the idea. Inside one bar the stop comes before the target; a bar that
  opens beyond either leaves at its open.
- **No trade** when, at the entry: the price is on the wrong side of the stop; the stop is more than 4 ATR(14) away;
  the round-trip cost (cfd/instruments.py: 0.015 % FX majors, 0.03 % gold, 0.25 % crypto) is more than 0.25 of the
  stop's distance; or a target at a price is nearer than the stop.
- **One trade** per idea, market and day (and side, for ASIA-SWEEP), whatever its end.
- **Result** in R (the stop's distance), less the round-trip cost. No financing: nothing is held overnight.

## The ideas

- **OPEN-RANGE** («Open range») -- EURUSD, GBPUSD, USDJPY, XAUUSD. The range is the high and low of the four bars of
  London's first hour (08:00-09:00 Europe/London). The first later bar to close outside it is the signal: above, long;
  below, short. Stop: the middle of the range. Target: 2R.
- **BRK-15** («Retest») -- all six. The league's BRK-RETEST (PAPER_LEAGUE.md, A3) on 15-minute bars, as the book the
  user sent has it: a close beyond the 20-bar range, a retest of the level within 5 bars, a confirming bar. Stop: the
  extreme since the break. Target: 2R.
- **ASIA-SWEEP** («Asia sweep») -- all six. The range is the high and low of the six hours before 09:00 (03:00-09:00
  Europe/Riga). A bar goes under its low and that bar or one of the next two is the first to close back above it:
  long (a short mirrors it). Stop: the lowest low from the piercing bar on. Target: the other side of the range.

## The verdict

Four weeks from the first weekday run. An idea **passes** when its closed trades number at least 20 and their sum,
after costs, is above zero. A pass is a reason to go on watching it, not proof: twenty trades cannot show an edge.
Gold is a future on Yahoo and its bars can come some minutes late; a trade is made on what the bars show.

## Amendment A1 (2026-10-11, before the first run; no trade made)

The user asked for EURUSD first, at least three take-profit levels, and TradingView as the source of news.

- **Market:** EURUSD only.
- **Exits:** every trade has TP1, TP2, TP3 at 1R, 2R and 3R. A third of the position closes at each; the stop moves
  to the entry after TP1 and to TP1 after TP2 (it acts from the next bar); what is left closes at the stop or at
  18:00. The 2R target and ASIA-SWEEP's own target are dropped.
- **News:** no entry when the signal bar closes within 30 minutes of a US or euro-area event that TradingView's
  economic calendar marks as of high importance. If the calendar cannot be read, the day has no gate.

These rules were backtested first (PREREGISTRATION_DAY_EURUSD.md, REPORT_DAY_EURUSD.md): on 2021-2024 none of the
168 variants qualified, this one included. The league runs it forward as the user asked; the report is the honest
expectation.

## Amendment A2 (2026-10-11, before the first run)

The user will trade at IC Markets on a Raw Spread account: EURUSD's round trip is counted as 0.8 pips (about 0.1 of
spread and $7 a lot) instead of 0.015 % of the price.
