"""Schedule 13D/G stake signals: a 5%+ holder declaring or growing a position."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import db

from .common import STAKE_MIN_INCREASE_PP, STAKE_MIN_PERCENT


@dataclass
class StakeSignal:
    """A 5%+ beneficial owner declaring or increasing a position (Schedule 13D/G).

    Unlike a ClusterSignal this is one filer, and the headline number is a share of
    the company rather than an amount of money -- which is the point: €500k means
    something entirely different in a €50m company than in a €500bn one, and until
    these filings were added nothing here could tell the difference.
    """
    source: str          # always "SEC13DG"
    ticker: str
    company: str
    person: str
    form_type: str
    percent: float
    prev_percent: float | None
    amount_owned: float | None
    event_date: str
    url: str
    corroborated_by: list[str] = field(default_factory=list)
    # Other reporting persons on the SAME filing (accession) as `person` -- SEC
    # rules require every control person in a fund's ownership chain (GP, LP,
    # individual managers) to be listed separately, even though they're one
    # economic position. See find_stake_signals.
    co_filer_names: list[str] = field(default_factory=list)

    @property
    def is_activist(self) -> bool:
        return self.form_type.startswith("SCHEDULE 13D")
def find_stake_signals(conn, min_percent: float = STAKE_MIN_PERCENT,
                        min_increase_pp: float = STAKE_MIN_INCREASE_PP,
                        activist_only: bool = False,
                        max_age_days: int | None = None,
                        new_positions_only: bool = False,
                        ignore_alert_state: bool = False) -> list[StakeSignal]:
    """Newly declared or materially increased 5%+ stakes.

    What counts as news:
      - a first 13D/13G for a holder in an issuer (a stake that wasn't there before);
      - an amendment showing the stake up by at least `min_increase_pp` points.
    What doesn't: the constant drizzle of 13G/A amendments where an index fund's
    holding moved a tenth of a point, and anything below the 5% filing trigger,
    which can only be a position being wound down.

    Alert state is keyed per (issuer, holder) rather than per issuer -- two
    unrelated funds building stakes in the same company are two separate pieces of
    news, and one must not mask the other.

    Co-filers of the SAME accession (SEC requires every control person in a
    fund's ownership chain -- the GP, the LP, individual managers -- to be
    listed as its own "reporting person" on one filing) are merged into a
    single signal: one economic position, not N near-identical ones. The
    first-listed filer becomes the signal's `person`; the rest land in
    `co_filer_names`, which commit_stake_alert uses to suppress re-firing for
    all of them, not just the one shown. Two different filing groups on the
    same ticker (different accessions) stay separate signals -- those are
    genuinely different holders.

    `max_age_days` drops a candidate whose own event_date is older than that
    many days. Unlike this file's other finders, which scope to `window_days`
    back from today by construction, sec_stakes has no natural recency window
    -- without this a stake signal can resurface a filing that's years old.
    None (the default) keeps every filing regardless of age, matching every
    other parameter here: off unless asked for.

    `new_positions_only` drops every amendment outright, however large --
    a holder already at 20% jumping to 40% is still the same holder, not a
    new activist showing up. This is a stricter, different bar than
    `min_increase_pp` (which still requires an amendment be material), not a
    bigger version of it; the two are mutually exclusive in effect since
    `new_positions_only` skips the amendment path entirely.

    Checked via `form_type` ("SCHEDULE 13D"/"13G" vs "...{D,G}/A"), the SEC's
    own designation for an original filing vs. an amendment -- deliberately
    NOT via "is this the first row we've seen for this holder", which this
    project's own scan history cannot answer reliably yet: 13D/G tracking is
    young, so for most holders the first row we HAVE is already an amendment
    to a long-standing real position. form_type has no such blind spot.
    """
    rows = conn.execute(
        """SELECT ticker, issuer_cik, issuer_name, person_name, form_type, event_date,
                  percent_of_class, amount_owned, source_url, accession, found_at
           FROM sec_stakes
           WHERE percent_of_class IS NOT NULL
           ORDER BY event_date, found_at""",
    ).fetchall()

    # (issuer, holder) -> filings in chronological order, so "did it grow" compares
    # against that holder's own previous filing rather than anyone else's.
    history: dict[tuple, list] = {}
    for r in rows:
        ticker, issuer_cik, issuer_name, person, form_type, event_date, pct, amount, url, acc, found = r
        history.setdefault((ticker or issuer_cik, person), []).append(r)

    # Filers that qualify individually, kept with the accession their qualifying
    # filing was on so co-filers of ONE filing can be merged in the next pass.
    candidates = []
    for (key, person), filings in history.items():
        latest = filings[-1]
        (ticker, issuer_cik, issuer_name, person_name, form_type, event_date,
         pct, amount, url, acc, found) = latest
        if pct is None or pct < min_percent:
            continue
        # No trading symbol means the issuer isn't listed anywhere this bot can
        # follow -- a shell, a private fund, a delisted name. The row stays in
        # sec_stakes for the dossier; it just isn't something to act on, and the
        # alert used to show the issuer's CIK where the ticker should be.
        if not (ticker or "").strip():
            continue
        if activist_only and not form_type.startswith("SCHEDULE 13D"):
            continue
        if max_age_days is not None:
            try:
                age_days = (dt.date.today() - dt.date.fromisoformat(event_date)).days
            except (TypeError, ValueError):
                age_days = None
            if age_days is not None and age_days > max_age_days:
                continue

        prev_pct = filings[-2][6] if len(filings) > 1 else None
        if new_positions_only:
            if form_type.endswith("/A"):
                continue
        elif prev_pct is not None and pct - prev_pct < min_increase_pp:
            continue

        state_key = f"{key}|{person}"
        if not ignore_alert_state:
            prev = db.get_alert_state(conn, "SEC13DG", state_key)
            if prev is not None:
                last_pct = prev["total_value"]
                if last_pct is not None and pct - last_pct < min_increase_pp:
                    continue

        candidates.append((ticker or issuer_cik, acc, latest, prev_pct))

    by_filing: dict[tuple, list] = {}
    for ticker, acc, row, prev_pct in candidates:
        by_filing.setdefault((ticker, acc), []).append((row, prev_pct))

    signals = []
    for (ticker, acc), entries in by_filing.items():
        (_t, _cik, issuer_name, person_name, form_type, event_date,
         pct, amount, url, _acc, _found), prev_pct = entries[0]
        co_filer_names = [row[3] for row, _ in entries[1:]]
        signals.append(StakeSignal(
            source="SEC13DG", ticker=ticker, company=issuer_name,
            person=person_name, form_type=form_type, percent=pct, prev_percent=prev_pct,
            amount_owned=amount, event_date=event_date, url=url,
            co_filer_names=co_filer_names,
        ))
    return signals
def commit_stake_alert(conn, signal: StakeSignal) -> None:
    """Records alert-state for `signal.person` AND every name in
    co_filer_names -- a merged signal's un-recorded co-filer would otherwise
    look "new" again on the very next run and re-fire the whole group."""
    for person in [signal.person] + signal.co_filer_names:
        db.save_cluster_alert_state(conn, "SEC13DG", f"{signal.ticker}|{person}",
                                     1, [person], signal.percent)
