"""Unattended-run safety: nothing in the autopilot or the dashboard gets stuck or doubles an order."""

import dataclasses
import os
import time

import pytest

from polybot import cli, dashboard
from polybot.broker import Broker, Fill
from polybot.config import Risk, Settings
from polybot.polymarket import parse_market
from polybot.risk import EntryState, check_entry
from test_flow import desk  # noqa: F401 - fixture

MARKET = parse_market({"slug": "btc-updown-5m-1790679600", "question": "q", "outcomes": '["Up","Down"]',
                       "clobTokenIds": '["111","222"]', "acceptingOrders": True})


def test_live_sell_is_sized_in_whole_lots(tmp_path, monkeypatch):
    s = Settings(mode="live", live_confirmed=True, data_dir=tmp_path)
    b = Broker(s)
    sent = {}
    monkeypatch.setattr(b, "token_balance", lambda _t: 1.418494)
    monkeypatch.setattr(b, "_live", lambda m, o, side, amount, limit: sent.setdefault("shares", amount)
                        and Fill(True, "live", side, o, amount, 0.5, 0.35, "x", "ok"))
    b.sell(MARKET, "Up", 1.418494, 0.3, [], 0.0)
    assert sent["shares"] == pytest.approx(1.41)


def test_unconfirmed_live_buy_is_not_retried(desk, capsys, monkeypatch):  # noqa: F811
    s, state, _ = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, bankroll_usd=10.0, max_stake_usd=10.0))
    monkeypatch.setattr(cli, "load", lambda: s)
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    calls = []

    def buy(*_a, **_k):
        calls.append(1)
        return Fill(False, "paper", "BUY", "Up", 0, 0, 0, "order-1", "matched but NO shares arrived")
    monkeypatch.setattr(Broker, "buy", buy)
    assert cli.main(["auto", "--rounds", "1"]) == 0
    assert len(calls) == 1 and "not retrying this window" in capsys.readouterr().out


def test_manage_errors_are_retried_not_raised(desk, monkeypatch, capsys):  # noqa: F811
    s, _, w = desk
    seq = iter([RuntimeError("sdk hiccup"), True, False])

    def fake(*_a, **_k):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    monkeypatch.setattr(cli, "_manage_once", fake)
    a = type("A", (), {"hold": True, "stop_bid": 0.0, "stop_pct": 0.4})()
    cli._manage_until_close(s, w, a)
    assert "error while managing" in capsys.readouterr().out
    with pytest.raises(StopIteration):
        next(seq)  # it kept going until the position was gone


def test_a_failed_round_does_not_end_the_run(desk, capsys, monkeypatch):  # noqa: F811
    def boom(*_a, **_k):
        raise RuntimeError("unexpected")
    monkeypatch.setattr(cli, "_auto_round", boom)
    assert cli.main(["auto", "--rounds", "2"]) == 0
    assert capsys.readouterr().out.count("continuing with the next window") == 2


def test_stop_request_finishes_the_round_then_exits(desk, capsys, monkeypatch):  # noqa: F811
    rounds = []

    def round_(*_a, **_k):
        rounds.append(1)
        cli._on_stop(None, None)  # Stop pressed while this round runs
        return "traded"
    monkeypatch.setattr(cli, "_auto_round", round_)
    assert cli.main(["auto", "--rounds", "5", "--forever"]) == 0
    assert len(rounds) == 1 and "autopilot stopped by user" in capsys.readouterr().out


def test_geoblock_outage_pauses_but_a_block_is_final():
    base = dict(kill=False, live=True, cash_usd=5.0, realised_today=0.0, trades_today=0,
                open_in_window=False, seconds_left=100)
    down = check_entry(Risk(), EntryState(geoblock_ok=None, **base), 1.0, 0.8)
    blocked = check_entry(Risk(), EntryState(geoblock_ok=False, **base), 1.0, 0.8)
    assert not any(k in r for r in down for k in cli.FINAL)
    assert any(k in r for r in blocked for k in cli.FINAL)


def test_old_briefs_are_pruned(tmp_path):
    s = Settings(data_dir=tmp_path)
    d = tmp_path / "briefs"
    d.mkdir()
    old, new = d / "old.json", d / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    past = time.time() - 8 * 86400
    os.utime(old, (past, past))
    cli._prune_briefs(s)
    assert not old.exists() and new.exists()


class _Proc:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def test_dashboard_restarts_crashes_but_not_deliberate_exits(tmp_path):
    bot = dashboard.Bot(Settings(data_dir=tmp_path))
    bot.state_path.write_text("running")
    bot.proc = _Proc(1)  # crashed
    assert not bot.running() and bot.wanted() and bot.crashes == 1 and bot.crashed_at
    bot.proc = _Proc(0)  # ran out of cash
    assert not bot.running() and not bot.wanted()


class _Pos:
    def __init__(self, cid):
        self.condition_id, self.redeemable, self.slug, self.outcome, self.current_size = cid, True, "s-" + cid, "Up", 1


def test_failed_claim_waits_an_hour_before_retrying(tmp_path, monkeypatch):
    s = Settings(mode="live", live_confirmed=True, data_dir=tmp_path, private_key="k", funder="f",
                 builder_key=("a", "b", "c"))
    b = Broker(s)
    fake = type("C", (), {"list_positions": lambda self, **_: [_Pos("c1"), _Pos("c2")]})()
    monkeypatch.setattr(b, "clob", lambda: fake)
    tried = []

    def redeem(cid):
        tried.append(cid)
        if cid == "c1":
            raise RuntimeError("relayer timeout")
        return "ok"
    monkeypatch.setattr(b, "redeem", redeem)
    failed = {}
    b.claim_all(failed)
    assert tried == ["c1", "c2"] and set(failed) == {"c1"}
    b.claim_all(failed)  # c1 is cooling down; c2 is claimed again only if still redeemable
    assert tried.count("c1") == 1


def test_autopilot_does_not_wait_for_claims(desk, monkeypatch):  # noqa: F811
    s, _, _ = desk
    kicked = []
    monkeypatch.setattr(cli._Claimer, "kick", lambda self: kicked.append(1))
    monkeypatch.setattr(Broker, "claim_all", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("inline claim")))
    assert cli.main(["auto", "--rounds", "1"]) == 0
    assert kicked


def test_low_cash_while_claiming_pauses_instead_of_stopping(desk, capsys, monkeypatch):  # noqa: F811
    s, state, _ = desk
    s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, bankroll_usd=0.5))
    monkeypatch.setattr(cli, "load", lambda: s)
    state.update(spot=100_400.0, up_ask=0.88, down_ask=0.14)
    monkeypatch.setattr(cli._Claimer, "pending", lambda self: True)
    assert cli.main(["auto", "--rounds", "2", "--forever", "--fixed", "--usd", "1"]) == 0
    out = capsys.readouterr().out
    assert "autopilot paused: stake $1.00 exceeds available cash" in out and "autopilot stopping" not in out


def test_healthcheck_needs_no_password_but_pages_do(tmp_path):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    s = Settings(data_dir=tmp_path, dashboard_password="pw")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.make_handler(dashboard.App(s)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
            c.request("GET", path)
            return c.getresponse().status
        assert get("/healthz") == 200
        assert get("/") == 401 and get("/api/summary") == 401
    finally:
        srv.shutdown()
