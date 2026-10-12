# «Sequence trading» (an averaging-down grid) on EURUSD and gold (2026-10-12)

The Knockoutforex white paper the user sent describes baskets of trades with no stop, added to as price goes
against them and closed together at a small profit. cfd/research_grid.py runs a plain version: a further trade
every half a day's range against the basket, the basket closed at a profit of 1 % or 0.25 % of the account, each
further trade the same size or 1.5 times the last, the direction alternating; IC Markets' costs; the broker's
margin (1:30, gold 1:20) and its stop-out at 50 % as the only limit. Hourly bars, EURUSD 2012-2026, gold
2010-2026, a 10,000 account. An illustration with fixed settings, not a search.

| market | target, size | sequences won | first stop-out | account before -> after | stop-outs | end, x start |
|---|---|---|---|---|---|---|
| EURUSD | 1 %, x1 | 350 | 2012-05 | 16,122 -> 4,839 | 7 | 0.01 |
| EURUSD | 1 %, x1.5 | 1,107 | 2012-09 | 32,677 -> 8,740 | 14 | 0.01 |
| EURUSD | 0.25 %, x1 | 945 | 2014-07 | 18,482 -> 5,885 | 5 | 0.01 |
| EURUSD | 0.25 %, x1.5 | 2,858 | 2014-07 | 34,331 -> 10,384 | 7 | 0.56 |
| gold | 1 %, x1 | 230 | 2010-10 | 21,091 -> 7,511 | 6 | 0.00 |
| gold | 1 %, x1.5 | 310 | 2010-04 | 14,741 -> 3,908 | 7 | 0.00 |
| gold | 0.25 %, x1 | 596 | 2010-11 | 12,120 -> 2,517 | 4 | 0.01 |
| gold | 0.25 %, x1.5 | 1,135 | 2011-07 | 21,203 -> 7,136 | 4 | 0.00 |

Every version wins nearly every sequence and grows fast -- for five months to two and a half years. Then one move
that does not come back takes about 70 % of the account in a single stop-out, and the next ones take most of the
rest. Seven of the eight accounts end with 1 % or less of what they began with; the eighth with 56 %.
