"""model_eval.py -- offline. No sklearn fit on real data, no network.
Synthetic row lists and monkeypatched fetchers throughout."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
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
    # Chronological on purpose: the feature is date-based (a member's trades whose
    # own 2*horizon-day settle window closed before D), so order in the list must
    # not matter -- these rows happen to be sorted, but the logic does not rely on it.
    rows = [
        _prow("T1", "Rep A", "2025-01-01", 1),
        _prow("T2", "Rep A", "2025-02-01", 0),   # Jan is 31d back (< 2*21=42) -> not settled -> nan
        _prow("T3", "Rep A", "2025-03-01", 1),   # Jan settled (59d), Feb not (28d) -> mean(1) = 1.0
        _prow("T4", "Rep A", "2025-06-01", 0),   # Jan/Feb/Mar all settled -> mean(1,0,1) = 2/3
        _prow("T5", "Rep Z", "2025-06-01", 1),   # no prior Rep Z trade -> nan
    ]
    hr = model_eval._prior_hitrate(rows, horizon=21, id_key="member")
    assert math.isnan(hr[0])
    assert math.isnan(hr[1])
    assert hr[2] == pytest.approx(1.0)
    assert hr[3] == pytest.approx(2 / 3)
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


def _dates(spec: dict[str, int]) -> list[str]:
    out = []
    for month, n in spec.items():
        out += [f"{month}-{(i % 27) + 1:02d}" for i in range(n)]
    return sorted(out)


def test_walk_forward_folds_expanding_no_overlap():
    dates = _dates({"2025-03": 15, "2025-04": 15, "2025-05": 15,
                    "2025-06": 15, "2025-07": 15})
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    assert folds is not None and len(folds) == 3
    for train_idx, test_idx in folds:
        assert not (set(train_idx) & set(test_idx))
        latest_train = max(dates[i] for i in train_idx)
        earliest_test = min(dates[i] for i in test_idx)
        assert latest_train[:7] < earliest_test[:7]      # train strictly before test month
    # test months are the last 3
    assert {min(dates[i] for i in f[1])[:7] for f in folds} == {"2025-05", "2025-06", "2025-07"}


def test_walk_forward_folds_skips_a_sparse_month_as_a_test_fold():
    dates = _dates({"2025-02": 15, "2025-03": 15, "2025-04": 15,
                    "2025-05": 15, "2025-06": 15, "2025-07": 4})   # July too thin
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    test_months = {min(dates[i] for i in f[1])[:7] for f in folds}
    assert test_months == {"2025-04", "2025-05", "2025-06"}   # July excluded


def test_walk_forward_folds_none_when_not_enough_months():
    dates = _dates({"2025-06": 30, "2025-07": 30})
    assert model_eval.walk_forward_folds(dates, n_folds=3, min_test=12) is None


def test_check_sufficiency_flags_too_few_rows():
    dates = _dates({"2026-07": 10, "2026-08": 10})
    msg = model_eval.check_sufficiency(dates, 3, 80, 12, "insiders", "2026-07")
    assert msg is not None and "INSUFFICIENT DATA for insiders" in msg
    assert "re-run around" in msg


def test_check_sufficiency_passes_a_healthy_political_set():
    dates = _dates({"2025-03": 20, "2025-04": 20, "2025-05": 20,
                    "2025-06": 20, "2025-07": 20})
    assert model_eval.check_sufficiency(dates, 3, 80, 12, "politicians", "2025-03") is None


def test_pooled_auc_perfect_and_none():
    assert model_eval.pooled_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)
    assert model_eval.pooled_auc([1, 1, 1], [0.3, 0.6, 0.9]) is None


def test_top_decile_precision():
    y = [0] * 18 + [1, 1]
    p = [0.1] * 18 + [0.9, 0.95]        # the two 1s are the top decile
    assert model_eval.top_decile_precision(y, p) == pytest.approx(1.0)


def test_calibration_deciles_group_by_predicted_probability():
    y = [0, 0, 1, 1]
    p = [0.05, 0.05, 0.95, 0.95]
    dec = model_eval.calibration_deciles(y, p)
    lo = next(d for d in dec if d["bucket"] == "0.0-0.1")
    hi = next(d for d in dec if d["bucket"] == "0.9-1.0")
    assert lo["actual"] == pytest.approx(0.0) and hi["actual"] == pytest.approx(1.0)


@pytest.mark.parametrize("auc,fragment", [
    (0.50, "No detectable edge"), (0.53, "No detectable edge"),
    (0.62, "Nominal edge"), (0.38, "Worse than chance"), (None, "label variety"),
])
def test_interpret_bands(auc, fragment):
    assert fragment in model_eval._interpret(auc, n=150, folds=3)


def _synthetic_frame(n=90, signal=True):
    rng = np.random.default_rng(0)
    months = ["2025-04", "2025-05", "2025-06", "2025-07", "2025-08"]
    dates, feat, y = [], [], []
    for i in range(n):
        m = months[i % len(months)]
        dates.append(f"{m}-{(i % 27) + 1:02d}")
        strong = rng.normal()
        noise = rng.normal()
        label = 1 if (strong if signal else rng.normal()) > 0 else 0
        feat.append({"log_amount": strong, "lag_days": noise, "cluster_size": 0,
                     "member_prior_hitrate": float("nan"), "price_vs_spy_63d": noise,
                     "chamber": "house", "mcap_bucket": "small"})
        y.append(label)
    X = pd.DataFrame(feat, columns=model_eval._COLUMNS["politicians"][0]
                                  + model_eval._COLUMNS["politicians"][1])
    return X, np.array(y), sorted(dates)


def test_run_walk_forward_recovers_a_planted_signal():
    X, y, dates = _synthetic_frame(signal=True)
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    res = model_eval.run_walk_forward(X, y, dates, folds, "politicians")
    assert res["models"]["lr"]["auc"] > 0.75          # log_amount ~ label by construction
    assert len(res["models"]["lr"]["per_fold_auc"]) == 3
    assert 0.0 <= res["base_rate"] <= 1.0
    assert res["lr_coefficients"][0][0] in X.columns
    assert res["calibration"] and "n" in res["calibration"][0]


def test_run_walk_forward_finds_nothing_in_noise():
    X, y, dates = _synthetic_frame(signal=False)
    folds = model_eval.walk_forward_folds(dates, n_folds=3, min_test=12)
    res = model_eval.run_walk_forward(X, y, dates, folds, "politicians")
    assert 0.30 < res["models"]["lr"]["auc"] < 0.70   # ~coin flip


_FAKE_METRICS = {
    "n": 60, "base_rate": 0.48,
    "models": {
        "lr": {"auc": 0.52, "brier": 0.249, "per_fold_auc": [0.49, 0.58, 0.50], "train_auc": 0.71},
        "gbt": {"auc": 0.50, "brier": 0.255, "per_fold_auc": [0.47, 0.55, 0.49], "train_auc": 0.95},
        "baseline": {"auc": 0.51, "per_fold_auc": [0.50, 0.53, 0.50], "train_auc": 0.55},
    },
    "calibration": [{"bucket": "0.4-0.5", "n": 30, "predicted": 0.45, "actual": 0.47},
                    {"bucket": "0.5-0.6", "n": 30, "predicted": 0.55, "actual": 0.50}],
    "top_decile": 0.50,
    "lr_coefficients": [("log_amount", 0.31), ("chamber_house", -0.02)],
    "gbt_importance": [("log_amount", 0.01), ("lag_days", -0.00)],
}


def test_format_report_renders_metrics_and_interpretation():
    text = model_eval.format_report("politicians", _FAKE_METRICS, horizon=21, folds=3)
    assert "MODEL EVALUATION" in text and "political trades" in text
    assert "0.52" in text and "Base rate" in text
    assert "No detectable edge" in text          # AUC 0.52 -> coin-flip band
    assert "Per-fold AUC (LR): 0.49, 0.58, 0.50" in text
    assert "log_amount" in text
    assert "not investment advice" in text.lower()


def test_format_report_passes_through_an_abort_message():
    text = model_eval.format_report("insiders", "INSUFFICIENT DATA for insiders.\n  20 rows.",
                                     horizon=21, folds=3)
    assert "INSUFFICIENT DATA for insiders." in text
    assert "not investment advice" in text.lower()


def test_format_report_has_no_ticker_verdict_language():
    text = model_eval.format_report("politicians", _FAKE_METRICS, horizon=21, folds=3).lower()
    for bad in ("buy ", "sell ", "will rise", "will fall", "target price", "forecast:"):
        assert bad not in text
