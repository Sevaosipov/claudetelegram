"""cfd: the CFD-signal research (spec docs/superpowers/specs/2026-10-05-cfd-signals.md).

Part 2 of the spec lives here: a small, pure backtest engine -- the universe and its costs
(instruments), the bars and their hygiene (data), indicators, the three setups, the exits, and
the research run that applies the pre-registered gate. Everything but data.py's yfinance edge is
pure: bars in, signals and trades out, so the whole engine runs offline on synthetic series.
"""
