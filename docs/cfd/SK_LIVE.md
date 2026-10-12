# The SK sequence on paper

Started 2026-10-12 at the user's request, after the tests (REPORT_ABC.md, REPORT_TPR.md): 150 versions of the
method on history, none qualifying, and the best of them failing on 2020-2026 (-0.06R a trade on 350 trades). It
runs on paper so the user can watch the method work in real time; the tests are the expectation.

- **Markets:** EURUSD and XAUUSD, from the user's MetaTrader 5 terminal (Yahoo when it is shut).
- **Bars:** four-hour, UTC 00/04/08/12/16/20, completed ones only.
- **The rule** (cfd/research_abc.py, the tested code itself): point 0 is the highest high or lowest low of 300
  bars; A the extreme since, beyond the last swing point confirmed before point 0 (a structure shift); once price
  has retraced 38.2 % of 0-A, a **limit order at the 61.8 % retracement, stop at point 0, target at A**. A new
  extreme beyond A, or beyond point 0, cancels the order. One trade at a time a market and side.
- **Costs in the results:** 0.8 pips on EURUSD, $0.30 an ounce on gold, 0.010 % / 0.020 % a night; no trade when the
  cost is over a quarter of the risk.
- **Messages:** the order (entry, stop, target), then one line when it is filled, cancelled, or closed. They follow
  the four-hour bars, so a fill or a close is told up to four hours after it happened; an order placed with its
  stop and target at the broker needs no faster news.
- `/sk` shows the results since the start and the orders in force; `/sk off` mutes the messages.
- **Stop rule (cfd/paper.py):** a market whose closed trades sum to -10R or worse gets no new orders.
