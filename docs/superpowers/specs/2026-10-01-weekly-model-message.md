# Weekly model message (Fridays)

Date: 2026-10-01. Requested by the user: "i want the automatic telegram signal to be sent every friday, not every day.
though i still want to have opportunity to send any ticker in telegram or terminal and receive analysis".
It changes §4–§5 of docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md.

## Decisions

1. **The weekly run.** The weekly run is the first full (not filtered) daily run on a Friday, Saturday or Sunday of an
   ISO week.
   - The first Friday run is the normal case. Saturday or Sunday only when the Mac missed Friday.
   - It is marked in kv_cache per ISO week. There are two independent keys: the model's buys, and the Telegram
     message.
2. **The model buys only on the weekly run.** Exits are still checked, and sell orders placed, on every daily run
   (stops are risk control).
   - **Why:** every buy the user sees on Friday can be copied at the same fill the model gets (the next close). The
     paper record then matches what a Friday follower can achieve.
   - Scoring still runs daily: it feeds the journal's tier, the cached scores, the menu and the analyst.
3. **The weekly message**, one per weekly run, is always sent, even in a quiet week, because it is the portfolio's
   weekly report. Its key is set only after a successful send, so a failed Friday send retries on the next run of
   the same Fri–Sun window.
4. **Daily Telegram:** only the close alerts for the user's own `/bought` positions, as soon as they fire, plus the
   existing breakage warnings (source failed / silent, model crashed).
   - The model's daily buys and sales and the group exits are no longer sent daily.
   - **Why:** close alerts are about real money and shouldn't wait up to a week.
5. **The monthly report** is sent only on a weekly run (so the first Friday of a month), as a second message right
   after the weekly one. Its own once-per-month key is unchanged.
6. **On demand, unchanged:** ticker lookups, `/ask` and free-text questions, and `/portfolio` in Telegram;
   `python analyst.py ask`; and the menu.

## The weekly message (paper_report.format_week)

`format_week(conn, today, report, *, html=True) -> str`. The week is `today - 6 days … today`.

Sections:
- **Header:** «📊 Модельный портфель — неделя DD.MM–DD.MM», in bold.
- **«🟢 Покупки»:** the model's buy orders created in the week, from both model books.
  - Each shows ticker, amount, «(N% портфеля)», stop, score, and the order's `reason` text.
  - A filled order shows its fill date and entry close; a pending one shows «исполнится по закрытию ближайшего
    торгового дня».
  - The section is omitted when empty.
- **«🔴 Продажи»:** positions closed in the week, each with reason and result in %, plus sell orders still pending
  («ждёт исполнения»). Omitted when empty.
- **«📋 В портфеле»:** each open position with ticker, days held, result in %, and «стоп −N%». It reads
  «пусто — всё в деньгах» when there are none.
- **«👀 Наблюдение»:** up to 5 WATCH scores from `report.scored` (or `model.cached_scores` when `report` is None):
  ticker, score and the top reason. Omitted when empty.
- **«🚨 Продают те, кто покупал»:** `signal_journal` rows with `kind = 'exit'` emitted in the week, each with ticker,
  company and the sellers from the members JSON. Omitted when empty.
- **Last line:** «Портфель: €X (±Y% с начала), за неделю ±Z%; смесь 70/30: ±W% с начала». The week figure comes from
  the summed `paper_equity` values on/before `today-7` versus today. Leave out any part that can't be computed.

Rules: `<b>` and `<pre>` only, every dynamic string escaped, Russian, no disclaimers.

## Code

- **`model.run(..., buy: bool = True)`:** with `buy=False`, `_buy_stocks`/`_buy_coins` are skipped and everything else
  is unchanged.
- **`bot.py`:**
  - `_weekly_due(conn, today) -> bool`: weekday ≥ 4 and the week's key not set. There are two keys in kv_cache:
    `model_buys_<YYYY>-W<ww>` and `weekly_message_<YYYY>-W<ww>`, built from `isocalendar`.
  - `_run_model(conn, args, *, buy)`: bot passes `buy = (not filtered) and today.weekday() >= 4 and
    buys-key not set`, and sets the buys key after a model pass that didn't crash.
  - Daily: `_send_closes(conn, closes)` sends «🚪 Ваши позиции» plus each `format_close_alert`, and marks them
    alerted only on a successful send. It replaces `_send_day`.
  - On a weekly run with Telegram on and the message key not set: send `paper_report.format_week(conn, today,
    report)`, set the key on success, then `maybe_send_monthly_report`. `maybe_send_monthly_report` is no longer
    called on other days.
- **`telegram_notify.format_model_day`** and its tests: delete when nothing uses them. `format_trade` too if unused.
- **`telegram_bot.HELP_TEXT`:** one line «Сводка модельного портфеля приходит по пятницам; /portfolio — в любой
  момент.»
- **README:** the model section and the Telegram section describe the weekly rhythm (Friday message, Friday-only
  buys, daily exits, immediate /bought alerts, monthly report on the first Friday).

## Tests (offline)

- **`_weekly_due` across the week:**
  - Mon–Thu → False;
  - Friday with no key → True;
  - Saturday after Friday's key is set → False;
  - Saturday when Friday was missed → True;
  - a new ISO week → True again.
- **Buys:** `model.run(buy=False)` places no buy (an exit still sells), and `buy=True` keeps today's behaviour. bot
  passes `buy` correctly and sets the buys key only after a non-crashing pass; a filtered run never buys.
- **Daily close alerts:** sent and marked alerted on success; nothing sent when there are none; a failed send leaves
  them unmarked.
- **The weekly message:**
  - sent once per week;
  - retried next run after a failed send;
  - the monthly report only on a weekly run;
  - `format_week` sections and their omission, the quiet-week text, the week return, and HTML escaping of a hostile
    company name.
- **The `main()` order test** is updated.
