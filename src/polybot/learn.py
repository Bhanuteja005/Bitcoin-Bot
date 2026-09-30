"""Learn from every scan the bot has made (`pm learn`).

Each brief in data/briefs is one scan: the model's P(Up), the book, the time left.
Once its window resolves, that scan is a labelled example. From them we fit the
`Calibration` in config.py - a logistic correction on the model's probability and the
market's own mid - and accept it only if it beats the current model on later windows
it was not fitted on. Scans inside one window are near-duplicates, so every window
carries the same total weight however many scans it has.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from . import polymarket
from .config import CALIBRATION_FILE, Calibration, Settings
from .model import calibrate, logit, market_mid_up
from .net import FeedError

OUTCOMES_FILE = "outcomes.json"
# From the first saved calibration (2026-09-30 13:34 UTC) until briefs gained `raw_fair_up`,
# a brief's `fair_up` was the calibrated value. Those scans cannot be refit on, so skip them.
CALIBRATED_SINCE = 1790776440
MIN_WINDOWS = 40  # below this, a fit is noise
TEST_FRACTION = 0.3


@dataclass(frozen=True, slots=True)
class Scan:
    start: int
    secs: float
    z: float
    p_model: float
    m_up: float
    fill_up: float | None
    fill_down: float | None


def load_scans(data_dir: Path, zone_s: float, min_s: float) -> list[Scan]:
    out = []
    for f in (data_dir / "briefs").glob("*.json"):
        try:
            b = json.loads(f.read_text(encoding="utf-8"))
            d, up, dn = b["decision"], b["decision"]["up"], b["decision"]["down"]
        except (OSError, ValueError, KeyError):
            continue
        if not (min_s <= b["seconds_left"] <= zone_s):
            continue  # only where the bot actually decides
        p_raw = d.get("raw_fair_up")
        if p_raw is None:
            if b.get("fetched_at", 0) >= CALIBRATED_SINCE:
                continue
            p_raw = d["fair_up"]
        m = market_mid_up(*(([(q[k], 0)] if q.get(k) is not None else [])
                            for q in (up, dn) for k in ("best_bid", "best_ask")))
        if m is None:
            continue
        out.append(Scan(int(b["slug"].rsplit("-", 1)[1]), b["seconds_left"], d["z"], p_raw, m,
                        up.get("avg_fill"), dn.get("avg_fill")))
    return out


def outcomes(s: Settings, starts: set[int], now: float) -> dict[int, str]:
    """Winner per window from Polymarket's resolution, cached in data/outcomes.json."""
    path = s.data_dir / OUTCOMES_FILE
    cache: dict[str, str] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for start in sorted(starts):
        if str(start) in cache or now < start + 300 + 60:
            continue
        try:
            m = polymarket.market_by_slug(s.gamma_host, f"btc-updown-5m-{start}")
        except FeedError:
            continue
        if m.closed and m.outcome_prices and max(m.outcome_prices) >= 0.99:
            cache[str(start)] = "Up" if m.outcome_prices[0] > m.outcome_prices[1] else "Down"
    path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")
    return {int(k): v for k, v in cache.items()}


# ---------------------------------------------------------------- fitting (pure)
def _solve3(a: list[list[float]], b: list[float]) -> list[float]:
    m = [row[:] + [v] for row, v in zip(a, b)]
    n = len(m)
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c and m[c][c]:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] if m[i][i] else 0.0 for i in range(n)]


def fit(xs: list[tuple[float, float]], ys: list[int], ws: list[float], ridge: float = 1e-3,
        intercept: bool = False) -> Calibration:
    """Weighted logistic regression y ~ sigmoid(a + b1*x1 + b2*x2), Newton's method.
    A small ridge on the slopes keeps two nearly collinear inputs from blowing up.

    No intercept by default: Up and Down are symmetric, and over a few hundred windows an
    intercept only learns which way BTC happened to trend - a bias on every future call."""
    beta = [0.0, 1.0, 0.0]
    for _ in range(50):
        g = [0.0, 0.0, 0.0]
        h = [[0.0] * 3 for _ in range(3)]
        for (x1, x2), y, w in zip(xs, ys, ws):
            v = (1.0, x1, x2)
            z = beta[0] + beta[1] * x1 + beta[2] * x2
            p = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
            for i in range(3):
                g[i] += w * (y - p) * v[i]
                for j in range(3):
                    h[i][j] += w * p * (1 - p) * v[i] * v[j]
        for i in (1, 2):
            g[i] -= ridge * (beta[i] - (1.0 if i == 1 else 0.0))
            h[i][i] += ridge
        if not intercept:
            g[0], h[0] = 0.0, [1.0, 0.0, 0.0]
            h[1][0] = h[2][0] = 0.0
        step = _solve3(h, g)
        beta = [b + d for b, d in zip(beta, step)]
        if max(abs(d) for d in step) < 1e-7:
            break
    return Calibration(a=beta[0], b_model=beta[1], b_market=beta[2])


def scores(ps: list[float], ys: list[int], ws: list[float]) -> tuple[float, float]:
    """(log loss, Brier), weighted. Lower is better for both."""
    tw = sum(ws) or 1.0
    ll = sum(w * -math.log(min(max(p if y else 1 - p, 1e-6), 1.0)) for p, y, w in zip(ps, ys, ws)) / tw
    br = sum(w * (p - y) ** 2 for p, y, w in zip(ps, ys, ws)) / tw
    return ll, br


def simulate(scans: list[Scan], won: dict[int, str], cal: Calibration, s: Settings) -> tuple[int, int, float]:
    """First qualifying $1 entry per window under the current entry rules (edge band,
    price band, |z|). Reversal filter not replayed: briefs before today lack the input.
    Returns (trades, wins, P&L)."""
    r = s.risk
    taken: dict[int, tuple[str, float]] = {}
    for sc in sorted(scans, key=lambda x: (x.start, -x.secs)):
        if sc.start in taken or sc.start not in won or abs(sc.z) < r.min_abs_z:
            continue
        p_up = calibrate(sc.p_model, sc.m_up, cal)
        for side, fair, price in (("Up", p_up, sc.fill_up), ("Down", 1 - p_up, sc.fill_down)):
            if price is None or not (r.min_entry_price <= price <= r.max_entry_price):
                continue
            edge = fair - price - 0.07 * price * (1 - price)
            need = r.high_price_min_edge if price > r.high_price else r.min_edge
            if need <= edge <= r.max_edge:
                taken[sc.start] = (side, price)
                break
    wins = sum(won[k] == side for k, (side, _) in taken.items())
    pnl = sum((1 / p if won[k] == side else 0.0) - 1 - 0.07 * (1 - p) for k, (side, p) in taken.items())
    return len(taken), wins, pnl


# ---------------------------------------------------------------- the command
def run(s: Settings, now: float, write: bool = True) -> list[str]:
    r = s.risk
    scans = load_scans(s.data_dir, r.entry_zone_s, r.min_seconds_left)
    won = outcomes(s, {sc.start for sc in scans}, now)
    scans = [sc for sc in scans if sc.start in won]
    windows = sorted({sc.start for sc in scans})
    out = [f"labelled scans {len(scans)} across {len(windows)} resolved windows (entry zone only)"]
    if len(windows) < MIN_WINDOWS:
        return out + [f"need at least {MIN_WINDOWS} resolved windows to fit; keep the bot scanning."]

    per_window: dict[int, int] = {}
    for sc in scans:
        per_window[sc.start] = per_window.get(sc.start, 0) + 1

    def xyw(rows):
        return ([(logit(sc.p_model), logit(sc.m_up)) for sc in rows],
                [int(won[sc.start] == "Up") for sc in rows],
                [1.0 / per_window[sc.start] for sc in rows])

    cut = windows[int(len(windows) * (1 - TEST_FRACTION))]
    train = [sc for sc in scans if sc.start < cut]
    test = [sc for sc in scans if sc.start >= cut]
    cal_train = fit(*xyw(train))
    _, yt, wt = xyw(test)
    raw = scores([sc.p_model for sc in test], yt, wt)
    mkt = scores([sc.m_up for sc in test], yt, wt)
    new = scores([calibrate(sc.p_model, sc.m_up, cal_train) for sc in test], yt, wt)
    n_test = len({sc.start for sc in test})
    out += [f"held-out test: the last {n_test} windows (fitted on the {len(windows) - n_test} before them)",
            f"  {'':22}{'log loss':>9}{'Brier':>8}   (lower is better)",
            f"  {'current model':22}{raw[0]:9.3f}{raw[1]:8.3f}",
            f"  {'market price':22}{mkt[0]:9.3f}{mkt[1]:8.3f}",
            f"  {'learned model':22}{new[0]:9.3f}{new[1]:8.3f}",
            f"  learned on train: {cal_train.a:+.3f} + {cal_train.b_model:.3f}*model + {cal_train.b_market:.3f}*market "
            "(logit scale)"]
    cur = s.calibration
    for label, cal in (("current", cur), ("learned", cal_train)):
        n, w, pnl = simulate(test, won, cal, s)
        out.append(f"  replay on test windows, {label} model: {n} trades, {w} won, P&L ${pnl:+.2f} at $1 each")

    out += ["", "where the raw model is wrong (all windows):", _reliability(scans, won, per_window)]

    if new[0] >= raw[0]:
        return out + ["", "learned model is not better on unseen windows - calibration unchanged."]
    final = fit(*xyw(scans))
    if write:
        (s.data_dir / CALIBRATION_FILE).write_text(json.dumps({
            **asdict(final), "fitted_at": now, "windows": len(windows), "scans": len(scans),
            "test": {"windows": n_test, "raw": raw, "market": mkt, "learned": new}}, indent=1), encoding="utf-8")
        out += ["", f"accepted: refit on all {len(windows)} windows -> {final.a:+.3f} + {final.b_model:.3f}*model + "
                f"{final.b_market:.3f}*market, saved to data/{CALIBRATION_FILE}.",
                "Restart the autopilot to use it. PM_CALIBRATION=off reverts to the raw model."]
    else:
        out += ["", "learned model is better; not saved (--dry)."]
    return out


def _reliability(scans: list[Scan], won: dict[int, str], per_window: dict[int, int]) -> str:
    """Model P(Up) bucket -> how often Up actually won, and what the market said."""
    edges = [0.0, 0.1, 0.25, 0.4, 0.6, 0.75, 0.9, 1.01]
    rows = [f"  {'model says Up':>14} {'windows':>8} {'Up won':>7} {'market said':>12}"]
    for lo, hi in zip(edges, edges[1:]):
        b = [sc for sc in scans if lo <= sc.p_model < hi]
        if not b:
            continue
        w = [1.0 / per_window[sc.start] for sc in b]
        tw = sum(w)
        act = sum(wi * (won[sc.start] == "Up") for sc, wi in zip(b, w)) / tw
        mk = sum(wi * sc.m_up for sc, wi in zip(b, w)) / tw
        rows.append(f"  {f'{lo:.2f}-{min(hi, 1):.2f}':>14} {len({sc.start for sc in b}):8d} {act:7.0%} {mk:12.0%}")
    return "\n".join(rows)
