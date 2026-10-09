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

