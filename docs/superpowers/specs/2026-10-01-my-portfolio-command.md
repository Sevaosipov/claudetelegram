# /portfolio shows your own positions

Date: 2026-10-01. Requested by the user: "how should i send /bought indicator? as well as /portfolio should then show
only bought stocks". It changes the Telegram commands of docs/superpowers/specs/2026-09-30-model-portfolio-and-analyst-design.md
§7 and the 2026-10-01 weekly spec's help line.

## Decisions

1. **`/portfolio` shows only the user's own positions** (what they recorded with `/bought`), with live status.
   `/positions` is an alias for the same view.
2. **`/model` takes over the old `/portfolio` behaviour:** the model portfolio summary
   (`paper_report.format_summary(conn, today, html=True)`, with its existing failure handling). The Friday message
   is unchanged.
3. **`/bought` accepts Oslo and Stockholm listings** written the Yahoo way (`EQNR.OL`, `VOLV-B.ST`). They are stored
   the way the signal tables know them: the bare ticker plus the source (`EQNR` with source `NORWAY`). Then pricing,
   insiders and the close alerts all work.
   - **Why:** today `/bought EQNR.OL` becomes `EQNR-OL` on Yahoo, a wrong or missing quote.
   - The same mapping applies to `/sold EQNR.OL`.
   - Use one helper shared with analyst.py's `_split_venue`. Move it into `positions.py`, or a small shared place
     both import, and make analyst use it.
4. **The analyst** sees the user's positions. `analyst.py portfolio` prints «ВАШИ ПОЗИЦИИ (/bought):» with the same
   per-position facts first (plain text, no HTML), then the model part as today. analyst_method.txt's step for
   portfolio questions says the portfolio output covers both the owner's own positions and the model.

## The view

**`positions.position_status(pos, today, *, closes_fn=None, price_fn=None) -> dict`** is pure apart from the two
seams, which have the same defaults as `check_exits`. It returns:
- `last`: the price, or None;
- `result`: `last/entry_price - 1`, or None;
- `days`;
- `peak`: the max of `entry_price` and the completed closes since `opened_at`;
- `stop_pct`: the stored one, else the closes before the open, else the model fallback;
- `stop_level` = `peak * (1 - stop_pct)`;
- `to_stop`: `last/stop_level - 1`, or None.

The peak/stop computation is the one `_trailing_stop` uses. Extract it into one helper both call; don't duplicate it.

**`telegram_notify.format_my_portfolio(rows, *, html=True) -> str`**, where `rows` is a list of
`(Position, status dict, model_holds: bool)`:
- **Header:** «💼 Ваш портфель — N позиц(ия/ии/ий)», in bold.
- **One block per position, oldest first:**
  - «• GME: вход 23,10 (01.10), сейчас 24,05 (+4,1%), 3 дн.»;
  - «   стоп 21,65 (−10% от максимума 24,05), до стопа 10,0%»;
  - «   слежу за продажами: Ryan Cohen, …» (only when there are insiders);
  - «   модель тоже держит» (only when MODEL-S/MODEL-C has an open position in it; compare by ticker, coins via
    `crypto.symbol_of`).
  - With no price: «сейчас — цена недоступна», and no stop line.
- **Footer:** «Средний результат: +X% по N позициям» (the equal-weighted mean of the known results). Then the line
  «Сигнал на продажу придёт сразу. /sold TICKER — закрыть, /model — модельный портфель.»
- **Empty:** «Ваших позиций нет. Купили? /bought TICKER [цена] — например /bought GME 23.10. Модельный портфель:
  /model.»
- Prices are formatted the Russian way (comma decimal, space thousands) and every dynamic string is escaped.

**`format_positions`** (the old one-line-per-position view) is removed if nothing else uses it.

## Telegram text

- `POSITIONS_USAGE`:
  - «/bought TICKER [цена] — отметить покупку (без цены — последнее закрытие); биржи Осло/Стокгольма: EQNR.OL,
    VOLV-B.ST»;
  - «/sold TICKER — отметить продажу»;
  - «/portfolio — ваши позиции».
- `HELP_TEXT`:
  - «/portfolio — ваши позиции (/bought), /model — модельный портфель.»;
  - «Сводка модельного портфеля приходит по пятницам.»
- The module docstring is updated to match.

## README

The Telegram section documents `/bought` usage (examples: `GME 23.10`, `BTC`, `EQNR.OL`), `/sold`, `/portfolio` = your
positions, and `/model` = the model.

## Tests (offline, stubbed seams)

- **`position_status`:** the peak includes the entry and completed closes only; the stored stop; a missing stop →
  computed from the closes before the open, or the fallback; `to_stop`; no price.
- **The shared peak/stop helper:** `_trailing_stop` still behaves exactly as before (the existing tests pass).
- **`format_my_portfolio`:**
  - the lines and the plural forms;
  - the insiders line only when present;
  - «модель тоже держит» only when true, including a coin;
  - no-price;
  - the average;
  - the empty text;
  - HTML escaping.
- **telegram_bot:**
  - `/portfolio` and `/positions` send `format_my_portfolio` (with stubbed positions and status);
  - `/model` sends the model summary;
  - `/bought EQNR.OL 150` stores ticker `EQNR`, source `NORWAY`, entry 150;
  - `/sold EQNR.OL` closes it;
  - `/bought VOLV-B.ST` → (`VOLV-B`, `SWEDEN`);
  - a US ticker and `BTC` are unchanged.
- **analyst `portfolio`:** prints the user's positions section first, then the model.
