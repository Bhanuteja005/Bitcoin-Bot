"""Public Polymarket reads: Gamma (market metadata), CLOB (books), Data API, geoblock.

Nothing here signs or spends. Order placement lives only in `broker`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .model import FeeSchedule
from .net import FeedError, get_json


@dataclass(frozen=True, slots=True)
class Market:
    slug: str
    question: str
    condition_id: str
    token_up: str
    token_down: str
    tick: float
    min_size: float
    accepting_orders: bool
    closed: bool
    neg_risk: bool
    fees: FeeSchedule
    outcome_prices: tuple[float, float] | None  # (up, down) once resolved or live mid
    rules: str = ""

    def token(self, outcome: str) -> str:
        return self.token_up if outcome.lower() == "up" else self.token_down


def _loads(v, default):
    if v is None:
        return default
    return json.loads(v) if isinstance(v, str) else v


def parse_market(m: dict) -> Market:
    outcomes = [o.lower() for o in _loads(m.get("outcomes"), [])]
    tokens = _loads(m.get("clobTokenIds"), [])
    if len(tokens) != 2 or outcomes != ["up", "down"]:
        raise FeedError(f"unexpected market shape for {m.get('slug')}: outcomes={outcomes}")
    fs = m.get("feeSchedule") or {}
    fees = FeeSchedule(
        rate=float(fs.get("rate") or 0.0),
        exponent=float(fs.get("exponent") or 1.0),
        enabled=bool(m.get("feesEnabled")),
    )
    prices = _loads(m.get("outcomePrices"), None)
    return Market(
        slug=m["slug"],
        question=m.get("question") or m["slug"],
        condition_id=m.get("conditionId") or "",
        token_up=tokens[0],
        token_down=tokens[1],
        tick=float(m.get("orderPriceMinTickSize") or 0.01),
        min_size=float(m.get("orderMinSize") or 5),
        accepting_orders=bool(m.get("acceptingOrders")),
        closed=bool(m.get("closed")),
        neg_risk=bool(m.get("negRisk")),
        fees=fees,
        outcome_prices=(float(prices[0]), float(prices[1])) if prices else None,
        rules=m.get("description") or "",
    )


def market_by_slug(gamma: str, slug: str) -> Market:
    rows = get_json(f"{gamma}/markets", {"slug": slug})
    if not rows:
        # Gamma leaves closed markets out of the default listing; resolved rounds
        # have to be asked for explicitly or settlement never sees the official result.
        rows = get_json(f"{gamma}/markets", {"slug": slug, "closed": "true"})
    if not rows:
        raise FeedError(f"market {slug} not found (not listed yet, or wrong slug)")
    return parse_market(rows[0])


Levels = list[tuple[float, float]]


def book(clob: str, token_id: str) -> tuple[Levels, Levels]:
    """(bids best-first, asks best-first). Sorted here rather than trusting wire order."""
    b = get_json(f"{clob}/book", {"token_id": token_id})
    bids = sorted(((float(x["price"]), float(x["size"])) for x in b.get("bids", [])), reverse=True)
    asks = sorted((float(x["price"]), float(x["size"])) for x in b.get("asks", []))
    return bids, asks


def geoblock(url: str) -> dict:
    """{'blocked': bool, 'country': ..., ...}. Live trading refuses unless blocked is False."""
    return get_json(url)
