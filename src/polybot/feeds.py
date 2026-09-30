"""BTC price data from Binance spot (BTCUSDT).

Polymarket settles on Chainlink BTC/USD, not Binance. Binance is used because
it is fast, public and tracks Chainlink closely; the residual gap is modelled
as `basis` in `model.prob_up`, not ignored.
"""

from __future__ import annotations

from dataclasses import dataclass

from .net import FeedError, get_json


@dataclass(frozen=True, slots=True)
class Candle:
    ts: int  # open time, unix seconds
    open: float
    high: float
    low: float
    close: float
    volume: float


def _candles(rows: list) -> list[Candle]:
    return [Candle(int(r[0]) // 1000, float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]


def spot(host: str) -> float:
    return float(get_json(f"{host}/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])


def klines(host: str, interval: str, limit: int, start: int | None = None) -> list[Candle]:
    params: dict = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
    if start is not None:
        params["startTime"] = start * 1000
    return _candles(get_json(f"{host}/api/v3/klines", params))


def open_at(host: str, ts: int) -> float:
    """BTC price at the exact second a window opened (open of that 1s candle)."""
    rows = klines(host, "1s", 1, start=ts)
    if not rows or rows[0].ts != ts:
        raise FeedError(f"no 1s candle at {ts}; window may not have opened yet")
    return rows[0].open


def server_time(host: str) -> float:
    return get_json(f"{host}/api/v3/time")["serverTime"] / 1000.0


def twap(host: str, start: int, seconds: int) -> tuple[float, int]:
    """Average of 1s closes over [start, start + seconds) -> (avg, samples).

    Mirrors a settlement TWAP. Refuses on an empty range rather than guessing.
    """
    rows = [c for c in klines(host, "1s", max(1, seconds), start=start) if c.ts < start + seconds]
    if not rows:
        raise FeedError(f"no 1s candles in [{start}, {start + seconds})")
    return sum(c.close for c in rows) / len(rows), len(rows)
