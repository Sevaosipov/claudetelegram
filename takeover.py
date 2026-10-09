"""takeover.py: is a company being bought out?

A buy signal on a takeover target is a false one. The bot reads a 13D as an activist arriving and a price
jump as momentum; in a buyout the 13D is the acquirer's and the jump is the offer's premium, after which the
price stands just under the offer and has nowhere to go. So the week's pick leaves such a company out.

What tells it, from the company's own filing list at SEC EDGAR (data.sec.gov/submissions) over WINDOW_DAYS:

  * a form only a target has -- a third party's tender offer (SC TO-T), the target's answer to one
    (SC 14D9, SC14D9C), a merger proxy (DEFM14A, DEFM14C), a going-private statement (SC 13E3): a
    **target**;
  * several business-combination communications (form 425). Both sides of a merger file those, so they
    say only that a deal is on. With a one-day jump of the price of JUMP or more in the window -- the
    offer's premium -- the company is the **target**; without one it is the buyer or a merger of equals: a
    **deal**, which is marked on the signal and not left out.

Only companies that file with the SEC can be checked; anything else (a coin, an Oslo listing) is not one.
"""
from __future__ import annotations

import datetime as dt
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

WINDOW_DAYS = 180
TARGET_FORMS = ("SC TO-T", "SC 14D9", "SC14D9", "DEFM14", "SC 13E3")     # prefixes: SC14D9C, DEFM14A, .../A


def _target_form(form: str) -> bool:
    return form.startswith(TARGET_FORMS)
DEAL_FORM = "425"
DEAL_MIN = 3                    # this many 425s say a deal is on (one may be a stray reference)
JUMP = 0.15                     # a one-day rise of the close that large is an offer's premium
TARGET, DEAL = "target", "deal"
US_SOURCES = (None, "SEC", "SEC13DG", "HOUSE", "SENATE")


@dataclass(frozen=True)
class Finding:
    kind: str                   # TARGET | DEAL
    text: str                   # what was seen, in Russian


def recent_forms(ticker: str, today: dt.date, *, session=None, cik_lookup=None) -> Counter | None:
    """The forms the company filed in the last WINDOW_DAYS, counted; None when the ticker has no CIK."""
    import cik_map
    import sec_edgar
    cik = (cik_lookup or cik_map.CikMap()).cik(ticker)
    if not cik:
        return None
    session = session or sec_edgar.new_session()
    resp = session.get(sec_edgar.SUBMISSIONS_URL.format(cik=str(cik).lstrip("0") or "0"), timeout=30)
    resp.raise_for_status()
    recent = resp.json().get("filings", {}).get("recent", {})
    since = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    return Counter(form for form, day in zip(recent.get("form", []), recent.get("filingDate", [])) if day >= since)


def biggest_jump(closes: list[tuple[str, float]], today: dt.date) -> float:
    """The largest one-day rise of the close in the last WINDOW_DAYS, as a fraction (0 with no history)."""
    since = (today - dt.timedelta(days=WINDOW_DAYS)).isoformat()
    values = [c for d, c in closes if d >= since and c]
    return max((b / a - 1 for a, b in zip(values, values[1:])), default=0.0)


def judge(forms: Counter | None, jump: float) -> Finding | None:
    """The finding the filings and the price give (see the module docstring), or None."""
    if not forms:
        return None
    target = sorted(f for f in forms if _target_form(f))
    if target:
        return Finding(TARGET, f"идёт выкуп компании (формы SEC: {', '.join(target)})")
    if forms.get(DEAL_FORM, 0) >= DEAL_MIN:
        if jump >= JUMP:
            return Finding(TARGET, f"идёт выкуп компании (форма 425 ×{forms[DEAL_FORM]}, "
                                   f"скачок цены {jump * 100:+.0f}% за день)")
        return Finding(DEAL, f"компания участвует в слиянии (форма 425 ×{forms[DEAL_FORM]})")
    return None


def check(ticker: str, source: str | None, today: dt.date, *, forms_fn: Callable | None = None,
          closes_fn: Callable | None = None) -> Finding | None:
    """judge() of a ticker's filings and price history; None for what cannot be checked or when a lookup
    fails (logged): a signal is never held back by a check that did not run."""
    if source not in US_SOURCES or ":" in ticker:
        return None
    try:
        forms = (forms_fn or recent_forms)(ticker, today)
        if not forms:
            return None
        jump = 0.0
        if forms.get(DEAL_FORM, 0) >= DEAL_MIN and not any(_target_form(f) for f in forms):
            import positions
            jump = biggest_jump((closes_fn or positions.daily_closes)(ticker, source), today)
        return judge(forms, jump)
    except Exception as e:
        print(f"[takeover] {ticker}: not checked: {type(e).__name__}: {e}", file=sys.stderr)
        return None
