# The forex paper league: rules fixed before the first trade

Written 2026-10-09. Nine forex ideas failed on history (rounds 1–4). The user chose to stop backtesting and run four
new ideas forward on paper for three months, aiming at 3–7 trades a week in total. Nothing here was tried on past
data: the results do not exist yet, so they cannot have shaped the rules.

The bot places no orders. Every trade here is a paper trade: a message, and a row in `league_trades`.

## Common rules

- Daily bars from Yahoo, completed ones only; ATR(14) Wilder of the signal bar (the last completed bar).
- **Entry** at the open of the first bar after the signal bar (at the morning run this is today's open).
- **Stop** = entry ∓ k × ATR; R is that distance. A stop gapped through fills at the open; within a bar the stop
  comes before any other exit.
- **Result** in R after costs: the instrument's round trip (0.015 % a major, 0.030 % a cross, 0.060 % EURNOK) and
  0.010 % of the entry per night.
- One open trade per idea and pair. A signal is made once (its `ref`).
- The league runs 13 weeks from its first run. After that no new trade is opened; open ones run to their exit.

## The ideas

**1. COT-WITH («За спекулянтами»)** — with the large speculators at an extreme.
CFTC legacy futures-only report, weekly. net = (non-commercial long − short) ÷ open interest; index = its place, 0–100,
in the range of the last 156 weekly values. From the first run after the Friday of a report's week, for EUR, GBP, AUD,
NZD, JPY, CAD, CHF: index ≥ 90 → long the currency; index ≤ 10 → short it (against USD: EURUSD, GBPUSD, AUDUSD,
NZDUSD with the currency; USDJPY, USDCAD, USDCHF against it). Stop 2.0 ATR. Exit at the open of the 10th bar after
the entry bar. ref: the report's date.

**2. RATE-MOM («Ставки»)** — towards the currency whose 2-year yield is gaining.
Daily 2-year government yields: US (Treasury), euro area (ECB AAA curve), Canada (Bank of Canada), Japan (MoF).
spread = the other currency's yield − the US yield. Once per ISO week, at its first run: change = the spread's last
value − its value 10 observations earlier (dates both have). change ≥ +0.10 pp → long the other currency;
change ≤ −0.10 pp → short it. Pairs: EURUSD (with EUR), USDCAD and USDJPY (against CAD, JPY). Stop 2.0 ATR. Exit at
the open of the 5th bar after the entry bar. ref: the ISO week.

**3. CMD-LEAD («Сырьё»)** — a commodity currency after a sharp move of its commodity.
Each day, on the commodity's last completed bar (not older than 3 days): z = its 3-bar return ÷ the standard deviation
of the 60 3-bar returns before it. |z| ≥ 1.5 → trade the currency the same way: WTI (CL=F) → CAD (USDCAD, against),
copper (HG=F) → AUD (AUDUSD, with), Brent (BZ=F) → NOK (EURNOK, against). Stop 1.5 ATR. Exit at the open of the 3rd
bar after the entry bar. ref: the date of the commodity's bar.

**4. MONTH-END («Конец месяца»)** — against the dollar after a strong month for US stocks, with it after a weak one.
On a weekday with two (or, if that run was missed, one) weekdays left in the month: m = the S&P 500's last close ÷
its last close of the previous month − 1. m ≥ +1 % → short USD; m ≤ −1 % → long USD; between, nothing. Pairs: EURUSD,
GBPUSD, AUDUSD (short USD = long the pair), USDJPY (short USD = short the pair). Stop 2.0 ATR. Exit at the open of the
first bar of the new month. ref: the month.

## What is reported

- A message for each paper trade opened and each one closed (they can be muted: `/league off`).
- At the first run of each calendar month, a scoreboard: per idea, the trades closed and the result in R of the month
  just ended and since the start.
- After 13 weeks, a final scoreboard with a verdict per idea.

## The verdict

The user's rule is "profitable when checking at the end of each month". An idea **qualifies** when, over the 13 weeks:
its result after costs is above zero, at least two of its three month-end checks were in profit, and it closed at least
8 trades. A qualifying idea is reported to the user, who decides whether it becomes a real signal; three months and a
handful of trades cannot show that an idea works, only that it has not failed yet.

## Amendment A1 (2026-10-09, the league's first day, two trades open, none closed)

The user asked for take-profit levels on the league's trades. Every trade now shows TP1–TP4 = +1R…+4R from its entry,
and a message says when one is reached. They are **marks, not exits**: the trade still leaves at its stop or its time
exit, and the scoreboard counts that result, exactly as written above. The exits are not changed because the rules were
fixed before the first trade, and because in rounds 1–2 closing in stages at such levels lowered every result. Within a
bar the stop comes before a new mark, unless the bar opened beyond it.

## Amendment A2 (2026-10-09, the league's first day, two trades open, none closed, no bar processed)

The user asked for a second version of every trade that does close at the take-profit levels, to compare the two after
13 weeks. Each trade is now scored twice:

- **the first version**, as written above: the stop or the time exit, the whole position;
- **the second version («с целями»)**: a quarter closes at each of TP1…TP4 (+1R…+4R); the stop moves to the entry after
  TP1, to TP1 after TP2 and to TP2 after TP3; what is left closes at that stop or at the trade's time exit. Within a bar
  the stop comes before the next level. The costs are the same round trip and the financing to the day its last part
  closes.

The scoreboard shows both. The verdict stays on the first version, as fixed before the first trade; the second is the
comparison the user asked for.

## Amendment A3 (2026-10-11): a fifth idea, BRK-RETEST («Пробой и ретест»)

The user sent a price-action book (15-minute chart patterns: break of a level, retest, confirming candle, stop below
the previous low, target at 2 or 3 times the stop) and asked to try its idea. The book has no statistics and its
patterns are drawn by eye, so its one mechanical rule is written down here and run forward like the others. It is on
**daily** bars, as the rest of the league (the book is on 15 minutes: the league has no intraday engine).

- **Pairs:** EURUSD, GBPUSD, AUDUSD, NZDUSD, USDJPY, USDCAD, USDCHF, XAUUSD, BTCUSD, ETHUSD.
- **Long** (a short mirrors it): a bar closes above the highest high of the 20 bars before it, the bar before it had
  not (the breakout); within the next 5 bars a bar's low comes back to that level (the retest); the first bar from
  the retest on that closes above the level and above its own open confirms -- the signal is made when that bar is
  the last completed one. No close may be more than one ATR(14) back under the level on the way.
- **Entry** at the next open. **Stop:** the lowest low from the breakout bar to the confirming bar. No trade when the
  entry is on the wrong side of the stop, or the stop is 0.25 ATR or nearer, or more than 3 ATR away.
- **Target:** 2 times the stop's distance -- a real exit, unlike the TP marks of the other ideas. Inside one bar the
  stop comes before the target.
- **Otherwise** out at the open of the 20th bar after the entry bar.
- ref: the breakout bar's date and the side. The idea joins on the league's third day and ends with it.

## Amendment A4 (2026-10-11): a sixth idea, RNG-DEV («Ложный пробой»)

The user sent ten recorded trading sessions (STONKS, 2022-23). They hold no statistics; the one rule in them that can
be written down is «one deviation forms the other»: a false break of one side of a range is followed by a move to the
other side. It is run forward here like the others, on daily bars.

- **Pairs:** the ten of BRK-RETEST.
- **The range:** the highest high and the lowest low of the 20 bars before the piercing bar; it is at least 2 ATR(14)
  wide.
- **Long** (a short mirrors it): a bar's low goes under the range's low (the piercing bar); it or one of the next two
  bars closes back above that low, every bar before it having closed under it -- the signal is made when that bar is
  the last completed one.
- **Entry** at the next open. **Stop:** the lowest low from the piercing bar on. No trade when the entry is on the
  wrong side of the stop, or the stop is 0.25 ATR or nearer, or more than 3 ATR away.
- **Target:** the other side of the range (its high) -- a real exit. No trade when it is nearer the entry than the
  stop is. Inside one bar the stop comes before the target.
- **Otherwise** out at the open of the 20th bar after the entry bar.
- ref: the piercing bar's date and the side. The idea joins on the league's third day and ends with it.

## Amendment A5 (2026-10-11, an hour after A4, one RNG-DEV trade made, none closed)

The first run of RNG-DEV made a USDCAD short whose «range» was a month's rise from 1.38 to 1.43: the 20-bar high and
low of a trend, with the target ten times the stop away. A4 did not say what a range is. Two corrections, made before
any RNG-DEV trade closed:

- **The range went sideways:** its last close is no further from its first than half the range's width. Otherwise no
  signal.
- **No entry on a weekend** except in crypto: that run was on a Sunday and the trade was filled at Yahoo's stub of a
  closed market. This holds for every idea.

The USDCAD trade is void (never a result), as a trade the rules would not make.

## Note (2026-10-11): the source of COT-WITH

The CFTC site began refusing scripted downloads of its yearly archives (403), so the league reads the same legacy
futures-only report from the CFTC's public reporting API. Checked on 364 rows of 2025: every value is the same. The
rules are unchanged.

## Amendment A6 (2026-10-12): an idea is retired at -10R

An idea whose closed trades sum to -10R or worse makes no new trades (cfd/paper.py): from there a pass within the
13 weeks is implausible and the answer is known. Its open trades run to their end and its row stays on the board,
marked «stopped». No idea was near the line when this was written (every idea at 0.00R, none closed).
