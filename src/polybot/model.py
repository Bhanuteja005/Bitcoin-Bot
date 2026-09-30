"""Fair value, fees, edge and the entry/exit decision. Pure: no clock, no I/O.

The market resolves Up when BTC at the window close is >= BTC at the window
open (Chainlink BTC/USD). Over a few minutes BTC behaves close to a driftless
random walk, so the fair probability of Up is

    P(up) = Phi( ln(S/K) / sqrt(sigma^2 * t + basis^2) )

with S the current price, K the window-open price, sigma per-second volatility
and t seconds left. `basis` covers the gap between our feed (Binance) and the
settlement feed (Chainlink): it is why a $3 lead with 5 seconds left is not a
certainty.

Early in the window S ~= K, so P(up) ~= 0.5 and the book is also ~0.5: there is
no edge, and the model says so. Edge appears when the book lags a move.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from statistics import pstdev

from .config import Calibration, Risk


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sigma_per_second(closes_1m: list[float]) -> float:
    """Realised volatility of log returns, scaled from 1-minute to 1-second."""
    if len(closes_1m) < 3:
        raise ValueError("need at least 3 one-minute closes to estimate volatility")
    rets = [math.log(b / a) for a, b in zip(closes_1m, closes_1m[1:]) if a > 0 and b > 0]
    return pstdev(rets) / math.sqrt(60.0)


def blended_sigma(closes_1m: list[float]) -> float:
    """The larger of the hour and the last 15 minutes.

    Taking the max is deliberately conservative: when vol spikes the short
    window catches it, and a higher sigma pulls fair value toward 0.5, which
    means fewer, not more, confident trades.
    """
    long = sigma_per_second(closes_1m[-61:])
    short = sigma_per_second(closes_1m[-16:]) if len(closes_1m) >= 16 else long
    return max(long, short)


def prob_up(price: float, open_price: float, seconds_left: float, sigma_s: float,
            basis_usd: float = 8.0) -> float:
    if price <= 0 or open_price <= 0:
        raise ValueError("prices must be positive")
    b = basis_usd / price
    # Basis enters twice: the settlement feed can disagree at the open and at the close.
    denom = math.sqrt(sigma_s**2 * max(seconds_left, 0.0) + 2 * b**2)
    if denom == 0:
        return 1.0 if price >= open_price else 0.0
    return norm_cdf(math.log(price / open_price) / denom)


def prob_up_twap(price: float, strike: float, seconds_left: float, sigma_s: float, twap_s: float,
                 realised_avg: float | None = None, basis_usd: float = 8.0) -> float:
    """P(close TWAP >= strike) when settlement averages the last `twap_s` seconds.

    Since Aug 2026 Polymarket settles 5m crypto markets on a 60s Chainlink TWAP.
    Averaging shrinks the variance of the close: a Brownian average over L seconds
    has 1/3 the variance of the endpoint. Inside the averaging window, the part
    already printed is locked in (`realised_avg`) and only the rest can move.
    With twap_s == 0 this reduces to the snapshot model `prob_up`.
    """
    if twap_s <= 0:
        return prob_up(price, strike, seconds_left, sigma_s, basis_usd)
    L, t = float(twap_s), max(seconds_left, 0.0)
    if t >= L:
        mean, var = price, sigma_s**2 * ((t - L) + L / 3.0)
    else:
        done = L - t
        base = realised_avg if realised_avg is not None else price
        mean = (done * base + t * price) / L
        var = (t / L) ** 2 * sigma_s**2 * t / 3.0
    b = basis_usd / price
    denom = math.sqrt(var + 2 * b**2)
    if denom == 0:
        return 1.0 if mean >= strike else 0.0
    return norm_cdf(math.log(mean / strike) / denom)


P_CLIP = 0.005


def logit(p: float) -> float:
    p = min(max(p, P_CLIP), 1 - P_CLIP)
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x)) if x >= 0 else math.exp(x) / (1 + math.exp(x))


def calibrate(p_model: float, market_up: float | None, cal: Calibration) -> float:
    """Apply the learned correction. Without a market price the market term is dropped."""
    if cal.identity:
        return p_model
    x = cal.a + cal.b_model * logit(p_model)
    if market_up is not None:
        x += cal.b_market * logit(market_up)
    return sigmoid(x)


def market_mid_up(up_bids, up_asks, down_bids, down_asks) -> float | None:
    """The book's own P(Up): the Up mid, else 1 - the Down mid, else None."""
    for bids, asks, flip in ((up_bids, up_asks, False), (down_bids, down_asks, True)):
        if bids and asks:
            mid = (bids[0][0] + asks[0][0]) / 2
            return 1 - mid if flip else mid
    return None


def fair_up_range(price: float, strike: float, seconds_left: float, sigma_s: float,
                  settle: tuple[tuple[float, float | None], ...], basis_usd: float = 8.0) -> tuple[float, float]:
    """(lowest, highest) P(Up) across the settlement readings in `settle`."""
    ps = [prob_up_twap(price, strike, seconds_left, sigma_s, L, ra, basis_usd) for L, ra in settle]
    return min(ps), max(ps)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Polymarket taker fee per share, from the market's `feeSchedule`.

    Published formulas disagree on a leading factor of p (Jan 2026:
    rate*p*(p(1-p))^exp; mid-2026: rate*p(1-p)). We charge the larger,
    rate*(p(1-p))^exp, so an error in our reading costs trades, not money.
    `enabled` without a schedule falls back to the latest known rate, 0.07.
    """

    rate: float = 0.0
    exponent: float = 1.0
    enabled: bool = False

    def per_share(self, price: float) -> float:
        if not self.enabled:
            return 0.0
        rate = self.rate if self.rate > 0 else 0.07
        return rate * (price * (1 - price)) ** self.exponent


def fill_price(asks: list[tuple[float, float]], usd: float) -> tuple[float, float] | None:
    """Walk the asks (price ascending) for `usd` notional -> (avg price, worst price)."""
    remaining, shares, worst = usd, 0.0, 0.0
    for price, size in asks:
        take = min(size, remaining / price)
        shares += take
        remaining -= take * price
        worst = price
        if remaining <= 1e-9:
            return (usd / shares, worst)
    return None


def fill_price_sell(bids: list[tuple[float, float]], shares: float) -> tuple[float, float] | None:
    """Walk the bids (price descending) for `shares` -> (avg price, worst price)."""
    remaining, proceeds, worst = shares, 0.0, 0.0
    for price, size in bids:
        take = min(size, remaining)
        proceeds += take * price
        remaining -= take
        worst = price
        if remaining <= 1e-9:
            return (proceeds / shares, worst)
    return None


@dataclass(frozen=True, slots=True)
class SideQuote:
    outcome: str  # "Up" | "Down"
    fair: float
    best_bid: float | None
    best_ask: float | None
    avg_fill: float | None  # for the stake size
    worst_fill: float | None
    fee_per_share: float
    edge: float | None  # fair - avg_fill - fee


@dataclass(frozen=True, slots=True)
class EntryInputs:
    price: float
    open_price: float
    seconds_left: float
    sigma_s: float
    stake_usd: float
    up_asks: list[tuple[float, float]]
    up_bids: list[tuple[float, float]]
    down_asks: list[tuple[float, float]]
    down_bids: list[tuple[float, float]]
    fees: FeeSchedule
    tick: float = 0.01
    basis_usd: float = 8.0
    accepting_orders: bool = True
    # One (averaging seconds, realised average so far) pair per settlement reading.
    settle: tuple[tuple[float, float | None], ...] = ((0.0, None),)
    # BTC's change over the last ~2 minutes (same exchange at both ends, so no basis mixing).
    recent_move: float = 0.0


@dataclass(frozen=True, slots=True)
class Decision:
    action: str  # BUY_UP | BUY_DOWN | NO_TRADE
    fair_up: float  # midpoint of the range, for display
    z: float
    up: SideQuote
    down: SideQuote
    max_price: float | None
    reasons: list[str] = field(default_factory=list)
    raw_fair_up: float | None = None  # the model before calibration: what `pm learn` fits on


def _quote(outcome: str, fair: float, asks, bids, stake: float, fees: FeeSchedule) -> SideQuote:
    fp = fill_price(asks, stake) if asks else None
    avg, worst = fp if fp else (None, None)
    fee = fees.per_share(avg) if avg is not None else 0.0
    return SideQuote(
        outcome=outcome,
        fair=fair,
        best_bid=bids[0][0] if bids else None,
        best_ask=asks[0][0] if asks else None,
        avg_fill=avg,
        worst_fill=worst,
        fee_per_share=fee,
        edge=(fair - avg - fee) if avg is not None else None,
    )


def round_down(price: float, tick: float) -> float:
    return math.floor(price / tick + 1e-9) * tick


def evaluate(x: EntryInputs, risk: Risk, cal: Calibration = Calibration()) -> Decision:
    lo, hi = fair_up_range(x.price, x.open_price, x.seconds_left, x.sigma_s, x.settle, x.basis_usd)
    raw_mid = (lo + hi) / 2
    if not cal.identity:
        m = market_mid_up(x.up_bids, x.up_asks, x.down_bids, x.down_asks)
        lo, hi = calibrate(lo, m, cal), calibrate(hi, m, cal)
    fair_up = (lo + hi) / 2
    spread_move = x.sigma_s * math.sqrt(max(x.seconds_left, 1.0))
    z = math.log(x.price / x.open_price) / spread_move if spread_move else 0.0
    # Each side is valued at its least favourable reading: a trade must work under all of them.
    up = _quote("Up", lo, x.up_asks, x.up_bids, x.stake_usd, x.fees)
    down = _quote("Down", 1 - hi, x.down_asks, x.down_bids, x.stake_usd, x.fees)

    reasons: list[str] = []
    if not x.accepting_orders:
        reasons.append("market is not accepting orders")
    if x.seconds_left < risk.min_seconds_left:
        reasons.append(f"only {x.seconds_left:.0f}s left (< {risk.min_seconds_left}s): fill could land after the close")

    best = max((q for q in (up, down) if q.edge is not None), key=lambda q: q.edge, default=None)
    if best is None:
        reasons.append("no ask liquidity deep enough for the stake on either side")
    else:
        if best.edge > risk.max_edge:
            reasons.append(
                f"edge {best.edge:+.3f} ({best.outcome}) is above {risk.max_edge:.2f}: the model disagrees with "
                "the market by more than it plausibly can - treat it as bad data, not opportunity"
            )
        need = risk.high_price_min_edge if best.worst_fill > risk.high_price else risk.min_edge
        if best.edge < need:
            where = f" at prices above {risk.high_price:.2f}" if need != risk.min_edge else ""
            reasons.append(
                f"best edge {best.edge:+.3f} ({best.outcome}) is below the {need:.2f} minimum{where}: "
                "the book already prices what the model knows"
            )
        if best.worst_fill > risk.max_entry_price:
            reasons.append(f"{best.outcome} costs {best.worst_fill:.2f} > max entry {risk.max_entry_price:.2f}: payoff too thin for the basis risk")
        if best.worst_fill < risk.min_entry_price:
            reasons.append(f"{best.outcome} at {best.worst_fill:.2f} is a lottery ticket below {risk.min_entry_price:.2f}")
        if abs(z) < risk.min_abs_z:
            reasons.append(f"|z| {abs(z):.2f} is below {risk.min_abs_z:.2f}: the move is noise, not a lead")
        # Reversal: BTC has come back toward the strike, over the last ~2 minutes, by more
        # than the lead it still holds. The book usually sees this before the model does.
        lead = abs(x.price - x.open_price)
        toward = -x.recent_move if best.outcome == "Up" else x.recent_move
        if toward > lead:
            reasons.append(f"BTC moved ${toward:.0f} against {best.outcome} in the last ~2 min, more than "
                           f"its ${lead:.0f} lead: the move is reversing")

    if reasons or best is None:
        return Decision("NO_TRADE", fair_up, z, up, down, None, reasons, raw_mid)

    # Worst price we accept: never above the level where the edge halves, and
    # never more than `slippage` above what the book shows now.
    cap_by_edge = best.fair - best.fee_per_share - risk.min_edge / 2
    cap = round_down(min(best.worst_fill + risk.slippage_cents, cap_by_edge), x.tick)
    cap = max(cap, best.worst_fill)  # cap_by_edge >= worst_fill whenever edge >= min_edge
    action = "BUY_UP" if best.outcome == "Up" else "BUY_DOWN"
    why = [f"{best.outcome}: fair {best.fair:.3f} vs fill {best.avg_fill:.3f} + fee {best.fee_per_share:.3f} = edge {best.edge:+.3f}"]
    return Decision(action, fair_up, z, up, down, round(cap, 4), why, raw_mid)


def favourite_decision(d: Decision, seconds_left: float, risk: Risk, enter_at_s: float, tick: float) -> Decision:
    """Strategy "favourite": ignore the model's fair value and buy the side the book
    favours, once, when `enter_at_s` or fewer seconds are left and its $1 fill sits in
    [min_entry_price, max_entry_price]. The bet is the favourite-longshot bias: favourites
    in these markets have won more often than their price (our first 80 windows; a
    588M-trade Polymarket study). Unproven here - it runs on paper to find out."""
    if seconds_left > enter_at_s:
        return replace(d, action="NO_TRADE", max_price=None, reasons=[f"favourite: waiting for <= {enter_at_s:.0f}s left"])
    if seconds_left < risk.min_seconds_left:
        return replace(d, action="NO_TRADE", max_price=None, reasons=[f"only {seconds_left:.0f}s left"])
    band = [q for q in (d.up, d.down) if q.worst_fill is not None and q.worst_fill >= 0.5
            and risk.min_entry_price <= q.worst_fill <= risk.max_entry_price]
    if not band:
        return replace(d, action="NO_TRADE", max_price=None, reasons=[
            f"favourite: no side fills in {risk.min_entry_price:.2f}-{risk.max_entry_price:.2f}"])
    q = max(band, key=lambda q: q.worst_fill)
    cap = max(round_down(min(q.worst_fill + risk.slippage_cents, risk.max_entry_price), tick), q.worst_fill)
    return replace(d, action="BUY_UP" if q.outcome == "Up" else "BUY_DOWN", max_price=round(cap, 4),
                   reasons=[f"favourite {q.outcome} fills at {q.worst_fill:.2f} with {seconds_left:.0f}s left"])


def kelly_stake(fair: float, cost_per_share: float, cash: float, risk: Risk) -> float:
    """Fractional-Kelly stake for a binary share that pays $1.

    Full Kelly for paying c (fees included) on win probability q is f* = (q - c) / (1 - c)
    of the bankroll. Full Kelly is too aggressive when q is a model estimate, so we
    bet `kelly_fraction` of it, clamp to [min order, max_bankroll_frac * cash, max stake],
    and return 0 when even the minimum order would exceed that ceiling.
    """
    if cost_per_share >= 1 or fair <= cost_per_share or cash <= 0:
        return 0.0
    f_star = (fair - cost_per_share) / (1 - cost_per_share)
    ceiling = min(risk.max_stake_usd, risk.max_bankroll_frac * cash, cash)
    if ceiling < risk.min_order_usd:
        return 0.0
    stake = max(risk.min_order_usd, risk.kelly_fraction * f_star * cash)
    return math.floor(min(stake, ceiling) * 100) / 100  # whole cents


@dataclass(frozen=True, slots=True)
class ExitAdvice:
    action: str  # SELL | HOLD
    fair: float
    bid: float | None
    net_bid: float | None  # bid after fee
    unrealised_usd: float | None
    reasons: list[str]


def evaluate_exit(fair: float, bids: list[tuple[float, float]], shares: float,
                  entry_price: float, fees: FeeSchedule, exit_margin: float = 0.02,
                  stop_fair: float = 0.0, stop_min_bid: float = 0.03) -> ExitAdvice:
    """Sell when the book pays more than the position is worth, or when the stop trips.

    Take-profit: if BTC has moved your way and a bidder offers more than fair
    value, take it.

    Stop-loss: once the held side's chance of winning falls to `stop_fair`,
    sell at the bid and keep what is left. On expected value alone, holding is
    usually slightly better (the bid tends to sit below fair), but a position
    that is ~75% likely to go to zero is where a whole stake disappears, and the
    bankroll's owner has chosen to cap that. Below `stop_min_bid` there is
    nothing left to save, so hold.
    """
    fp = fill_price_sell(bids, shares) if bids else None
    if fp is None:
        return ExitAdvice("HOLD", fair, None, None, None, ["no bid depth for the full position: hold to resolution"])
    avg, _ = fp
    net = avg - fees.per_share(avg)
    pnl = (net - entry_price) * shares
    if net >= fair + exit_margin:
        return ExitAdvice("SELL", fair, avg, net, pnl,
                          [f"bid nets {net:.3f} > fair {fair:.3f} + {exit_margin:.2f}: the market is overpaying, lock it in"])
    if fair <= stop_fair and avg >= stop_min_bid:
        return ExitAdvice("SELL", fair, avg, net, pnl,
                          [f"STOP-LOSS: chance of winning {fair:.0%} <= {stop_fair:.0%}; salvage {net:.3f}/share instead of riding to $0"])
    return ExitAdvice("HOLD", fair, avg, net, pnl,
                      [f"bid nets {net:.3f} vs fair {fair:.3f}: holding to resolution has higher expected value"])


def pct_stop_level(entry_price: float, pct: float) -> float | None:
    """Bid at which a position has lost `pct` of its entry price (0.40 -> sell once the
    bid is 60% of what was paid). None when the stop is off. Backtest on Bot B's 22
    real trades: -40% of entry was the only stop still positive after 8c slippage."""
    if pct <= 0:
        return None
    return entry_price * (1 - pct)
