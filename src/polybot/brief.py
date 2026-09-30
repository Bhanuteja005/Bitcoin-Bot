"""Assemble a trading brief: one parallel fetch, one model evaluation, one snapshot.

The brief is what the analyst (Claude, following prompts/btc-5m-trader.md) reads
before deciding. It is also written to data/briefs/ so every decision can be
replayed against exactly what was seen.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from . import chainlink, feeds, polymarket
from .config import Settings
from .model import Decision, EntryInputs, blended_sigma, evaluate
from .net import FeedError, parallel
from .polymarket import Market
from .window import Window, window_at

BRIEF_MAX_AGE_S = 20  # a 5-minute market moves too fast to trade on anything older


@dataclass(slots=True)
class Brief:
    id: str
    fetched_at: float
    clock_skew_s: float | None
    window: Window
    seconds_left: float
    market: Market
    btc: float
    open_price: float
    sigma_s: float
    candles: list[feeds.Candle]
    books: dict[str, tuple[list, list]]  # outcome -> (bids, asks)
    decision: Decision
    stake_usd: float
    source: str = "binance"

    def age(self) -> float:
        return time.time() - self.fetched_at


def price_inputs(s: Settings, w: Window, now: float) -> tuple[dict, dict]:
    """Callables for spot, strike and realised averages, preferring live Chainlink.

    Chainlink is what the market settles on; Binance is the fallback, used per
    value whenever the Chainlink buffer does not cover it (just started, stale,
    or disconnected). Returns (callables, sources) so the brief can say which
    feed each number came from.
    """
    cl = chainlink.feed(s)
    calls = strike_fetchers(s, w, now)
    calls["spot"] = lambda: feeds.spot(s.binance_host)
    binance = {k: "binance" for k in calls}
    # All or nothing: Binance BTCUSDT and Chainlink BTC/USD sit several dollars apart,
    # so a Chainlink price against a Binance strike manufactures a lead that is not
    # there. That exact mix produced a live trade with a fake 26c "edge".
    px, k = cl.price_now(), cl.strike(w.start)
    if px is None or k is None:
        return calls, binance
    cl_calls = {"spot": (lambda px=px: px), "open_price": (lambda k=k: k)}
    for L in s.risk.settle_windows:
        key = f"realised_{L}"
        if key in calls:
            v = cl.realised_mean(w.end - L, int(now))
            if v is None:
                return calls, binance
            cl_calls[key] = (lambda v=v: v)
    return cl_calls, {key: "chainlink" for key in cl_calls}


def source_label(sources: dict) -> str:
    kinds = set(sources.values())
    return kinds.pop() if len(kinds) == 1 else "mixed"


def strike_fetchers(s: Settings, w: Window, now: float) -> dict:
    """Callables for the strike and, per settlement reading, the average printed so far.

    Rules text: Up if the Chainlink TWAP "of the time range in the title" is >= "the
    price at the beginning of that range", sourced from the 60s TWAP stream. Strike is
    read as that stream at the open (average of [start - 60, start)). The close is
    priced under each window in PM_SETTLE_WINDOWS (60s tail and full 300s window).
    """
    SL = s.risk.strike_twap_s
    out = {"open_price": (lambda: feeds.twap(s.binance_host, w.start - SL, SL)[0]) if SL > 0
           else (lambda: feeds.open_at(s.binance_host, w.start))}
    for L in s.risk.settle_windows:
        # The current second's candle is still forming and may not be published yet,
        # so only completed seconds count toward the realised average.
        elapsed = int(now - (w.end - L)) - 1
        if L > 0 and elapsed >= 1:
            n = min(L, elapsed)
            out[f"realised_{L}"] = (lambda L=L, n=n: feeds.twap(s.binance_host, w.end - L, n)[0])
    return out


def basis_for(s: Settings, sources: dict) -> float:
    return s.risk.basis_chainlink_usd if source_label(sources) == "chainlink" else s.risk.basis_usd


def settle_tuple(s: Settings, r: dict) -> tuple[tuple[float, float | None], ...]:
    return tuple((float(L), r.get(f"realised_{L}")) for L in s.risk.settle_windows)


def build(s: Settings, stake_usd: float, window: Window | None = None, market: Market | None = None) -> Brief:
    """`market`: pass the window's market when already known. Its token ids let both
    books be fetched in the same parallel round as everything else, instead of
    after a Gamma lookup - about a second off the decision-to-order path."""
    now = time.time()
    w = window or window_at(now)
    calls, sources = price_inputs(s, w, now)
    extra = {}
    if market is None:
        extra["market"] = lambda: polymarket.market_by_slug(s.gamma_host, w.slug)
    else:
        extra["Up"] = lambda: polymarket.book(s.clob_host, market.token_up)
        extra["Down"] = lambda: polymarket.book(s.clob_host, market.token_down)
    r = parallel(
        candles=lambda: feeds.klines(s.binance_host, "1m", 61),
        server=lambda: feeds.server_time(s.binance_host),
        **extra,
        **calls,
    )
    if market is not None:
        r["market"] = market
    for name in ("market", "spot", "candles", "open_price", *(k for k in r if k.startswith("realised_"))):
        if isinstance(r[name], Exception):
            raise FeedError(f"{name}: {r[name]}")
    m: Market = r["market"]
    books = ({"Up": r["Up"], "Down": r["Down"]} if market is not None else
             parallel(Up=lambda: polymarket.book(s.clob_host, m.token_up),
                      Down=lambda: polymarket.book(s.clob_host, m.token_down)))
    for k, v in books.items():
        if isinstance(v, Exception):
            raise FeedError(f"{k} book: {v}")

    fetched = time.time()
    skew = (r["server"] - (now + fetched) / 2) if not isinstance(r["server"], Exception) else None
    # Use exchange time, not the PC clock: Windows clocks drift by seconds and
    # "seconds left" is the most important input in the model.
    true_now = fetched + (skew or 0.0)
    candles = r["candles"]
    sigma = blended_sigma([c.close for c in candles])
    secs = w.seconds_left(true_now)
    x = EntryInputs(price=r["spot"], open_price=r["open_price"], seconds_left=secs, sigma_s=sigma,
                    stake_usd=stake_usd, up_bids=books["Up"][0], up_asks=books["Up"][1],
                    down_bids=books["Down"][0], down_asks=books["Down"][1], fees=m.fees, tick=m.tick,
                    accepting_orders=m.accepting_orders and not m.closed,
                    settle=settle_tuple(s, r), basis_usd=basis_for(s, sources),
                    recent_move=candles[-1].close - candles[-3].close if len(candles) >= 3 else 0.0)
    d = evaluate(x, s.risk, s.calibration)
    bid = f"{w.slug}-{int(fetched)}"
    b = Brief(bid, fetched, skew, w, secs, m, r["spot"], r["open_price"], sigma, candles, books, d, stake_usd,
              source_label(sources))
    _save(s, b)
    return b


def _save(s: Settings, b: Brief) -> None:
    d = s.data_dir / "briefs"
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": b.id, "fetched_at": b.fetched_at, "slug": b.window.slug, "seconds_left": b.seconds_left,
        "btc": b.btc, "open_price": b.open_price, "sigma_s": b.sigma_s, "stake_usd": b.stake_usd,
        "source": b.source,
        "decision": asdict(b.decision),
        "books": {k: {"bids": v[0][:5], "asks": v[1][:5]} for k, v in b.books.items()},
    }
    (d / f"{b.id}.json").write_text(json.dumps(payload, indent=1))


def _lvl(levels: list, n: int = 3) -> str:
    return "  ".join(f"{p:.2f}x{sz:,.0f}" for p, sz in levels[:n]) or "(empty)"


def render(b: Brief) -> str:
    d = b.decision
    move = b.btc - b.open_price
    last = b.candles[-6:]
    tape = "\n".join(
        f"    {time.strftime('%H:%M', time.gmtime(c.ts))}  o {c.open:,.1f}  h {c.high:,.1f}  l {c.low:,.1f}  c {c.close:,.1f}  "
        f"{'▲' if c.close >= c.open else '▼'} {c.close - c.open:+,.1f}"
        for c in last)
    lines = [
        f"BRIEF {b.id}   ({b.window.label()}, {b.seconds_left:.0f}s left"
        + (f", clock skew {b.clock_skew_s:+.1f}s" if b.clock_skew_s is not None else "") + ")",
        f"  {b.market.question}",
        f"  BTC {b.btc:,.2f}  vs strike {b.open_price:,.2f} [{b.source}]  →  {move:+,.2f} ({move / b.open_price * 1e4:+.1f} bp)",
        f"  vol σ {b.sigma_s * 1e4:.2f} bp/√s  |  z {d.z:+.2f}  |  model P(Up) {d.up.fair:.3f}-{1 - d.down.fair:.3f} (across settlement readings)",
        f"  fees: {'rate ' + str(b.market.fees.rate) + ', exp ' + str(b.market.fees.exponent) if b.market.fees.enabled else 'none'}"
        f"  |  tick {b.market.tick}  min size {b.market.min_size:g}  |  accepting {b.market.accepting_orders}",
        "  last 1m candles (Binance):",
        tape,
    ]
    for q in (d.up, d.down):
        book = b.books[q.outcome]
        edge = f"{q.edge:+.3f}" if q.edge is not None else "n/a"
        fill = f"{q.avg_fill:.3f}" if q.avg_fill is not None else "n/a"
        lines.append(f"  {q.outcome:<4} fair {q.fair:.3f} | bid {_lvl(book[0])} | ask {_lvl(book[1])} | "
                     f"fill ${b.stake_usd:g} @ {fill} fee {q.fee_per_share:.3f} → edge {edge}")
    verdict = d.action + (f"  (max price {d.max_price:.2f})" if d.max_price else "")
    lines.append(f"  MODEL: {verdict}")
    lines += [f"    - {r}" for r in d.reasons]
    return "\n".join(lines)
