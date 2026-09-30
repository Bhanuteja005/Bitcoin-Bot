"""Live broker path against a fake SDK client: every response shape the V2 SDK returns."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from polymarket import TransactionFailedError
from polymarket import TimeoutError as SettleTimeout
from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder

from polybot import broker as brokermod
from polybot import journal
from polybot.broker import Broker
from polybot.config import Settings
from polybot.polymarket import parse_market


def _accepted(status="matched", making="1", taking="1.6"):
    return AcceptedOrder(order_id="0xabc", status=status, making_amount=Decimal(making),
                         taking_amount=Decimal(taking), trade_ids=("t1",), transactions_hashes=())


class FakeClient:
    def __init__(self, resp, settle=None, order=None):
        self.resp, self.settle, self.order, self.calls = resp, settle, order, []

    def place_market_order(self, **kw):
        self.calls.append(kw)
        return self.resp

    def wait_for_order_fill_settlement(self, resp, timeout_s):
        if self.settle:
            raise self.settle
        return ("0xhash",)

    def get_order(self, order_id):
        return self.order

    held = None  # on-chain token balance in shares; None = no position endpoint answer

    def get_balance_allowance(self, asset_type, token_id=None):
        if self.held is None:
            raise RuntimeError("no balance")
        return SimpleNamespace(balance=int(self.held * 1e6))


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(brokermod.time, "sleep", lambda _s: None)
    s = Settings(mode="live", live_confirmed=True, private_key="0x" + "1" * 64,
                 funder="0x" + "2" * 40, data_dir=tmp_path)
    m = parse_market({"slug": "btc-updown-5m-1790679600", "outcomes": '["Up","Down"]',
                      "clobTokenIds": '["111","222"]', "acceptingOrders": True})

    def make(client):
        b = Broker(s)
        b._clob = client
        return b
    return s, m, make


def test_matched_and_settled_buy(live):
    s, m, make = live
    c = FakeClient(_accepted())
    c.held = 1.6
    f = make(c).buy(m, "Up", 1.0, 0.66, [], 0.0)
    assert f.ok and f.mode == "live" and f.shares == 1.6 and f.usd == 1.0
    assert c.calls[0] == {"token_id": "111", "side": "BUY", "amount": "1.00", "max_price": "0.66", "order_type": "FOK"}
    assert journal.replay(journal.rows(s.data_dir)).positions


def test_sell_uses_shares_and_min_price(live):
    _, m, make = live
    c = FakeClient(_accepted(making="1.6", taking="1.2"))
    f = make(c).sell(m, "Down", 1.6, 0.70, [], 0.0)
    assert f.ok and f.shares == 1.6 and f.usd == 1.2
    assert c.calls[0]["shares"] == "1.6000" and c.calls[0]["min_price"] == "0.70" and c.calls[0]["token_id"] == "222"


def test_rejected_fok_is_not_a_fill(live):
    s, m, make = live
    f = make(FakeClient(RejectedOrder(code="fok_not_filled", message="order couldn't be fully filled"))).buy(m, "Up", 1.0, 0.6, [], 0.0)
    assert not f.ok and "fok_not_filled" in f.detail
    assert not journal.replay(journal.rows(s.data_dir)).positions


def test_settlement_failure_is_not_a_fill(live):
    s, m, make = live
    f = make(FakeClient(_accepted(), settle=TransactionFailedError("reverted"))).buy(m, "Up", 1.0, 0.6, [], 0.0)
    assert not f.ok and "settlement FAILED" in f.detail
    assert not journal.replay(journal.rows(s.data_dir)).positions


def test_settlement_timeout_is_flagged(live):
    _, m, make = live
    c = FakeClient(_accepted(), settle=SettleTimeout("slow"))
    c.held = 1.6
    f = make(c).buy(m, "Up", 1.0, 0.6, [], 0.0)
    assert f.ok and "not confirmed" in f.detail


def test_delayed_then_matched(live):
    _, m, make = live
    order = SimpleNamespace(size_matched=Decimal("1.5"), price=Decimal("0.6"), side="BUY", status="MATCHED")
    c = FakeClient(_accepted(status="delayed", making="0", taking="0"), order=order)
    c.held = 1.5
    f = make(c).buy(m, "Up", 1.0, 0.66, [], 0.0)
    assert f.ok and f.shares == 1.5 and f.usd == pytest.approx(0.9)


def test_delayed_never_matched(live):
    _, m, make = live
    order = SimpleNamespace(size_matched=Decimal("0"), price=Decimal("0.6"), side="BUY", status="CANCELED")
    f = make(FakeClient(_accepted(status="delayed", making="0", taking="0"), order=order)).buy(m, "Up", 1.0, 0.66, [], 0.0)
    assert not f.ok and "delayed" in f.detail


def test_sell_uses_on_chain_balance_when_fee_shaved_shares(live):
    _, m, make = live
    c = FakeClient(_accepted(making="1.58", taking="1.1"))
    c.held = 1.587  # the buy reported 1.6 shares; the fee left 1.587 in the wallet
    f = make(c).sell(m, "Up", 1.6, 0.60, [], 0.0)
    assert f.ok and c.calls[0]["shares"] == "1.5800"


def test_sell_refused_when_nothing_held(live):
    _, m, make = live
    c = FakeClient(_accepted())
    c.held = 0.0
    f = make(c).sell(m, "Up", 1.6, 0.60, [], 0.0)
    assert not f.ok and "no shares" in f.detail and not c.calls


def test_paginated_results_are_flattened():
    from polybot.broker import items
    pages = [SimpleNamespace(items=(1, 2)), SimpleNamespace(items=()), SimpleNamespace(items=(3,))]
    assert list(items(pages)) == [1, 2, 3]


def test_matched_buy_with_no_shares_in_wallet_is_not_a_fill(live):
    s, m, make = live
    c = FakeClient(_accepted())
    c.held = 0.0  # "matched" and settled, but nothing arrived
    f = make(c).buy(m, "Up", 1.0, 0.66, [], 0.0)
    assert not f.ok and "NO shares arrived" in f.detail
    assert not journal.replay(journal.rows(s.data_dir)).positions


def test_prices_sent_on_tick_grid(live):
    from polybot.broker import _on_tick
    assert _on_tick(0.97 - 0.02, 0.01, up=False) == "0.95"
    assert _on_tick(0.6449999, 0.01, up=False) == "0.64"
    assert _on_tick(0.123, 0.001, up=False) == "0.123"
    _, m, make = live
    c = FakeClient(_accepted(making="1.58", taking="1.4"))
    c.held = 1.6
    make(c).sell(m, "Up", 1.58, 0.97 - 0.02, [], 0.0)
    assert c.calls[0]["min_price"] == "0.95"
