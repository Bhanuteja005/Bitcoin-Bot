import time

from polybot import brief, chainlink
from polybot.chainlink import ChainlinkFeed, _Series
from polybot.config import Settings
from polybot.window import window_at


def test_series_at_mean_and_dedupe():
    s = _Series()
    for t in range(100, 160):
        s.add(float(t), 1000.0 + t)
    s.add(120.0, 9999.0)  # out-of-order history point is ignored
    assert s.at(130.0) == 1130.0
    assert s.at(130.5) == 1130.0  # last point at or before
    assert s.at(200.0) is None  # too far past the last point
    assert s.mean(100, 110) == sum(1000.0 + t for t in range(100, 110)) / 10
    assert s.mean(150, 200) is None  # series stops at 159: coverage too thin


def test_price_inputs_prefer_chainlink_and_fall_back(monkeypatch):
    s = Settings()
    w = window_at(time.time())
    fake = ChainlinkFeed(s)
    now = time.time()
    for i in range(700):  # 700s of history, 1 point per second, ending now
        t = now - 700 + i
        fake.price.add(t, 100_000.0 + i)
        fake.twap60.add(t, 99_000.0)
    monkeypatch.setattr(chainlink, "feed", lambda *_a, **_k: fake)
    calls, sources = brief.price_inputs(s, w, now)
    assert sources["spot"] == "chainlink" and calls["spot"]() == 100_699.0
    assert sources["open_price"] == "chainlink" and calls["open_price"]() == 99_000.0
    assert brief.basis_for(s, sources) == s.risk.basis_chainlink_usd or brief.source_label(sources) == "mixed"

    empty = ChainlinkFeed(s)  # nothing received: every value falls back to Binance
    monkeypatch.setattr(chainlink, "feed", lambda *_a, **_k: empty)
    _, sources = brief.price_inputs(s, w, now)
    assert set(sources.values()) == {"binance"}
    assert brief.basis_for(s, sources) == s.risk.basis_usd


def test_never_mixes_feeds(monkeypatch):
    """Chainlink price but no Chainlink strike (feed connected mid-window): everything
    must come from Binance, or the feeds' offset shows up as a fake lead."""
    s = Settings()
    w = window_at(time.time())
    fake = ChainlinkFeed(s)
    now = time.time()
    for i in range(30):  # only the last 30s: covers "now" but not the window's open
        fake.price.add(now - 30 + i, 100_000.0)
    monkeypatch.setattr(chainlink, "feed", lambda *_a, **_k: fake)
    calls, sources = brief.price_inputs(s, w, now)
    assert set(sources.values()) == {"binance"}
    assert brief.source_label(sources) == "binance"
