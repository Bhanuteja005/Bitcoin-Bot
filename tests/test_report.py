import pytest

from polybot import report


def _row(kind, slug, outcome, shares, usd, price, **kw):
    return {"ts": 1.0, "kind": kind, "mode": "paper", "slug": slug, "outcome": outcome, "shares": shares,
            "usd": usd, "price": price, "status": "settled" if kind == "settle" else "filled", **kw}


def test_report_pnl_exits_and_calibration():
    rows = [
        _row("entry", "a", "Up", 20, 10.0, 0.5, fair=0.6, edge=0.08),
        _row("settle", "a", "Up", 20, 20.0, 1.0),  # held, won: +10
        _row("entry", "b", "Down", 20, 10.0, 0.5, fair=0.6, edge=0.08),
        _row("exit", "b", "Down", 20, 4.0, 0.2, reason="stop-loss"),  # stopped: -6
        _row("entry", "c", "Up", 12.5, 10.0, 0.8, fair=0.9, edge=0.08),
        _row("exit", "c", "Up", 12.5, 11.25, 0.9, reason="take-profit"),  # +1.25
    ]
    r = report.summarise(report.trades(rows), {"b": "Up", "c": "Up"})  # b's side lost; c's side won
    assert r["trades"] == 3 and r["wins"] == 2
    assert r["realised"] == pytest.approx(10 - 6 + 1.25)
    assert r["expected"] == pytest.approx((0.6 * 20 - 10) * 2 + (0.9 * 12.5 - 10))
    assert r["stop_n"] == 1 and r["stop_saved"] == pytest.approx(4.0)  # sold for $4 a side that paid $0
    assert r["tp_n"] == 1 and r["tp_saved"] == pytest.approx(11.25 - 12.5)  # holding would have paid $12.50
    assert r["brier_model"] == pytest.approx(((0.6 - 1) ** 2 + 0.6**2 + (0.9 - 1) ** 2) / 3)
    assert r["buckets"] == {"0.6-0.7": [2, 1], "0.9-1.0": [1, 1]}
