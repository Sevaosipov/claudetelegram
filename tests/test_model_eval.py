"""model_eval.py -- offline. No sklearn fit on real data, no network.
Synthetic row lists and monkeypatched fetchers throughout."""
from __future__ import annotations

import math

import numpy as np
import pytest

import model_eval


def _prow(ticker, member, disc, label, amount="$1,001 - $15,000"):
    return {"ticker": ticker, "member": member, "chamber": "house",
            "disclosure_date": disc, "trade_date": disc, "amount_range": amount,
            "label": label}


def test_cluster_counts_is_backward_looking_and_excludes_self():
    rows = [
        _prow("AAA", "Rep A", "2025-06-01", 1),
        _prow("AAA", "Rep B", "2025-06-10", 0),   # 1 other (Rep A) in the prior 30d
        _prow("AAA", "Rep A", "2025-06-20", 1),   # others = {Rep B} -> 1 (self excluded)
        _prow("AAA", "Rep C", "2025-09-01", 0),   # A/B trades are >30d back -> 0
    ]
    assert model_eval._cluster_counts(rows, window_days=30, id_key="member") == [0, 1, 1, 0]


def test_prior_hitrate_only_uses_settled_earlier_trades():
    rows = [
        _prow("AAA", "Rep A", "2025-01-01", 1),
        _prow("BBB", "Rep A", "2025-02-01", 0),
        # 2025-05-01: earlier Rep A trades whose window settled by 2025-05-01 minus
        # 42 days (= 2025-03-20). Both Jan and Feb qualify -> mean(1, 0) = 0.5
        _prow("CCC", "Rep A", "2025-05-01", 1),
        # 2025-02-15: only the Jan trade is old enough (>= 2025-01-04) -> 1.0
        _prow("DDD", "Rep A", "2025-02-15", 0),
        _prow("EEE", "Rep Z", "2025-05-01", 1),   # no prior Rep Z trades -> nan
    ]
    hr = model_eval._prior_hitrate(rows, horizon=21, id_key="member")
    assert hr[0] != hr[0] or math.isnan(hr[0])   # first trade ever -> nan
    assert hr[2] == pytest.approx(0.5)
    assert hr[3] == pytest.approx(1.0)
    assert math.isnan(hr[4])


def test_prior_hitrate_excludes_a_trade_disclosed_10_days_earlier():
    rows = [
        _prow("AAA", "Rep A", "2025-06-01", 1),
        _prow("BBB", "Rep A", "2025-06-11", 0),   # only 10 days later -> not settled -> nan
    ]
    hr = model_eval._prior_hitrate(rows, horizon=21, id_key="member")
    assert math.isnan(hr[1])


def test_build_feature_frame_politicians_shape(conn, monkeypatch):
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: 1.5)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 1e9)
    rows = [_prow("AAA", "Rep A", "2025-06-01", 1),
            _prow("BBB", "Rep B", "2025-06-02", 0),
            _prow("AAA", "Rep C", "2025-06-15", 1)]
    X, y, dates = model_eval.build_feature_frame(conn, rows, "politicians", horizon=21)
    num, cat, _ = model_eval._COLUMNS["politicians"]
    assert list(X.columns) == num + cat
    assert len(X) == 3 and list(y) == [1, 0, 1]
    assert dates == ["2025-06-01", "2025-06-02", "2025-06-15"]
    assert X["log_amount"].iloc[0] > 0
    assert X["mcap_bucket"].iloc[0] in ("small", "mid")


def test_build_feature_frame_insiders_shape(conn, monkeypatch):
    from conftest import add_sec_purchase
    monkeypatch.setattr(model_eval.backtest, "_return_before", lambda t, d, days=63: -2.0)
    monkeypatch.setattr(model_eval.marketcap, "market_cap_eur", lambda c, t: 5e9)
    rows = [
        {"ticker": "AAA", "owner": "Buyer One", "value": 250_000, "role": "officer/director",
         "disclosure_date": "2026-09-03", "trade_date": "2026-09-01",
         "shares": 100.0, "owned_after": 1100.0, "label": 1},
        {"ticker": "AAA", "owner": "Buyer Two", "value": 90_000, "role": "10% holder",
         "disclosure_date": "2026-09-04", "trade_date": "2026-09-02",
         "shares": 500.0, "owned_after": 500.0, "label": 0},
    ]
    X, y, dates = model_eval.build_feature_frame(conn, rows, "insiders", horizon=21)
    num, cat, _ = model_eval._COLUMNS["insiders"]
    assert list(X.columns) == num + cat
    assert list(y) == [1, 0]
    assert X["lag_days"].iloc[0] == 2
    assert X["position_increase_pct"].iloc[0] == pytest.approx(10.0)   # 100 into 1000 held
    assert X["position_increase_pct"].iloc[1] == pytest.approx(100.0)  # brand-new position
    assert X["role"].iloc[0] == "officer/director"
    assert X["is_first_buy"].iloc[0] in (0.0, 1.0)
