"""Real-time autopilot rehearsal: real clock + real Binance BTC, simulated Polymarket book.

The simulated book is a market maker that quotes fair value computed from the BTC
price LAG seconds ago, +/- a half-spread. It exists to exercise timing, entry,
exit, settlement and P&L end to end when Polymarket is unreachable. Its P&L says
nothing about real profitability: the lag is a parameter we chose.

    uv run python scripts/sim_live.py --rounds 2 [--lag 4]
"""

import argparse
import collections
import os
import sys
import time

os.environ.setdefault("PM_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data-sim"))

from polybot import cli, feeds, polymarket  # noqa: E402
from polybot.config import load  # noqa: E402
from polybot.model import blended_sigma, prob_up_twap  # noqa: E402
from polybot.polymarket import parse_market  # noqa: E402
from polybot.window import parse_slug  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--rounds", type=int, default=2)
ap.add_argument("--lag", type=float, default=4.0)
ap.add_argument("--half-spread", type=float, default=0.01)
args, rest = ap.parse_known_args()

s = load()
history: collections.deque = collections.deque(maxlen=600)  # (t, spot)
real_spot = feeds.spot
sigma = blended_sigma([c.close for c in feeds.klines(s.binance_host, "1m", 61)])
opens: dict[int, float] = {}
tokens: dict[str, tuple[str, str]] = {}


def spot(host):
    p = real_spot(host)
    history.append((time.time(), p))
    return p


def market(_host, slug):
    w = parse_slug(slug)
    tokens[f"U{w.start}"] = (slug, "Up")
    tokens[f"D{w.start}"] = (slug, "Down")
    return parse_market({"slug": slug, "question": f"SIM Bitcoin Up or Down {w.label()}", "outcomes": '["Up","Down"]',
                         "clobTokenIds": f'["U{w.start}","D{w.start}"]', "acceptingOrders": True,
                         "orderMinSize": 5, "orderPriceMinTickSize": 0.01, "feesEnabled": True,
                         "feeSchedule": {"rate": 0.07, "exponent": 1}})


def lagged_spot():
    cutoff = time.time() - args.lag
    old = [p for t, p in history if t <= cutoff]
    return old[-1] if old else (history[0][1] if history else real_spot(s.binance_host))


def book(_host, token):
    slug, outcome = tokens[token]
    w = parse_slug(slug)
    L = s.risk.twap_s
    if w.start not in opens:
        opens[w.start] = feeds.twap(s.binance_host, w.start - L, L)[0] if L else feeds.open_at(s.binance_host, w.start)
    now = time.time() - args.lag
    left = max(0.0, w.end - now)
    tail = [p for t, p in history if w.end - L <= t <= now]
    fu = prob_up_twap(lagged_spot(), opens[w.start], left, sigma, L, sum(tail) / len(tail) if tail else None)
    mid = fu if outcome == "Up" else 1 - fu
    bid = max(0.01, round(mid - args.half_spread, 2))
    ask = min(0.99, round(mid + args.half_spread + 0.005, 2))
    return [(bid, 150.0), (round(bid - 0.01, 2), 400.0)], [(ask, 150.0), (round(ask + 0.01, 2), 400.0)]


feeds.spot = spot
polymarket.market_by_slug = market
polymarket.book = book
print(f"SIM: real BTC via Binance, simulated book lagging {args.lag}s, sigma {sigma * 1e4:.2f} bp/sqrt(s), "
      f"data in {s.data_dir}", flush=True)
sys.exit(cli.main(["auto", "--rounds", str(args.rounds), *rest]))
