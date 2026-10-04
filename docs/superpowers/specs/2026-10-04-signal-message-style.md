# Signal message style

Date: 2026-10-04. Requested by the user with a screenshot of another bot's alert and "i want signals to look something
like that":

> 🔴 **GBPUSD!**: сработал исходный стоп — закрыты части TP1, TP2, TP3, TP4 по 1.32452, итог **-865.74**

That alert is one short message per event: a coloured dot, the ticker in bold with "!", what happened in plain words,
the price, and the result in bold.

This changes how the bot's automatic Telegram signals look. It doesn't change any rule about when a signal fires.
It doesn't touch on-demand replies: ticker analysis, `/ask`, `/portfolio`, `/model`, and the menu.

## The line

`{dot} <b>{NAME}!</b>: {event} — {details}` followed, for a close, by `, итог <b>{result}</b>{extra}`.

One helper builds it: `telegram_notify.signal_line(dot, name, event, details=None, *, result=None, extra=None,
html=True) -> str`. Every dynamic part is escaped. With `html=False` there are no tags (for logs and the menu).

**Dots:**
- 🟢: a buy signal, or a close / sell alert with a result ≥ 0;
- 🔴: a close or sell alert with a result < 0, or a sell alert whose result is unknown;
- ⚪: neutral events (Trading 212 sync notices, a carry strategy going flat).

**Names:** a coin by its symbol (`BTC`); a Trading 212 holding by its shown name (as today's `_position_name`);
otherwise the ticker.

**Numbers** are formatted the Russian way, as the existing helpers do: `23,10`, `€10 000`, `+24,5%`, with the real
minus sign `−`.

**Result:**
- For a position with a known quantity (Trading 212 holdings): the money result in bold and the percent in
  brackets: `итог <b>−€24,10</b> (−10,4%)`.
  - The money is in the position's own currency symbol when it isn't EUR and no EUR value is known: `−$26,40`.
- Otherwise the percent in bold: `итог <b>−10,4%</b>`. For the model's virtual trades, the EUR amount goes in
  brackets: `итог <b>+24,5%</b> (+€2 450)`.

## Messages

Each signal is its own Telegram message.

### 1. Model buy (weekly run)

One per buy order created in the week:

`🟢 <b>GME!</b>: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10% от максимума, в модели €10 000`

- **details** = the order's reason text without its «балл N: » prefix, at most 2 reason parts joined by «; ». Then
  «балл N», «стоп −N% от максимума», «в модели €X».
- Add «, нет на Trading 212» when the stored score says `t212 is False`. Look the ticker up in the week's cached
  scores (`model.cached_scores`); leave the label out when unknown.
- A filled order adds «, вход DD.MM по 23,10». A pending one adds nothing.

### 2. Model sale (weekly run)

One per position closed in the week:

`🔴 <b>GME!</b>: сработал стоп −10% от максимума — продано DD.MM, итог <b>−9,8%</b> (−€980)`

- **event** = the position's `close_reason`, worded as an event (see the mapping below).
- A sell order still pending: `🔴 <b>GME!</b>: сработал стоп −10% от максимума — продажа по ближайшему закрытию,
  сейчас <b>−9,8%</b>`. The dot follows the sign of the current result.

### 3. Sell alert for the user's own position (daily, immediate)

One message per alert:

`🔴 <b>GME!</b>: сработал стоп от максимума — пора продавать: вход 23,10 → сейчас 20,70, итог <b>−€24,10</b> (−10,4%)`

- **event** by trigger:

  | Trigger | Event |
  |---|---|
  | trailing_stop | «сработал стоп от максимума» |
  | insider_sell | «продаёт инсайдер» |
  | caution | «отток по монете» |
  | trend_down | «тренд развернулся вниз» |
  | dead_money | «стоит на месте» |
  | time | «год в позиции» |
  | news | «плохие новости» |

- **details** = «пора продавать: вход X → сейчас Y». With no price: «пора продавать: вход X».
- The alert's own detail (the insider's name and date, the headline, the stop distance text) goes on a second line,
  indented by three spaces, as plain escaped text.
- It replaces `format_close_alert`'s old «🚪 …» layout and the «🚪 Ваши позиции» header.

### 4. Group exit signals found in the week (weekly run)

`🔴 <b>XYZ!</b>: продают те, кто покупал — Name A, Name B`. This is the same data `format_week` lists today.

### 5. Trading 212 sync notices

- **New holding:** `⚪ <b>GME!</b>: куплено в Trading 212 — 10 шт. по 23,10 USD, слежу: стоп, срок и новости`. The
  watched list is the existing honest text.
- **Sold:** `⚪ <b>GME!</b>: продано в Trading 212 — слежение закрыто`.
  - When the position's average price and a last known price exist, add `, итог ≈ <b>+4,1%</b>`.
  - With a quantity, give money first: `итог ≈ <b>+€8,30</b> (+4,1%)`.
  - The dot stays ⚪.
- **The one-time first-sync message and the warnings (⚠️)** keep their current text.

### 6. Carry strategy (`format_carry_signal`)

- **Into a trade:**
  - `🟢 <b>EURUSD!</b>: вход в лонг ({reason}) — цена 1,1327, 200-дн. средняя 1,1615, стоп ~1,1200 (6×ATR)`;
  - `🔴 <b>EURUSD!</b>: вход в шорт ({reason}) — …`.
- **Out:** `⚪ <b>EURUSD!</b>: выход во флэт ({reason}) — цена 1,1327` plus `, выход ~X` when a level is given.
- The spread goes in details as «спред DE-US 2 г. −1,68 п.п.».
- The old English caveat line is dropped. The text is in Russian.

### 7. The weekly summary (sent last on the weekly run)

One short message: `paper_report.format_week_summary(conn, today, report, *, html=True, model_failed=False)`.

```
📊 <b>Модель, неделя 26.09–02.10</b>: €101 230 (+1,2% с начала, за неделю +0,4%); смесь 70/30 +0,8%
В портфеле (9): BBD +1,2%, TRMD −0,4%, …            ← «В портфеле: пусто — всё в деньгах» when none
Сигналов за неделю: покупок 2, продаж 1               ← «Сигналов за неделю не было.» when none
Ваш счёт Trading 212: €186 (за неделю +0,3%)          ← as today, omitted when stale/unknown
⚠️ Модель на этой неделе не отработала — покупок не было.   ← only with model_failed
```

- Leave out any part that can't be computed, as `format_week` does today.
- The watchlist is no longer in the weekly messages. It stays in the menu and the analyst.

## Sending

**Weekly run** (`bot._send_weekly`):
- Build the week's signal messages in this order: buys (score order), sales, pending sales, group exits. Then the
  summary.
- Each signal has a stable key: `buy:<order id>`, `sell:<position id>`, `sellpending:<order id>`, `exit:<journal id>`.
- The keys already sent this ISO week are kept in kv (`weekly_sent_<YYYY>-W<ww>`, a JSON list). A signal whose key is
  there is skipped. A key is added only after its message went out.
- The summary is sent last. The week's `weekly_message_…` key is set only after it went out.
- A failed signal send stops the sequence: the summary isn't sent and the week isn't marked. The next run of the
  window resends only what is missing.
- The monthly report still follows a successful summary.
- `paper_report.week_signals(conn, today, report) -> list[tuple[str, str]]` returns (key, html text) pairs.
- `format_week` is removed (replaced by `week_signals` and `format_week_summary`).

**Daily** (`bot._send_closes`): one message per close alert. Each alert is marked alerted
(`positions.mark_alerted(conn, [alert])`) right after its own successful send. A failed send leaves that alert, and
the ones after it, for the next run.

**Trading 212 notices:** unchanged sending; only the text changes.

## Tests (offline)

- **`signal_line`:** every dot, bold name with «!», escaping of a hostile name and details, `html=False` has no
  tags, result with and without extra.
- **Each message type:** exact expected strings for buy (filled/pending, with/without the T212 label), sale
  (profit/loss), pending sale, close alert for each trigger (with quantity → money first; without → percent; no
  price), group exit, T212 new/sold (with and without a result), and carry long/short/flat.
- **Weekly sending:**
  - the order;
  - one message per signal;
  - the summary last;
  - a signal send failing mid-way → the week isn't marked, and the rerun sends only the missing signals and the
    summary;
  - a second run in the same week sends nothing;
  - a quiet week sends only the summary;
  - the monthly report only after the summary.
- **Daily:** two alerts → two messages; the first succeeds and the second fails → only the first is marked.
- **Summary:** each line, the omissions, `model_failed`.
- Update or replace the tests of the removed `format_week` and the old close-alert layout.

## README

Update the Telegram section's message examples to the new style: one example per message type, plus the weekly
summary.

## Amendment

Date: 2026-10-04, after the spec above. From the user, who quoted «от максимума, в модели €10 000» from the buy
example and said "i dont need this info". Binding; where it differs from the spec above, it wins.

1. **Model buy message:** no «от максимума» and no «в модели €X». It ends «…; балл 70, стоп −10%», plus «, нет на
   Trading 212» and «, вход DD.MM по 23,10» when they apply. Example:
   `🟢 <b>GME!</b>: покупка — 2 инсайдера из руководства; CEO среди покупателей; балл 70, стоп −10%`.
2. **No virtual-money amounts for the model anywhere in the signal messages.** A model sale shows only the percent:
   `🔴 <b>GME!</b>: сработал стоп −10% — продано DD.MM, итог <b>−9,8%</b>` (no «(−€980)»); a pending model sale
   likewise.
3. **«от максимума» is dropped from the main line of every signal.** The event for a trailing stop is «сработал стоп»
   (model sales: «сработал стоп −10%»; the user's own position alert: «сработал стоп»). The alert's second detail line
   may still carry the peak level text.
4. **Real positions** (Trading 212 holdings with a quantity) keep the money result as specified.
5. **The weekly summary** keeps the portfolio value line as specified (that line is the summary's point).

Tests and the README examples follow the amendment.
