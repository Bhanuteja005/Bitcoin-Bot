import math

import pytest

from polybot import journal
from polybot.broker import Broker
from polybot.config import Risk, Settings
from polybot.model import (EntryInputs, FeeSchedule, evaluate, evaluate_exit, fill_price,
                           prob_up, sigma_per_second)
from polybot.polymarket import parse_market
from polybot.risk import EntryState, check_entry
from polybot.window import parse_slug, window_at

SIGMA = 0.00004  # ~0.4 bp per sqrt(second): a typical quiet BTC hour


# ---- window -----------------------------------------------------------------
def test_slug_from_user_example():
    w = parse_slug("btc-updown-5m-1790679600")
    assert (w.start, w.end) == (1790679600, 1790679900)
    assert window_at(1790679600 + 299).slug == "btc-updown-5m-1790679600"
    assert window_at(1790679900).slug == "btc-updown-5m-1790679900"


def test_bad_slug_rejected():
    with pytest.raises(ValueError):
        parse_slug("btc-updown-5m-1790679601")


# ---- model ------------------------------------------------------------------
def test_flat_price_is_coin_flip():
    assert prob_up(100_000, 100_000, 240, SIGMA) == pytest.approx(0.5)


def test_lead_matters_more_as_time_runs_out():
    early = prob_up(100_050, 100_000, 280, SIGMA)
    late = prob_up(100_050, 100_000, 10, SIGMA)
    assert 0.5 < early < late < 1


def test_basis_keeps_tiny_late_leads_uncertain():
    # $2 up with 1s left: Binance and Chainlink can disagree by that much.
    assert prob_up(100_002, 100_000, 1, SIGMA) < 0.7


def test_sigma_scaling():
    closes = [100.0 * math.exp(0.001 * (i % 2)) for i in range(61)]
    assert sigma_per_second(closes) == pytest.approx(0.001 / math.sqrt(60), rel=1e-3)  # returns alternate +/-0.1%


def test_fee_peaks_at_half():
    f = FeeSchedule(rate=0.25, exponent=2, enabled=True)
    assert f.per_share(0.5) > f.per_share(0.9) > 0
    assert FeeSchedule().per_share(0.5) == 0


def test_fill_walks_book():
    avg, worst = fill_price([(0.50, 1.0), (0.60, 10.0)], 1.10)  # 0.50 on first, 0.60 on rest
    assert worst == 0.60 and 0.5 < avg < 0.6
    assert fill_price([(0.5, 1.0)], 5.0) is None


def _inputs(price, secs, up_ask, down_ask, fees=FeeSchedule()):
    return EntryInputs(price=price, open_price=100_000, seconds_left=secs, sigma_s=SIGMA, stake_usd=1.0,
                       up_asks=[(up_ask, 500)], up_bids=[(up_ask - 0.01, 500)],
                       down_asks=[(down_ask, 500)], down_bids=[(down_ask - 0.01, 500)], fees=fees)


def test_no_trade_when_book_is_fair():
    d = evaluate(_inputs(100_000, 200, 0.51, 0.51), Risk())
    assert d.action == "NO_TRADE"


def test_buys_the_lagging_side():
    # BTC $28 up with 60s left: fair Up ~0.80, book still offers Up at 0.70 (edge ~10c).
    d = evaluate(_inputs(100_028, 60, 0.70, 0.32), Risk())
    assert d.action == "BUY_UP"
    assert 0.70 <= d.max_price <= Risk().max_entry_price
    assert d.max_price <= d.up.fair - Risk().min_edge / 2 + 1e-9


def test_refuses_expensive_and_late():
    assert evaluate(_inputs(100_500, 60, 0.95, 0.06), Risk()).action == "NO_TRADE"  # above max entry
    assert evaluate(_inputs(100_120, 5, 0.70, 0.32), Risk()).action == "NO_TRADE"  # past cutoff


def test_refuses_long_shots_even_with_edge():
    # BTC $30 below strike with 40s left: Up fair ~0.1, offered at 0.05. Positive EV, but
    # a 90% chance of losing the whole bankroll.
    d = evaluate(_inputs(99_970, 40, 0.05, 0.96), Risk())
    assert d.action == "NO_TRADE"


def test_exit_sells_only_when_bid_beats_fair():
    fees = FeeSchedule()
    assert evaluate_exit(0.70, [(0.80, 100)], 2, 0.5, fees).action == "SELL"
    assert evaluate_exit(0.70, [(0.60, 100)], 2, 0.5, fees).action == "HOLD"
    assert evaluate_exit(0.70, [], 2, 0.5, fees).action == "HOLD"


# ---- market parsing -----------------------------------------------------------
def test_parse_market_shapes():
    m = parse_market({"slug": "btc-updown-5m-1790679600", "outcomes": '["Up", "Down"]',
                      "clobTokenIds": '["111", "222"]', "orderMinSize": 5, "orderPriceMinTickSize": 0.01,
                      "acceptingOrders": True, "feesEnabled": True, "feeSchedule": {"rate": 0.25, "exponent": 2}})
    assert m.token("up") == "111" and m.token("Down") == "222" and m.fees.enabled


# ---- risk -------------------------------------------------------------------
def _state(**kw):
    base = dict(kill=False, live=False, geoblock_ok=None, cash_usd=1.0, realised_today=0.0,
                trades_today=0, open_in_window=False, seconds_left=120)
    return EntryState(**{**base, **kw})


def test_risk_passes_clean_paper_order():
    assert check_entry(Risk(), _state(), 1.0, 0.7) == []


@pytest.mark.parametrize("kw,stake,fragment", [
    ({"kill": True}, 1.0, "kill"),
    ({"live": True, "geoblock_ok": False}, 1.0, "geoblock"),
    ({"live": True, "geoblock_ok": None}, 1.0, "geoblock"),
    ({"cash_usd": 0.5}, 1.0, "available cash"),
    ({}, 2.0, "max stake"),
    ({"realised_today": -1.0}, 1.0, "daily loss"),
    ({"open_in_window": True}, 1.0, "one position"),
    ({"seconds_left": 5}, 1.0, "cutoff"),
])
def test_risk_refusals(kw, stake, fragment):
    assert any(fragment in r for r in check_entry(Risk(max_stake_usd=1.0), _state(**kw), stake, 0.7))


# ---- broker + journal (paper) -----------------------------------------------
@pytest.fixture
def paper(tmp_path):
    return Settings(data_dir=tmp_path)


def _market():
    return parse_market({"slug": "btc-updown-5m-1790679600", "outcomes": '["Up","Down"]',
                         "clobTokenIds": '["111","222"]', "acceptingOrders": True})


def test_paper_round_trip_and_duplicate_guard(paper):
    b, m = Broker(paper), _market()
    assert b.cash() == pytest.approx(1.0)
    f = b.buy(m, "Up", 1.0, 0.60, [(0.50, 100)], 0.0)
    assert f.ok and f.mode == "paper" and f.shares == pytest.approx(2.0)
    assert b.cash() == pytest.approx(0.0)
    assert not b.buy(m, "Up", 1.0, 0.60, [(0.50, 100)], 0.0).ok  # duplicate within 20s
    s = b.sell(m, "Up", 2.0, 0.70, [(0.75, 100)], 0.0)
    assert s.ok and s.usd == pytest.approx(1.5)
    book = journal.replay(journal.rows(paper.data_dir))
    assert not book.positions
    assert sum(book.realised_by_day.values()) == pytest.approx(0.5)
    assert b.cash() == pytest.approx(1.5)


def test_paper_fok_respects_price_cap(paper):
    f = Broker(paper).buy(_market(), "Down", 1.0, 0.40, [(0.45, 100)], 0.0)
    assert not f.ok and "FOK" in f.detail


def test_dry_run_is_default_and_live_needs_both_switches():
    assert not Settings().is_live
    assert not Settings(mode="live").is_live
    assert Settings(mode="live", live_confirmed=True).is_live


# ---- TWAP settlement --------------------------------------------------------
from polybot.model import prob_up_twap  # noqa: E402


def test_twap_zero_is_snapshot():
    assert prob_up_twap(100_050, 100_000, 90, SIGMA, 0) == prob_up(100_050, 100_000, 90, SIGMA)


def test_twap_averaging_makes_leads_more_certain_before_the_tail():
    # Same lead, same time: the average moves less than the endpoint.
    assert prob_up_twap(100_050, 100_000, 90, SIGMA, 60) > prob_up(100_050, 100_000, 90, SIGMA)


def test_twap_locks_in_the_realised_part():
    # 10s left: price just dipped below strike, but the 50s already averaged were well above.
    p = prob_up_twap(99_990, 100_000, 10, SIGMA, 60, realised_avg=100_080)
    assert p > 0.95
    # ...and a late spike cannot rescue a tail that averaged below.
    assert prob_up_twap(100_060, 100_000, 5, SIGMA, 60, realised_avg=99_900) < 0.05


def test_fee_is_conservative_upper_bound():
    f = FeeSchedule(rate=0.07, exponent=1, enabled=True)
    assert f.per_share(0.5) == pytest.approx(0.0175)
    assert FeeSchedule(enabled=True).per_share(0.5) == pytest.approx(0.0175)  # no schedule -> 0.07


def test_dotenv_inline_comments_and_quotes(tmp_path):
    from polybot.config import _load_dotenv
    f = tmp_path / ".env"
    f.write_text('A=1.0          # paper cash\nB="x # not a comment"  # c\nC=abc#def\n# D=1\nE=\n')
    assert _load_dotenv(f) == {"A": "1.0", "B": "x # not a comment", "C": "abc#def", "E": ""}


def test_trade_needs_edge_under_every_settlement_reading():
    # 20s left. Last 40s averaged well above strike, but the first 4 minutes averaged below:
    # the 60s reading says Up is near-certain, the 300s reading says Down.
    base = _inputs(100_060, 20, 0.88, 0.14)
    agree = EntryInputs(**{**{f: getattr(base, f) for f in base.__slots__},
                           "settle": ((60.0, 100_060.0), (300.0, 100_055.0))})
    split = EntryInputs(**{**{f: getattr(base, f) for f in base.__slots__},
                           "settle": ((60.0, 100_060.0), (300.0, 99_950.0))})
    assert evaluate(agree, Risk()).action == "BUY_UP"
    d = evaluate(split, Risk())
    assert d.action == "NO_TRADE"
    assert d.up.fair < 0.5 < 1 - d.down.fair  # Up valued at the low reading, Down at the high


# ---- sizing -----------------------------------------------------------------
from polybot.model import kelly_stake  # noqa: E402


def test_kelly_scales_with_edge_and_respects_caps():
    r = Risk(max_stake_usd=10.0, daily_loss_limit_usd=3.0)
    small = kelly_stake(0.60, 0.52, 10.0, r)  # f* = 0.167 -> quarter = 4% -> below $1 min -> $1
    big = kelly_stake(0.90, 0.60, 10.0, r)  # f* = 0.75 -> quarter = 18.75% -> $1.87
    assert small == 1.0 and big == 1.87
    half = Risk(max_stake_usd=10.0, kelly_fraction=0.5)
    assert kelly_stake(0.99, 0.50, 10.0, half) == 2.5  # half-Kelly wants $4.90: capped at 25% of cash
    assert kelly_stake(0.99, 0.50, 100.0, half) == 10.0  # ...and at the max stake
    assert kelly_stake(0.99, 0.50, 10.0, r) == 2.45  # quarter-Kelly stays under the cap by itself
    assert kelly_stake(0.50, 0.55, 10.0, r) == 0.0  # no edge, no bet
    assert kelly_stake(0.90, 0.60, 3.0, r) == 0.0  # $1 min > 25% of $3: sit out


def test_stop_loss_salvages_when_chance_collapses():
    fees = FeeSchedule()
    # Chance fell to 20%, bid 0.15 is below fair (no take-profit) -> stop sells anyway.
    a = evaluate_exit(0.20, [(0.15, 100)], 20, 0.5, fees, stop_fair=0.25)
    assert a.action == "SELL" and "STOP-LOSS" in a.reasons[0]
    assert a.unrealised_usd == pytest.approx((0.15 - 0.5) * 20)  # -$7 instead of -$10
    # Still 40% to win: hold.
    assert evaluate_exit(0.40, [(0.35, 100)], 20, 0.5, fees, stop_fair=0.25).action == "HOLD"
    # Nothing left to save: hold.
    assert evaluate_exit(0.02, [(0.01, 100)], 20, 0.5, fees, stop_fair=0.25).action == "HOLD"
    # Stop disabled by default for callers that do not pass it.
    assert evaluate_exit(0.20, [(0.15, 100)], 20, 0.5, fees).action == "HOLD"


def test_high_priced_entries_need_bigger_edge():
    # $50 ahead, 60s left: Up fair ~0.935. At 0.88 the edge is ~5.5c: enough at a normal
    # price, not enough above 0.80. At $54 ahead the edge clears 6c and it buys.
    d = evaluate(_inputs(100_050, 60, 0.88, 0.14), Risk())
    assert d.up.edge is not None and 0.04 <= d.up.edge < 0.06
    assert d.action == "NO_TRADE" and "above 0.80" in d.reasons[0]
    assert evaluate(_inputs(100_054, 60, 0.88, 0.14), Risk()).action == "BUY_UP"


# ---- macro blackout ---------------------------------------------------------
from datetime import datetime as _dt  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from polybot.window import in_blackout  # noqa: E402


def _ny(h, m):
    return _dt(2026, 9, 29, h, m, 30, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_blackout_covers_release_windows_only():
    spec = "08:25-08:45,13:55-14:45"
    assert in_blackout(_ny(8, 31), spec) == "08:25-08:45"  # CPI/NFP print
    assert in_blackout(_ny(8, 22), spec) is None  # 08:20-08:25 ends as the blackout starts
    assert in_blackout(_ny(14, 40), spec) == "13:55-14:45"  # FOMC press conference
    assert in_blackout(_ny(9, 30), spec) is None
    assert in_blackout(_ny(8, 46), spec) is None
    assert in_blackout(_ny(8, 31), "") is None


def test_implausibly_large_edge_is_refused():
    # BTC far ahead with 30s left and Up offered at 0.55: a 40c "edge" is a data problem.
    d = evaluate(_inputs(100_200, 30, 0.55, 0.47), Risk())
    assert d.action == "NO_TRADE" and any("bad data" in r for r in d.reasons)


def _with(x, **kw):
    return EntryInputs(**{**{f: getattr(x, f) for f in x.__slots__}, **kw})


def test_small_z_is_noise_not_a_lead():
    # BTC $5 up with 200s left: tiny z. Even a cheap Up ask is refused.
    d = evaluate(_inputs(100_005, 200, 0.45, 0.56), Risk())
    assert d.action == "NO_TRADE"
    assert any("|z|" in r for r in d.reasons)


def test_reversal_toward_strike_blocks_entry():
    base = _inputs(100_028, 60, 0.70, 0.32)
    assert evaluate(base, Risk()).action == "BUY_UP"
    # Same lead, but BTC fell $60 in the last two minutes to get here: skip.
    d = evaluate(_with(base, recent_move=-60.0), Risk())
    assert d.action == "NO_TRADE"
    assert any("reversing" in r for r in d.reasons)
    # A move in the trade's favour does not block it.
    assert evaluate(_with(base, recent_move=+60.0), Risk()).action == "BUY_UP"


def test_identity_calibration_leaves_the_model_alone():
    from polybot.config import Calibration
    from polybot.model import calibrate
    assert calibrate(0.37, 0.5, Calibration()) == 0.37


def test_fit_recovers_a_market_leaning_calibration():
    import random
    from polybot.learn import fit
    from polybot.model import logit, sigmoid
    rng = random.Random(7)
    xs, ys = [], []
    for _ in range(4000):
        m = rng.uniform(0.05, 0.95)
        model = min(max(m + rng.uniform(-0.2, 0.2), 0.02), 0.98)  # a noisy model
        xs.append((logit(model), logit(m)))
        ys.append(int(rng.random() < m))  # the truth follows the market
    cal = fit(xs, ys, [1.0] * len(xs), intercept=True)
    assert cal.b_market > 0.7 and abs(cal.b_model) < 0.3
    assert abs(sigmoid(cal.a + cal.b_model * logit(0.5) + cal.b_market * logit(0.8)) - 0.8) < 0.08


def test_calibration_moves_the_decision():
    from polybot.config import Calibration
    base = _inputs(100_028, 60, 0.70, 0.32)
    assert evaluate(base, Risk()).action == "BUY_UP"
    # A calibration that trusts only the market sees no edge in the same book.
    trust_market = Calibration(a=0.0, b_model=0.0, b_market=1.0)
    assert evaluate(base, Risk(), trust_market).action == "NO_TRADE"


def test_favourite_strategy_waits_then_buys_the_favourite_in_band():
    from polybot.model import favourite_decision
    r = Risk(min_entry_price=0.60, max_entry_price=0.95)
    d = evaluate(_inputs(100_010, 110, 0.72, 0.30), r)
    assert favourite_decision(d, 130, r, 120, 0.01).action == "NO_TRADE"  # too early
    f = favourite_decision(d, 110, r, 120, 0.01)
    assert f.action == "BUY_UP" and 0.72 <= f.max_price <= 0.95
    assert favourite_decision(d, 10, r, 120, 0.01).action == "NO_TRADE"  # past the cutoff


def test_favourite_strategy_skips_when_the_favourite_is_out_of_band():
    from polybot.model import favourite_decision
    r = Risk(min_entry_price=0.60, max_entry_price=0.95)
    d = evaluate(_inputs(100_000, 110, 0.97, 0.04), r)  # favourite too dear
    assert favourite_decision(d, 110, r, 120, 0.01).action == "NO_TRADE"
    d = evaluate(_inputs(100_000, 110, 0.52, 0.50), r)  # no clear favourite
    assert favourite_decision(d, 110, r, 120, 0.01).action == "NO_TRADE"


def test_decision_keeps_the_raw_model_for_learning():
    from polybot.config import Calibration
    base = _inputs(100_028, 60, 0.70, 0.32)
    raw = evaluate(base, Risk())
    cal = evaluate(base, Risk(), Calibration(a=0.0, b_model=0.0, b_market=1.0))
    assert raw.raw_fair_up == raw.fair_up
    assert cal.raw_fair_up == raw.fair_up and cal.fair_up != raw.fair_up


def test_pct_stop_level():
    from polybot.model import pct_stop_level
    assert pct_stop_level(0.80, 0.40) == pytest.approx(0.48)
    assert pct_stop_level(0.80, 0.0) is None
