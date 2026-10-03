# Trading 212 account tracking

Date: 2026-10-01. Requested by the user: "can it track my real time portfolio in trading 212?" → "yes, build it".
It builds on docs/superpowers/specs/2026-10-01-my-portfolio-command.md: `/portfolio` shows the user's own
positions, and `/model` shows the model.

The bot only READS the account. It must never be able to place, change or cancel an order.

## The API (Trading 212 Public API v0, live)

- **Base:** `https://live.trading212.com/api/v0`
- **Auth:** reuse `trading212._auth_headers()`. That is HTTP Basic `base64(KEY:SECRET)`, or a bare key for old keys.
  Never log or print headers.
- **Endpoints used (GET only, this whitelist and nothing else):**
  - `GET /equity/positions` (rate limit 1 per 1 s). It returns a list of position objects:
    - `instrument {ticker, name, isin, currency}`
    - `quantity`, `quantityAvailableForTrading`, `quantityInPies`
    - `currentPrice`, `averagePricePaid` (both in the instrument's currency)
    - `createdAt` (ISO datetime)
    - `walletImpact {currency, totalCost, currentValue, unrealizedProfitLoss, fxImpact}` (in the account
      currency)
  - `GET /equity/account/summary` (rate limit 1 per 5 s). It returns:
    - `id`, `currency`, `totalValue`
    - `cash {availableToTrade, reservedForOrders, inPies}`
    - `investments {currentValue, totalCost, realizedProfitLoss, unrealizedProfitLoss}`
- **Parsing is tolerant:** a missing field becomes None, never a crash. A non-list positions payload or a
  non-dict summary raises ValueError, which is caught by the caller.
- **Errors:**
  - 401 gives `T212Error("ключ Trading 212 не подходит")`.
  - 403 gives `T212Error("ключу Trading 212 не хватает прав: нужны чтение портфеля и счёта")`.
  - 429 is retried once after 5 s, then becomes T212Error.
  - Network errors and other statuses give T212Error with the type name only.
  - No key configured gives T212Error("ключ Trading 212 не задан").

## Decisions

### 1. `t212_account.py` (new module): the client and the sync

- **`fetch_positions(session=None) -> list[T212Position]`** and **`fetch_summary(session=None) -> T212Summary`**.
- **Dataclasses:**
  - `T212Position(t212_ticker, name, isin, currency, quantity, avg_price, current_price, created_at,
    value_eur, cost_eur, pnl_eur, account_currency)`
  - `T212Summary(currency, total_value, cash_free, invested_value, invested_cost, unrealized_pnl, realized_pnl)`
- **Mapping a T212 instrument to the bot's position key:**
  - A `…_US_EQ` instrument becomes the US symbol (reuse `trading212._symbol`: AAPL_US_EQ→AAPL, BRK_B_US_EQ→BRK.B),
    with source None. Prices come from Yahoo daily closes, as for `/bought`; `averagePricePaid` is USD, the same
    currency.
  - Anything else (Frankfurt, London, Amsterdam…): the ticker is the instrument's ISIN and the source is "T212".
    - **Prices:** these positions have no Yahoo symbol. The exits use the T212 price snapshots below, in the same
      currency as `averagePricePaid`.
    - **Insiders:** looked up in the journal by the ISIN (BaFin and FI rows are ISIN-keyed). For a Norwegian ISIN,
      also by the Oslo ticker, via the reverse of norway's ISIN cache, when it is known.

### 2. Sync: `sync(conn, *, fetch=None, notify=None, now=None) -> SyncResult`

Runs every 15 minutes inside the always-on Telegram bot. A failure there is logged and the poll loop goes on.
`telegram_bot.T212_SYNC_SECONDS = 900`; the first sync runs at start. It also runs once in the daily run
(`bot.main`, before `positions.check_exits`, not on a filtered run).

With no key configured, the sync is a quiet no-op: it logs once per process and sends nothing.

**What a sync does:**
1. Fetches positions and the summary.
2. **Stores one price per held instrument per day:** `t212_prices(ticker, date, price)`, with the last
   `currentPrice` of the day replacing earlier ones.
3. **Stores one account snapshot per day:** `t212_equity(date, total_value, invested_value, invested_cost,
   cash_free, currency)`, the last of the day.
4. **New holding:** a T212 holding with no open bot position of `origin = 't212'` for its key.
   - If an open manual (`/bought`) position exists for the same key, it is converted: `origin` becomes 't212',
     and quantity and entry come from T212. No notification.
   - Otherwise a position is opened with:
     - `origin='t212'`, `quantity`, `t212_ticker`, `currency`;
     - `entry_price = averagePricePaid` and `opened_at = createdAt` date (today when missing);
     - insiders from the journal;
     - `stop_pct` computed as `/bought` does, from the price history.
     - Notify «📥 Вижу в Trading 212: GME — 10 шт. по 23,10 USD. Слежу: стоп, продажи инсайдеров, новости.»
5. **Held position, quantity or average price changed:** update `quantity` and `entry_price` (adding or trimming
   shares). No notification.
6. **Sold:** an open `origin='t212'` position missing from T212 is closed with `close_reason='продано в Trading
   212'`. Notify «📤 GME больше нет в Trading 212 — слежение закрыто.»
7. **First sync ever** (kv `t212_synced_once` unset): one combined message «📥 Слежу за вашими позициями в Trading
   212 (N): GME, INBX, …» instead of one message per holding. Then the flag is set.
8. **Notifications** go through `notify` (default `telegram_notify.send_text`) and are best-effort: a failed send
   doesn't undo the sync.
9. **A sync is atomic:** the DB changes are committed only when the whole fetch succeeded. A failed fetch changes
   nothing (no false "sold" on an API error).

**`SyncResult`** has `opened`, `updated`, `closed`, `error: str | None`, and `at` (the timestamp).

### 3. DB (`_ADDED_COLUMNS` and SCHEMA)

- `positions` gets: `origin TEXT DEFAULT 'manual'`, `quantity REAL`, `t212_ticker TEXT`, `currency TEXT`.
- New tables:
  - `t212_prices (ticker TEXT, date TEXT, price REAL, PRIMARY KEY (ticker, date))`
  - `t212_equity (date TEXT PRIMARY KEY, total_value REAL, invested_value REAL, invested_cost REAL,
    cash_free REAL, currency TEXT)`
- `positions.Position` gets the new fields (defaults keep old constructions working).

### 4. Prices for exits

`positions.daily_closes(ticker, source)` falls back to the `t212_prices` series when there's no Yahoo symbol and the
source is "T212". The function needs `conn` for that; add it, or a `t212_prices` reader passed via `closes_fn`.
Keep `check_exits`' seams working. The exit rules are unchanged: they apply to every open position, manual or
T212.

### 5. `/bought` and `/sold` with T212 positions

- `/bought X` where X is held in T212 replies «X уже отслеживается из Trading 212.»
- `/sold X` for a T212 position replies «X отслеживается из Trading 212: продайте там — бот увидит продажу сам.»
  The position is not closed.

### 6. `/portfolio` (extends the my-portfolio view)

**Live call.** `/portfolio` does one live `fetch_positions` and `fetch_summary` (no full sync, but it stores the
day's prices and equity like a sync does).

**«💼 Trading 212»** comes first:
- An account line: «Счёт: €X · вложено €Y · P/L ±€Z (±W%) · свободно €C».
- Per T212 position: «• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30», then the existing
  status lines from the my-portfolio view:
  - stop level and distance;
  - the insiders line;
  - «модель тоже держит».

**«✍️ Вне Trading 212»:** the manual positions, as the my-portfolio view shows them.

**If the live call fails,** show the stored T212 positions with «⚠️ Trading 212 не ответил (причина) — данные на
HH:MM последней синхронизации». A 403 or no key shows the reason and a one-line hint: «Создайте в Trading 212 →
Настройки → API ключ только для чтения (Portfolio, Account data) и положите в .env».

The footer and the empty text are as in the my-portfolio view.

### 7. The weekly message

`paper_report.format_week` gets one line before the portfolio line, when `t212_equity` has a value today or on/before
today: «Ваш счёт Trading 212: €X (за неделю ±Y%)». The week change is today vs the value on/before today-7, left out
when unknown.

### 8. The analyst

`analyst.py portfolio`'s «ВАШИ ПОЗИЦИИ» section includes the T212 holdings with quantity and P/L from the stored
data (no live call needed). It also includes the latest `t212_equity` line.

### 9. CLI: `python t212_account.py --check`

It prints «Trading 212: доступ есть, позиций N, валюта EUR» or the error reason, and never values that identify
the account or any secret. `python t212_account.py --sync` runs one sync with notifications off.

## Safety tests (mandatory)

- **Source inspection** of t212_account.py and trading212.py:
  - no `.post(`, `.put(`, `.patch(`, `.delete(` or `requests.request(`;
  - every URL string is one of the whitelisted read paths (instruments, positions, account/summary);
  - no path containing `/orders` other than history (which isn't used here: so none at all in t212_account.py).
- **The client** calls only `session.get` (a stub session records the calls).

## Tests (offline, stub sessions and fetchers)

- **Parsing:** the full and the minimal payload, missing fields, and a bad payload type.
- **Error mapping:** 401, 403, 429 (retry once, then error), timeout, no key.
- **Instrument mapping:** a US instrument, a class share, a non-US instrument by ISIN, and a Norwegian ISIN mapped to
  its Oslo ticker for insiders.
- **Sync:**
  - first sync (one combined message);
  - a new holding (message, position fields);
  - quantity/avg change (update, no message);
  - sold (closed, message);
  - API failure (nothing changes, no messages);
  - a manual position converted;
  - the day's price and equity rows replaced within a day;
  - no key (no-op).
- **Exits:** a T212 non-US position priced from `t212_prices` triggers the trailing stop. A US one uses Yahoo closes
  (stubbed).
- **`/bought` and `/sold`** replies for a T212-held ticker.
- **`/portfolio`:** live success (sections, the account line), live failure (the stored data with the warning), and
  403/no key (the hint).
- **The weekly line:** shown, the week change, and left out without data.
- **The analyst portfolio section** with T212 rows.
- **The telegram_bot loop:** the sync runs at start and then every 900 s (fake clock), and a sync exception doesn't
  stop polling.
- **bot.main:** the sync runs before check_exits on a full run, and not on a filtered run.

## README

A «Trading 212» section covers:
- what is read (positions and account summary, read-only), how often (every 15 minutes plus the daily run), and the
  notifications;
- that `/portfolio` is live;
- the read-only key recommendation (create a key without order permissions);
- `--check`;
- that the bot never places orders.

## Amendment (coordinator rulings, 2026-10-03)

Three rulings made after the first implementation. They change §2 (sync), §3 (DB), §6 (`/portfolio`), §8 (the
analyst) and §9 (CLI). Where they differ from the text above, they win.

### R1. Holdings that pre-date tracking ("legacy") bring no burst of close alerts

Connecting an account that already holds positions must not fire «год в позиции», «стоит на месте» and stops from
old highs on day one. The bot's rule clock and its stop start when tracking starts, not at the original purchase.

**Which holdings are legacy.** The sync before which no position of origin 't212' was ever stored (open or closed)
is the one that starts tracking. Every holding it opens was in the account before the bot looked. This keys on the
stored positions, not on the notification flag (see R3).

**A legacy holding:**
- `opened_at` is the date of that sync (was: createdAt). The 365-day rule and dead money (60 business days, result
  below +5% against `entry_price`) count from `opened_at` as they do for any position, so they count from the
  tracking start.
- `entry_price` stays `averagePricePaid`. The result shown is the real one.
- `stop_base` is `currentPrice` at that sync (the last close of the price history when Trading 212 gives no price).
- `stop_pct` is sized as usual, from the price history before tracking started.

**The stop.** `positions._stop_and_peak` uses `stop_base`, when it is not None, in place of `entry_price` as the
floor of the peak: the peak is the highest of `stop_base` and the completed closes since `opened_at`. A holding
that is deep under water does not hit its stop on day one. A fall of `stop_pct` from the peak since tracking began
fires the trailing stop.

**A holding first seen by a later sync** is a normal new position, as in §2: `opened_at` is its createdAt date
(today when missing), `entry_price` its average price, `stop_base` NULL.

**A `/bought` position taken over** keeps its own `opened_at` and has no `stop_base`: the bot has watched it since
the `/bought`.

**DB (§3).** `positions` gets two more nullable columns in `_ADDED_COLUMNS`, and `Position` the matching fields:
- `stop_base REAL`: the floor of a legacy holding's stop;
- `t212_created TEXT`: the date Trading 212 says the holding was bought. It is shown, and no rule uses it.

**The first-sync message (§2.7)** gets a second line when a holding it lists is legacy:
«Для уже купленных бумаг правила выхода считаются с сегодняшнего дня.» When a silent sync stored those holdings
on an earlier day (R3), the line names that day: «…считаются с 01.10.»

**What is shown (§6, §8).** `/portfolio` and the analyst show the owner's own figures, whatever the rules count
from:
- the result is the price against the average price paid;
- the Trading 212 line ends with the days since Trading 212's purchase date when it is known:
  «• GME — 10 шт., средняя 23,10, сейчас 24,05 USD (+4,1%), €+8,30, 5 дн.»;
- the stop line of a legacy holding reads from its floor: «стоп 45,00 (−10% от максимума 50,00), до стопа 10,0%».

### R2. A message names only what is really watched for its holding

The «📥 Вижу в Trading 212: …» notification ends with what the bot watches for that holding:
- a US holding: «Слежу: стоп, продажи инсайдеров, новости.»;
- a holding keyed by its ISIN has no news feed: «Слежу: стоп и срок.»;
- «…, а также продажи инсайдеров» is added only when the journal matches insiders for it (BaFin and FI by the
  ISIN, Oslo through the mapped ticker);
- «…, а также новости» is added only for a company with an Oslo listing, whose headlines are read there;
- both: «Слежу: стоп и срок, а также продажи инсайдеров и новости.»

The first-sync message lists the holdings and says when the rules start. It makes no claim about news or insiders.

### R3. A silent sync does not use up the first-sync message

- `sync(conn, *, fetch=None, notify=None, now=None, closes_fn=None, silent=False)`. A silent sync stores everything
  and sends nothing: `notify` is not called.
- `python t212_account.py --sync` and a `bot.py --no-telegram` run sync silently.
- The flag `t212_synced_once` is set only by a sync that sent the combined first message, or tried to with the
  notifications on (a failed send still counts). A silent sync does not set it, and neither does a sync of an
  account that holds nothing (there is no message to send).
- The first notifying sync after a silent one sends the combined message and lists every Trading 212 holding the
  bot tracks, not only what that sync opened.
- The legacy rule (R1) does not depend on this flag.

### Tests added for the rulings

- **R1:**
  - a legacy holding bought long ago and deep under water raises no close alert at the first check;
  - later, a fall of `stop_pct` from the peak since tracking began fires the trailing stop;
  - the year and dead money count from the tracking start (dead money's result from the average price);
  - a holding first seen after the first sync opens at its Trading 212 date, with no `stop_base`;
  - a `/bought` position taken over at the first sync keeps its clock;
  - the legacy rule keys on stored positions, not on the flag;
  - `/portfolio` and the analyst show the real result and the days since the purchase.
- **R2:** the message for a US holding, an ISIN holding with and without matched insiders (BaFin, FI), an Oslo
  company with and without a signal; the first message claims nothing it does not watch.
- **R3:**
  - a silent sync sends nothing and leaves the flag unset;
  - the next notifying sync lists every tracked holding and sets the flag;
  - a failed send still sets it, and an empty account does not;
  - `--sync` and `--no-telegram` are silent.

## Amendment 2 (coordinator rulings after the whole-change review, 2026-10-03)

The review found the change spec-compliant, read-only and secret-safe, and asked for fixes. These rulings change
§1 (the key of an instrument), §2 (sync), §4 (prices for exits), §5 (`/bought`, `/sold`), §6, §7, §9 and the
safety tests. Where they differ from the text above, they win. Not done, by ruling: M5 (a holding that can't be
keyed) and a code change for M7 (stock splits).

### I1. A sale is believed only when the list adds up; a returning holding is restored

**a) Closes need a list that agrees with the summary of the same sync.**
- `summary.invested_value` known and above 0 with an EMPTY positions list is a bad answer: nothing changes, and
  `SyncResult.error` is «пустой список позиций при вложенных средствах».
- A list whose summed `value_eur` differs from `invested_value` by more than max(2 %, €5) applies opens and
  updates but NO closes. One line is logged (with the relative difference, no amounts), and `SyncResult.held`
  lists the positions not closed with `SyncResult.note` «список позиций не сходится со сводкой счёта».
- With no value to check against (the summary has no `invested_value`, or a holding has no `value_eur`), a
  position is closed only when it was missing in two consecutive successful syncs. The kv entry `t212_missing`
  (JSON, position ids) keeps who was missing at the last such sync. A sync whose list does not add up is no
  evidence either way: it neither counts as a miss nor clears one.
- A genuinely empty account (nothing invested, nothing listed) closes what was held.

**b) A returning holding is restored, not re-created.** When a holding is not tracked and a closed position of
origin 't212' has the same `t212_ticker` and the same `t212_created`, that row is reopened: `closed_at` and
`close_reason` are cleared; `opened_at`, `stop_base`, `stop_pct`, `insiders` and `close_alerted_at` are kept;
quantity and average price are updated. No «📥» is sent. A legacy holding (R1) therefore does not come back as a
position bought years ago. Without a known purchase date nothing tells the same holding from a new purchase, so
no row is restored. An open `/bought` position in the same name is taken over first.

### I2. The market symbol of a US instrument

Trading 212's `_US_EQ` codes are old ones for about one instrument in five (FB_US_EQ is Meta, PCLN_US_EQ Booking,
UTX_US_EQ RTX). This replaces §1's "a `…_US_EQ` instrument becomes the US symbol".

- **The symbol.** `position_key(t212_ticker, isin, conn)` takes a US instrument's market symbol from the cached
  instrument list, `t212_instruments.short_name` for that Trading 212 ticker. With no row there (or an empty
  name) it falls back to `trading212._symbol`.
- **The price source is checked when the position is opened.** Yahoo's history is asked for under that symbol.
  When Yahoo has no completed close for it, or its last completed close differs from Trading 212's `currentPrice`
  by more than 20 %, the holding is keyed by its ISIN with source "T212" instead and priced from the sync's own
  day prices. With no `currentPrice` there is nothing to compare, and a symbol Yahoo knows is taken. With no ISIN
  to fall back on the symbol stays.
- **Matching.** A tracked position is found on the list by the `t212_ticker` stored with it, never by a key
  derived again, so an instrument whose key changes (a new market symbol, Yahoo starting or stopping to know it)
  stays one position. Only a position stored without an id is found by its key. The day's price is stored under
  the position's own ticker. One ISIN held on two exchanges stays one position, whichever is listed first.
- `/portfolio`'s live call matches the same way. It stores the day's price of a holding it does not track yet only
  when the key is certain (an ISIN); a US one is keyed by the sync.
- A holding keyed by its ISIN is named by its Trading 212 symbol also when its code is a US one
  (GME_US_EQ → GME).

### I3. A sync that keeps failing is not silent

- **A day without a sync.** When a sync has got through before and none has for 24 hours, a failing sync sends
  «⚠️ Trading 212 не отвечает уже сутки (причина): позиции не обновляются.» once per day. The day is marked in kv
  (`t212_silent_<date>`) once the message went out, so one that did not go out is tried again at the next sync. It
  comes from the bot loop and from the daily run alike (both call `sync`); a silent sync never sends it. The
  reason is the failure's own: an HTTP status, a network error's type, a key that no longer fits or is gone, a bad
  answer.
- **`SyncResult.error_kind`** is the failure's kind (`T212Error.kind`, or "answer" for a bad answer).
- **Hourly after a key error.** After a 401 or a 403 the bot loop syncs again in an hour
  (`telegram_bot.T212_KEY_RETRY_SECONDS = 3600`), not in 15 minutes. Any other failure keeps the 15 minutes.
- **A stale day price is not a price (§4).** A stored Trading 212 day price older than 3 days
  (`positions.T212_STALE_DAYS`) is not returned by `last_close`: the stop check is skipped for that holding, as
  with «цена недоступна». The history (`daily_closes`) is unchanged. `/portfolio`'s stored view and the analyst
  show such a price as «цена на DD.MM: 24,05 USD», with no stop line, and leave it out of the average result.
- **A stale account value is not the account's (§7).** The weekly line is left out when the newest `t212_equity`
  row is more than 3 days older than today. The week change is left out when the snapshot it compares with is more
  than 3 days older than a week ago.

### Minors

- **M1. The reach of the safety tests.**
  - Trading 212's host, the URL constants, `_auth_headers` and the names of the key's variables are referenced
    only in `trading212.py` and `t212_account.py`, across all the project's code (tests excluded).
  - Those two modules import no network library other than `requests`, and contain no `.send(`, `.request(`,
    `getattr(` or `__import__(` (on top of `.post(`, `.put(`, `.patch(`, `.delete(`).
  - The README says what the tests prove: an inspection of the source text and of the client's behaviour, not a
    proof of impossibility. The main protection is a key with no orders permission.
- **M2. A US holding with no matched insiders** is announced with «Слежу: стоп, срок и новости.» «Продажи
  инсайдеров» is named only when the journal matched insiders, for a US holding too (this narrows R2).
- **M3. A price of zero is not a price.** A day price is stored only when it is above 0, and only such rows are
  read back. A `currentPrice` of 0 or None never becomes a `stop_base` and is shown as no price.
- **M4. A take-over needs the same listing.** A `/bought` position with source NORWAY or SWEDEN is never taken over
  by a US instrument under the same letters. That holding is tracked beside it, keyed by its ISIN; with no ISIN
  it is left untracked (and does not hold back a sale). A holding keyed by its ISIN takes over a position
  bought under the same ISIN whatever source named it, and never a position keyed by a ticker.
- **M6. `/bought` and `/sold` "already tracked" matching (§5)** is by the position's ticker or by the symbol of
  its stored US code only (Meta, held as FB_US_EQ and tracked as META, answers to both). A holding keyed by its
  ISIN is not matched by the name it is shown under: 475 non-US instruments share a US company's symbol.
- **M8. `--check` (§9)** prints a second line: «Ключи: США — 7, ISIN — 5; с ценой — 12 из 12; список и сводка
  сходятся». It counts the holdings keyed as US, as ISIN and (when any) neither, and those with a usable price.
  It also says whether the list adds up to the summary (I1) — «сходятся», «расходятся на N%» or «не сверить» —
  because a sale depends on it. Nothing in it identifies the account.
- **M9.** `telegram_bot`'s poll-error line redacts the bot token (`telegram_notify._redact`): the requests error
  text carries the URL.
- **M10.** `HELP_TEXT` says «/portfolio — ваш счёт Trading 212 и позиции /bought, /model — модельный портфель.»
  The README says that a stock split can cause one false stop alert for a holding priced by Trading 212.

### Tests added for Amendment 2

- **I1:** an empty list with money invested (no closes, no messages, nothing stored); a partial list (opens and
  updates, no closes, the note, one log line without amounts); the 2 % / €5 tolerance; a genuinely empty account
  closes; with no summary value a sale needs two syncs in a row (a holding back in between, a failed sync in
  between, a holding without its value, a list that disagrees in between); a holding that vanishes and returns
  keeps its clock, its `stop_base` and its used alert, gets no «📥» and no burst; bought back on another day, or
  with no purchase date, it is a new position.
- **I2:** FB_US_EQ with short_name META is keyed META; no instrument row or an empty name falls back to the code;
  Yahoo with no close, a price more than 20 % away or only today's bar keys by ISIN; within 20 % keeps the
  symbol; a stored position is found by its `t212_ticker` after its key changed; one ISIN on two exchanges in
  either order; a position stored without an id learns it.
- **I3:** the warning after 24 hours, once a day, for each kind of failure, not from a silent sync, retried when
  it did not go out, and ended by a sync that gets through; the hourly retry after 401 and 403; a day price
  older than 3 days gives no price and no stop, and reads «цена на DD.MM»; the weekly line and its week change
  with stale snapshots.
- **Minors:** the detector flags `.send(`, `getattr(`, `__import__(` and network imports; the reach across the
  project; the watch text of a US holding with and without insiders; a zero price; the take-over rules by
  listing; `/bought` and `/sold` matching; the second line of `--check`; the redacted poll error; the help text.

### K1. A Yahoo outage does not key a US holding by its ISIN (ruling on I2, 2026-10-03)

The price history cannot tell "Yahoo does not know the symbol" from "Yahoo is unreachable": both return nothing.
Under I2 an outage at the moment a US holding was first seen keyed it by its ISIN for good, without its US
insiders and its news. This narrows I2's check and widens §4's pricing.

- **(a) The key.** When Yahoo returns NO history for a US instrument's market symbol at open, the holding is keyed
  by the US symbol anyway: the instrument list vouches for the symbol. Only a Yahoo series that EXISTS — it has a
  completed close — and whose last completed close differs from Trading 212's `currentPrice` by more than 20 %
  keys the holding by its ISIN with source "T212".
- **(b) Pricing of any `origin='t212'` position** (`positions._pricing`, the defaults of `check_exits` and
  `position_status`): Yahoo's last close and Yahoo's closes when Yahoo has them; otherwise the day prices the
  sync stored for that holding (the same instrument, the same currency). The price and the history fall back
  each on its own. So a US-keyed holding Yahoo does not know is still priced, and its stop still works.
  - The stored history obeys the completed-bars rule: today's stored price is not a close.
  - The 3-day staleness rule stays: a stored price older than 3 days is no price.
  - A `/bought` position never reads the account's day prices. Seams handed to `check_exits` are used as given.
  - A new holding with no Yahoo history sizes its stop from the stored day prices it has.
- **Tests:** an empty Yahoo answer and a today-only bar keep the symbol; a series more than 20 % away keys by
  ISIN; a US-keyed holding with no Yahoo data trips its trailing stop on the stored day prices (peak from
  completed stored days); the price and the history fall back independently; a stale stored price gives no
  price; a `/bought` position is not priced from them; handed-in seams are not second-guessed.

### K2. A key removed on purpose is not an outage (ruling on I3, 2026-10-03)

Under I3 a key that was removed after the account had been tracked produced the daily «не отвечает уже сутки»
warning for ever. Removing the key is the owner's decision.

- When no key is configured and a sync had got through before, a sync sends ONE message: «Ключ Trading 212 убран —
  слежение за счётом остановлено. Позиции из Trading 212 остаются в /portfolio по последним данным.» It is marked
  in kv (`t212_key_removed`) once it went out, so a message that did not go out is tried again at the next sync.
- After that the sync stays quiet: no daily warning for a missing key.
- When a key is configured again the mark is cleared — by a sync that gets through, and by one that reaches
  Trading 212 and fails. A later removal is then said again.
- A silent sync says nothing and marks nothing.
- With no key and no sync ever having got through, nothing is said (the quiet no-op of §2).
- API errors — 401, 403, timeouts, other statuses, a bad answer — keep I3's daily warning.
- **Tests:** the message once over 30 days of syncs with no key; nothing without an earlier sync; the mark
  cleared by a sync that gets through and by one that fails with a key; a silent sync; a message that did not go
  out; the daily warning for 401, 403, a timeout and a 502.

## Amendment 3 (coordinator rulings after the final review, 2026-10-04)

Ten small fixes, F1-F10. They narrow K2 (F1), I1 (F3, F5, F6), I3 (F8, F9), §6 (F2), §4 and K1 (F7) and add one warning (F4). Where
they differ from the text above, they win. `trading212._setting` is unchanged.

- **F1.** «Ключ убран» no longer repeats daily in the two-process setup: `_say_key_removed` is quiet while any sync got through in the last hour (some process still holds a key), and the mark is cleared only by a sync of a process that was itself keyless before; README: restart the Telegram agent after changing the key.
- **F2.** `/portfolio`'s live call stores a day price only under a position's own ticker and skips an untracked holding whose key is an open position's ticker (one ISIN held on two exchanges), in either order of the list; a ticker gets one price, the first listing's.
- **F3.** The empty-list guard of I1 refuses only when `invested_value` is above `LIST_TOLERANCE_MONEY` (€5): a float residue after selling everything is an empty account and no longer blocks the close for ever.
- **F4.** A list that has disagreed with the summary for 24 h (a run of disagreeing syncs, kept in kv) is not silent: once per calendar day (per-day mark `t212_gap_<date>`) «⚠️ Trading 212: список позиций не сходится со счётом уже сутки — продажи не отмечаю.», only after a sync whose list added up (`t212_list_ok`), never from a silent sync; a sync whose list adds up ends the run, one that cannot be checked is no evidence either way.
- **F5.** `_list_gap`: a walletImpact currency different from the summary's means "cannot check" (`agrees` is None): the list falls to the two-sync rule and is not a disagreement (a currency not told is no mismatch); `SALE_UNCONFIRMED` now reads «список нельзя сверить со сводкой…».
- **F6.** The two-sync rule keeps the time of each position's first miss (`t212_missing` is now JSON `{id: epoch}`; an old list of ids reads as misses of that moment): the close needs the second miss at least 10 minutes (`CONFIRM_AFTER`) after the first, because the daily run and the bot loop are separate processes.
- **F7.** `positions._pricing` (K1 b): when both Yahoo's last completed close and a fresh stored Trading 212 day price exist for an `origin='t212'` position and differ by more than 20 % (`positions.YAHOO_TOLERANCE`), the stored Trading 212 series -- price and history -- is used; a Yahoo series that appears later for an old code does not drive a stop. Yahoo's history is fetched once per position; handed-in seams and `/bought` positions are unchanged.
- **F8.** A sync that raises inside `_apply` (or its commit) counts as a failed sync for the 24-hour warning, recorded like a fetch failure (reason «сбой синхронизации: <ExceptionType>», never the text, `error_kind` "internal"); the error still goes up to the caller after the rollback.
- **F9.** Warning marks (the 24-hour warning, «ключ убран», the F4 warning) are written before the send (`_say_once`): a locked database or a failed mark write sends nothing and raises nothing, so a warning is not repeated at every sync; a send that did not go out gives the mark back for a retry, and if even that fails the mark stays.
- **F10.** Stale text: `telegram_bot.py`'s comment no longer says a holding is found by the name it is shown under (matching is by the position's ticker or the stored `t212_ticker` only, M6), and the README's split note applies to any holding priced by Trading 212 (ISIN-keyed, or a US one Yahoo has nothing for or a foreign series for).
