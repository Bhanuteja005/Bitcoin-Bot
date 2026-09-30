"""End-to-end CLI flow in paper mode with every network feed stubbed."""

import dataclasses
import time

import pytest

from polybot import cli, feeds, journal, polymarket
from polybot.config import Settings
from polybot.feeds import Candle
from polybot.polymarket import parse_market
from polybot.window import window_at


@pytest.fixture
def desk(tmp_path, monkeypatch):
    s = Settings(data_dir=tmp_path)
    monkeypatch.setattr(cli, "load", lambda: s)
    clock = {"now": 1790679600 + 120.0}  # fixed: 180s left in the window
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    monkeypatch.setattr(time, "sleep", lambda sec: clock.update(now=clock["now"] + sec))
    w = window_at(time.time())
    state = {"spot": 100_000.0, "up_ask": 0.51, "down_ask": 0.51}

    def market(_host, slug):
        return parse_market({"slug": slug, "question": "BTC Up or Down?", "outcomes": '["Up","Down"]',
                             "clobTokenIds": '["111","222"]', "acceptingOrders": True})

    def book(_host, token):
        ask = state["up_ask"] if token == "111" else state["down_ask"]
        return [(round(ask - 0.02, 2), 500.0)], [(ask, 500.0)]

    candles = [Candle(w.start - 3600 + 60 * i, 1e5, 1e5, 1e5, 1e5 * (1 + 0.0003 * (i % 2)), 1) for i in range(61)]
    monkeypatch.setattr(polymarket, "market_by_slug", market)
    monkeypatch.setattr(polymarket, "book", book)
    monkeypatch.setattr(feeds, "spot", lambda _h: state["spot"])
    monkeypatch.setattr(feeds, "klines", lambda *_a, **_k: candles)
    monkeypatch.setattr(feeds, "open_at", lambda _h, _ts: 100_000.0)
    monkeypatch.setattr(feeds, "twap", lambda _h, _start, n: (100_000.0, n))
    monkeypatch.setattr(feeds, "server_time", lambda _h: time.time())
    return s, state, w


def test_scan_then_refuse_then_buy_then_manage(desk, capsys):
    s, state, w = desk
    assert cli.main(["scan"]) == 0
    assert "NO_TRADE" in capsys.readouterr().out

    assert cli.main(["buy", "auto"]) == 2  # fair book -> nothing sent
    assert "NO TRADE" in capsys.readouterr().out

    assert cli.main(["buy", "up"]) == 2  # against the model without --override
    assert "REFUSED" in capsys.readouterr().out

    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)  # BTC ripped, book lagging
    assert cli.main(["buy", "auto"]) == 0
    out = capsys.readouterr().out
    assert "[PAPER] BOUGHT" in out and " Up " in out

    assert cli.main(["buy", "up", "--force"]) == 3  # one position per window + no cash left
    assert "REFUSED by risk gate" in capsys.readouterr().out

    assert cli.main(["manage"]) == 0
    assert "Up" in capsys.readouterr().out

    state.update(up_ask=0.99)  # bid 0.97: manual sell goes through whatever the model says
    assert cli.main(["sell", "up"]) == 0
    assert "[PAPER] SOLD" in capsys.readouterr().out
    assert not journal.replay(journal.rows(s.data_dir)).positions


def test_kill_switch_blocks_entries(desk, capsys):
    s, state, _ = desk
    cli.main(["kill", "on"])
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    assert cli.main(["buy", "auto"]) == 3
    assert "kill switch is ON" in capsys.readouterr().out
    cli.main(["kill", "off"])
    assert not s.kill_file.exists()


def test_settlement_books_pnl_and_live_waits_for_polymarket(desk, monkeypatch, capsys):
    s, _, w = desk
    past = w.start - 600
    slug = f"btc-updown-5m-{past}"
    for mode in ("paper", "live"):
        journal.append(s.data_dir, "entry", mode=mode, slug=slug, outcome="Up", token="111",
                       shares=2.0, usd=1.0, price=0.5, status="filled")
    # Polymarket unreachable: paper settles from Binance (up 50 -> Up wins), live waits.
    monkeypatch.setattr(polymarket, "market_by_slug", lambda *_: (_ for _ in ()).throw(cli.FeedError("down")))
    SL = s.risk.strike_twap_s
    monkeypatch.setattr(feeds, "twap", lambda _h, start, n: (100_000.0 if start == past - SL else 100_050.0, n))
    notes = cli.settle_positions(s)
    assert any("WON $+1.00" in n and "binance-approx" in n for n in notes)
    assert any("live result not published" in n for n in notes)
    book = journal.replay(journal.rows(s.data_dir))
    assert list(book.positions) == [("live", slug, "Up")]
    assert sum(book.realised_by_day.values()) == 1.0


def test_autopilot_one_round(desk, capsys, monkeypatch):
    s, state, w = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, bankroll_usd=10.0, max_stake_usd=10.0))
    monkeypatch.setattr(cli, "load", lambda: s)
    # Flat book until the zone; BTC is $400 up once we are inside it.
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    assert cli.main(["auto", "--rounds", "1", "--interval", "1"]) == 0
    out = capsys.readouterr().out
    assert "[PAPER] BOUGHT" in out and "Up" in out
    entry = next(r for r in journal.rows(s.data_dir) if r["kind"] == "entry")
    assert 1.0 <= entry["usd"] <= 2.5  # quarter-Kelly on a $10 bankroll
    kinds = [(r["kind"], r.get("status")) for r in journal.rows(s.data_dir)]
    assert ("entry", "filled") in kinds and ("settle", "settled") in kinds
    assert not journal.replay(journal.rows(s.data_dir)).positions


def test_autopilot_advise_sends_nothing(desk, capsys):
    s, state, _ = desk
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    assert cli.main(["auto", "--rounds", "1", "--advise"]) == 0
    assert "advice only" in capsys.readouterr().out
    assert not [r for r in journal.rows(s.data_dir) if r["kind"] == "entry"]


def test_autopilot_skips_fair_rounds(desk, capsys):
    s, _, _ = desk
    assert cli.main(["auto", "--rounds", "1"]) == 0
    assert "no entry this round" in capsys.readouterr().out
    assert not [r for r in journal.rows(s.data_dir) if r["kind"] == "entry"]


def test_paper_settlement_waits_when_readings_disagree(desk, monkeypatch):
    s, _, w = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, settle_windows=(60, 300)))
    past = w.start - 600
    slug = f"btc-updown-5m-{past}"
    journal.append(s.data_dir, "entry", mode="paper", slug=slug, outcome="Up", token="111",
                   shares=2.0, usd=1.0, price=0.5, status="filled")
    monkeypatch.setattr(polymarket, "market_by_slug", lambda *_: (_ for _ in ()).throw(cli.FeedError("down")))
    end = past + 300
    # Last 60s averaged above the strike, but the full 5 minutes averaged below it.
    prices = {past - 60: 100_000.0, end - 60: 100_050.0, end - 300: 99_950.0}
    monkeypatch.setattr(feeds, "twap", lambda _h, start, n: (prices[start], n))
    notes = cli.settle_positions(s)
    assert any("disagree" in n for n in notes)
    assert journal.replay(journal.rows(s.data_dir)).positions  # still open


def test_autopilot_sits_out_when_bankroll_too_small_for_min_order(desk, capsys):
    s, state, _ = desk  # $1 bankroll: the $1 minimum would be 100% of cash
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    assert cli.main(["auto", "--rounds", "1"]) == 0
    assert "below the $1 minimum" in capsys.readouterr().out
    assert not [r for r in journal.rows(s.data_dir) if r["kind"] == "entry"]


def test_exit_values_position_at_midpoint_of_readings(desk, monkeypatch, capsys):
    s, state, w = desk
    journal.append(s.data_dir, "entry", mode="paper", slug=w.slug, outcome="Up", token="111",
                   shares=2.0, usd=1.0, price=0.5, status="filled")
    monkeypatch.setattr(cli, "fair_up_range", lambda *a, **k: (0.40, 0.80))  # readings disagree
    state.update(up_ask=0.60)  # bid 0.58: above the worst reading (0.40) but below the midpoint (0.60)
    cli._manage_once(s, auto_sell=True)
    out = capsys.readouterr().out
    assert "fair 0.600" in out and "HOLD" in out and "SOLD" not in out


def test_paper_autopilot_fills_against_fresh_book(desk, capsys, monkeypatch):
    s, state, w = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, bankroll_usd=10.0, max_stake_usd=10.0))
    monkeypatch.setattr(cli, "load", lambda: s)
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    real_book = polymarket.book
    calls = {"n": 0}

    def moving_book(host, token):
        # Every book read after the brief's two shows the Up ask already repriced past the cap.
        calls["n"] += 1
        if calls["n"] > 2 and token == "111":
            return [(0.95, 500.0)], [(0.97, 500.0)]
        return real_book(host, token)
    monkeypatch.setattr(polymarket, "book", moving_book)
    assert cli.main(["auto", "--rounds", "1", "--usd", "1", "--fixed"]) == 0
    assert "[PAPER] BOUGHT" not in capsys.readouterr().out  # the stale 0.60 ask is gone


def test_report_command_runs_on_journal(desk, capsys):
    s, state, w = desk
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    cli.main(["buy", "auto", "--usd", "1"])
    cli.main(["sell", "up"])
    capsys.readouterr()
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert "trades 1" in out and "expected from model edge" in out


def test_percent_stop_sells_at_40pct_below_entry(desk, capsys):
    s, state, w = desk
    journal.append(s.data_dir, "entry", mode="paper", slug=w.slug, outcome="Up", token="111",
                   shares=1.25, usd=1.0, price=0.80, status="filled")
    state.update(up_ask=0.52, down_ask=0.50)  # bid 0.50 > 0.48 stop level: hold
    assert cli._manage_once(s, auto_sell=False, stop_pct=0.40)
    assert journal.replay(journal.rows(s.data_dir)).positions
    state.update(up_ask=0.48, down_ask=0.54)  # bid 0.46 <= 0.80 * 0.6: sell
    cli._manage_once(s, auto_sell=False, stop_pct=0.40)
    assert "bid stop" in capsys.readouterr().out
    exit_row = next(r for r in journal.rows(s.data_dir) if r["kind"] == "exit")
    assert exit_row["reason"] == "stop-loss"


def _limited(desk, monkeypatch, **risk):
    s, state, _ = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, **{"bankroll_usd": 10.0, "max_stake_usd": 10.0, **risk}))
    monkeypatch.setattr(cli, "load", lambda: s)
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    return s


def test_forever_pauses_on_daily_limit_instead_of_exiting(desk, capsys, monkeypatch):
    _limited(desk, monkeypatch, max_trades_per_day=0)
    assert cli.main(["auto", "--rounds", "2"]) == 0
    assert "autopilot stopping: max 0 trades" in capsys.readouterr().out
    assert cli.main(["auto", "--rounds", "2", "--forever"]) == 0
    out = capsys.readouterr().out
    assert out.count("autopilot paused: max 0 trades") == 2 and "autopilot stopping" not in out


def test_forever_still_stops_when_cash_runs_out(desk, capsys, monkeypatch):
    _limited(desk, monkeypatch, bankroll_usd=0.5, max_stake_usd=1.0)
    assert cli.main(["auto", "--rounds", "3", "--forever", "--fixed", "--usd", "1"]) == 0
    assert "autopilot stopping: stake $1.00 exceeds available cash $0.50" in capsys.readouterr().out
