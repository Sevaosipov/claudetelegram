# Live CFD signals: crypto breakout with a trailing stop (experimental)

Date: 2026-10-08. The user's decision after both research rounds (docs/cfd/): "1". That is: keep the profit factor,
drop staged closing. It replaces Part 3 of docs/superpowers/specs/2026-10-05-cfd-signals.md.

**What goes live is exactly what was tested as E0.** Nothing is closed early. TP1–TP4 are checkpoints, not exits.

**This was a user decision, not a gate pass.** The pre-registered gate judged four-take-profit exits and enabled
nothing. The trailing-exit breakout is profitable on 13 coins out of sample (PF 1.60 on the 11 alts, 2.12 on
BTC/ETH), but it is below the pre-registered significance bar and it depends on a few very large trades. So it is
labelled experimental everywhere.

The bot never places orders.

## The rule (BO-D with exit E0, from cfd/setups.py and cfd/exits.py; reuse them, don't re-implement)

- **Universe:** BTC, ETH and the eleven alts of `cfd.instruments.ALT_COINS`, on daily bars from `cfd.data`. Every
  coin uses the 60 % wick threshold.
- **Signal** at the close of daily bar t:
  - long when close > highest high of the previous 55 bars and close > SMA200;
  - short on the mirror.
- **Initial stop** = signal close ∓ 2.5 × ATR(14).
- **Reference entry** = the open of the bar after the signal bar. That bar is forming when the daily run sees it,
  and crypto's day starts at 00:00 UTC. If it isn't available, use the signal close.
- **R** = |entry − stop|. The signal is skipped when R ≤ 0.25 × ATR or R > 6 × ATR, as in the backtest.
- **Exit:** the trailing stop only. On every completed daily bar, long stop = max(previous stop, bar high − 3 ×
  ATR(14)); short on the mirror. The signal is closed when a bar trades through the stop, at the stop, or at the
  open if it gapped. This is the same code path as the backtest (`exits`), fed one completed bar at a time.
- **One open signal per coin.**

## Storage

Table `cfd_signals`:
- `id`
- `coin`, `symbol`, `side`
- `signal_date`
- `entry`, `stop0`, `stop`, `r`
- `checkpoint` (0–4, the highest of +1R…+4R reached)
- `last_bar` (ISO date of the last completed bar processed)
- `status` (open/closed)
- `closed_date`, `exit_price`
- `result_r` (after the pre-registered costs: round trip 0.25 % for BTC/ETH, 0.40 % for alts; 0.06 % per night)
- `risk_pct`, `risk_eur`, `qty` (NULL with no balance set)
- `created`

kv settings: `cfd_balance_eur`, `cfd_risk_pct` (default 1.0), `cfd_max_open_risk_pct` (default 3.0), `cfd_paused`.

## Daily pass (`cfd/live.py`, called from `bot.main` after the positions step on a full run, under `_run_source("CFD", …)`)

1. **Track.** For each open signal, feed the completed bars after `last_bar`, in order. Each event sends one
   message. The signal's row is saved only after its message went out, so a failed send retries next run without
   duplicates:
   - **checkpoint k reached:** the bar's high (long) or low (short) touched entry ± k × R, for k = 1…4, each once;
   - **closed** at the trailing stop.
   - Within one bar, the stop is processed before a new checkpoint (as in the backtest's bar order), unless the bar
     opened beyond the checkpoint.
2. **Scan** (skipped when paused). For each coin with no open signal, if the last completed bar is a signal bar
   and no signal exists for that coin and `signal_date`, create it and send the entry message.

All network access goes through `cfd.data` (seam `fetch`). One coin failing is logged and the others go on.

## Sizing

- `risk_eur = balance × risk_pct / 100`. `qty = risk_eur ÷ (R × USD→EUR)`, using `fx.to_eur`. It is rounded down
  to 4 significant decimals of a coin: 0.0001 for prices above 100, whole coins for prices under 1.
- **With no balance** the message shows the risk percent and «объём на €1 000 баланса: …».
- **When the open risk of existing signals plus this one exceeds `cfd_max_open_risk_pct`,** the signal is still
  sent. It carries «открытый риск уже N% — выше вашего лимита M%».

## Messages (one per event; `telegram_notify.signal_line` style; prices with sensible precision: 2 decimals at 100 or above, 4 from 1 to under 100, 6 below 1)

```
🟢 <b>SOLUSD!</b>: покупка по 121,50 — стоп 108,20, трейлинг-стоп 3×ATR; TP1 134,80 · TP2 148,10 · TP3 161,40 · TP4 174,70 (отметки, позиция не закрывается); риск 1% = €5,00, объём 0,43 SOL; эксперимент
🟢 <b>SOLUSD!</b>: достигнут TP1 134,80 — стоп подтянут до 119,40, сейчас <b>+1,0R</b> (+€5,00)
🟢 <b>SOLUSD!</b>: сработал трейлинг-стоп — закрыто по 152,30, итог <b>+€11,60</b> (+2,3R)
🔴 <b>SOLUSD!</b>: сработал стоп — закрыто по 108,20, итог <b>−€5,10</b> (−1,0R)
```

- A short reads «продажа по …».
- With no balance, results are in R only.
- The close message's dot follows the sign of the result.
- A checkpoint message shows the current stop after that bar and the open result at the checkpoint level.

## Commands (Telegram) and menu

- **`/cfd`:**
  - the header «CFD-сигналы (эксперимент): пробой тренда на 13 монетах, выход по трейлинг-стопу»;
  - the settings line (balance, risk %, limit, paused or not);
  - the open signals (coin, side, entry, current stop, checkpoint, days);
  - the record of closed signals (count, win rate, profit factor, mean R, total R, and € when sized), marked
    «если брать каждый сигнал».
  - With none yet: «Сигналов пока не было.»
- **`/cfd balance 500`**, **`/cfd risk 1`** (0.1–5), **`/cfd maxrisk 3`** (1–20), **`/cfd off`**, **`/cfd on`**.
  Each validates its input and answers with the new settings line. A bad value gets a usage line.
- **HELP_TEXT** gets one line. The menu gets «5) CFD-сигналы (эксперимент)», the same text as `/cfd`.

## Tests (offline, stub fetch)

- **The live exit is the backtest's.** Replaying a crafted bar series one bar at a time through the live tracker
  gives the same exit price and R as `exits.simulate(..., E0)` on the whole series. Cover long and short, a gap
  through the stop, and stop-and-checkpoint in one bar.
- **Scanner:**
  - signal detection only on the last completed bar;
  - the skip rules;
  - one per coin;
  - no duplicate on a second run the same day;
  - paused → no new signals, but tracking continues.
- **Sizing:** with and without a balance, the rounding, and the over-limit note.
- **Messages:** exact strings for each event.
- **Send failures:** a failed send leaves the state untouched and retries.
- **Commands:** views and validation.
- **Wiring:** the daily run calls the pass after the positions step on a full run only and survives an exception.

## README

A «CFD-сигналы (эксперимент)» section covers:
- the rule;
- that TP1–TP4 are checkpoints;
- the sizing commands;
- the honest status of the evidence in three lines, with a pointer to docs/cfd/.
