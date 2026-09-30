"""Trade-by-trade report and calibration, from the journal plus market outcomes.

Pure: journal rows and a {slug: winning outcome} map in, a report dict out. The CLI
looks the outcomes up; this module only does arithmetic, so it can be tested.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Trade:
    ts: float
    slug: str
    outcome: str
    shares: float
    cost: float  # USD paid, fees included
    price: float  # avg fill price
    fair: float | None  # model's win chance for this side at entry
    edge: float | None
    exit_kind: str = "open"  # take-profit | stop-loss | manual | held
    proceeds: float = 0.0
    winner: str | None = None

    @property
    def pnl(self) -> float:
        return self.proceeds - self.cost

    @property
    def won_market(self) -> bool | None:
        return None if self.winner is None else self.winner == self.outcome

    @property
    def hold_value(self) -> float | None:
        """What holding to resolution would have paid."""
        w = self.won_market
        return None if w is None else (self.shares if w else 0.0)


def trades(rows: list[dict], mode: str = "paper") -> list[Trade]:
    open_by_key: dict[tuple[str, str], Trade] = {}
    out: list[Trade] = []
    for r in rows:
        if r.get("mode", "paper") != mode or r.get("status") not in {"filled", "settled"}:
            continue
        key = (r["slug"], r["outcome"])
        if r["kind"] == "entry":
            t = Trade(r["ts"], r["slug"], r["outcome"], r["shares"], r["usd"], r["price"],
                      r.get("fair"), r.get("edge"))
            open_by_key[key] = t
            out.append(t)
        elif r["kind"] in {"exit", "settle"} and key in open_by_key:
            t = open_by_key.pop(key)
            t.proceeds = r["usd"]
            t.exit_kind = r.get("reason", "manual") if r["kind"] == "exit" else "held"
            if r["kind"] == "settle":
                t.winner = t.outcome if r["price"] >= 0.5 else ("Down" if t.outcome == "Up" else "Up")
    return out


def summarise(ts: list[Trade], winners: dict[str, str]) -> dict:
    for t in ts:
        if t.winner is None and t.slug in winners:
            t.winner = winners[t.slug]
    closed = [t for t in ts if t.exit_kind != "open"]
    known = [t for t in closed if t.won_market is not None]
    with_fair = [t for t in known if t.fair is not None]
    stops = [t for t in closed if t.exit_kind == "stop-loss" and t.hold_value is not None]
    tps = [t for t in closed if t.exit_kind == "take-profit" and t.hold_value is not None]

    def brier(pairs):
        return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None

    buckets: dict[str, list[int]] = {}
    for t in with_fair:
        lo = min(int(t.fair * 10), 9) / 10
        b = buckets.setdefault(f"{lo:.1f}-{lo + 0.1:.1f}", [0, 0])
        b[0] += 1
        b[1] += int(t.won_market)
    return {
        "trades": len(closed),
        "open": len(ts) - len(closed),
        "wins": sum(t.pnl > 0 for t in closed),
        "realised": sum(t.pnl for t in closed),
        "staked": sum(t.cost for t in closed),
        # Expected P&L if the model's probabilities were exactly right and every trade were held.
        "expected": sum(t.fair * t.shares - t.cost for t in closed if t.fair is not None),
        "known": len(known),
        "brier_model": brier([(t.fair, int(t.won_market)) for t in with_fair]),
        "brier_market": brier([(t.price, int(t.won_market)) for t in with_fair]),
        "buckets": dict(sorted(buckets.items())),
        "stop_saved": sum(t.proceeds - t.hold_value for t in stops),
        "stop_n": len(stops),
        "tp_saved": sum(t.proceeds - t.hold_value for t in tps),
        "tp_n": len(tps),
    }
