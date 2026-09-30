"""Live Chainlink BTC/USD price and 60s TWAP - the feed Polymarket settles on.

Polymarket's RTDS stream (via the official SDK) publishes the Chainlink price and
the Chainlink 60s TWAP about once a second, each subscription opening with ~2
minutes of history. A background thread keeps a rolling buffer so the model can
read the real strike (the TWAP stream at the window open) and the real settlement
average instead of approximating both from Binance.

Everything here degrades to "not available": callers fall back to Binance and
label the source, rather than trading on a stale Chainlink value.
"""

from __future__ import annotations

import asyncio
import bisect
import threading
import time
from collections import deque

from .config import Settings

STALE_S = 5.0  # a Chainlink value older than this is not "now"
SILENT_S = 30.0  # a subscription quiet this long has stalled: reconnect it
BUFFER_S = 1200  # keep 20 minutes: covers the current window and the previous ones


class _Series:
    def __init__(self) -> None:
        self.ts: deque[float] = deque()
        self.px: deque[float] = deque()
        self.lock = threading.Lock()

    def add(self, ts: float, px: float) -> None:
        with self.lock:
            if self.ts and ts <= self.ts[-1]:
                if ts == self.ts[-1]:
                    self.px[-1] = px
                return  # out of order / duplicate history point
            self.ts.append(ts)
            self.px.append(px)
            cutoff = ts - BUFFER_S
            while self.ts and self.ts[0] < cutoff:
                self.ts.popleft()
                self.px.popleft()

    def last(self) -> tuple[float, float] | None:
        with self.lock:
            return (self.ts[-1], self.px[-1]) if self.ts else None

    def at(self, t: float, tolerance: float = 2.0) -> float | None:
        """Value of the last point at or before `t`, if one exists within `tolerance`."""
        with self.lock:
            ts = list(self.ts)
            i = bisect.bisect_right(ts, t) - 1
            if i < 0 or t - ts[i] > tolerance:
                return None
            return self.px[i]

    def mean(self, start: float, end: float, min_coverage: float = 0.8) -> float | None:
        """Mean of points in [start, end); None unless they cover most of the span."""
        with self.lock:
            vals = [p for t, p in zip(self.ts, self.px) if start <= t < end]
        span = max(end - start, 1.0)
        if not vals or len(vals) < min_coverage * span:
            return None
        return sum(vals) / len(vals)


class ChainlinkFeed:
    def __init__(self, settings: Settings):
        self.s = settings
        self.price = _Series()
        self.twap60 = _Series()
        self.error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- lifecycle -----------------------------------------------------------
    def start(self) -> ChainlinkFeed:
        if self._thread is None and self.s.has_wallet:
            self._thread = threading.Thread(target=self._run, name="chainlink", daemon=True)
            self._thread.start()
        return self

    def wait_ready(self, timeout: float = 4.0) -> bool:
        deadline = time.time() + timeout
        # Ready means both streams have delivered: the price and the TWAP (strike source).
        while time.time() < deadline:
            if self.fresh() and self.twap60.last() is not None:
                return True
            time.sleep(0.1)
        return self.fresh()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                asyncio.run(self._stream())
                backoff = 1.0
            except Exception as e:  # noqa: BLE001 - reconnect on anything; callers fall back meanwhile
                self.error = f"{type(e).__name__}: {e}"[:200]
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, 30.0)

    async def _stream(self) -> None:
        from polymarket import AsyncSecureClient
        from polymarket.streams import CryptoPriceSpec, CryptoTwapPriceSpec

        client = await AsyncSecureClient.create(private_key=self.s.private_key, wallet=self.s.funder)
        try:
            async def pump(spec, series: _Series) -> None:
                async with await client.subscribe(spec) as h:
                    it = h.__aiter__()
                    while True:
                        # A stream can go quiet without an error; waiting on it forever
                        # would leave the bot on the Binance fallback until a restart.
                        try:
                            ev = await asyncio.wait_for(it.__anext__(), SILENT_S)
                        except StopAsyncIteration:
                            return
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"no {spec.__class__.__name__} update for {SILENT_S:.0f}s") from None
                        if self._stop.is_set():
                            return
                        p = ev.payload
                        points = getattr(p, "data", None) or [p]
                        for pt in points:
                            ts = getattr(pt, "timestamp", None)
                            val = getattr(pt, "value", None)
                            if ts is None or val is None:
                                continue
                            series.add(ts.timestamp() if hasattr(ts, "timestamp") else float(ts), float(val))
                        self.error = None

            await asyncio.gather(pump(CryptoPriceSpec(symbols=["btcusd"]), self.price),
                                 pump(CryptoTwapPriceSpec(symbols=["btcusd"]), self.twap60))
        finally:
            await client.close()

    # ---- reads ---------------------------------------------------------------
    def fresh(self) -> bool:
        last = self.price.last()
        return last is not None and time.time() - last[0] <= STALE_S

    def price_now(self) -> float | None:
        last = self.price.last()
        return last[1] if last and time.time() - last[0] <= STALE_S else None

    def strike(self, window_start: int) -> float | None:
        """The TWAP stream's value at the window open: the market's 'price to beat'."""
        return self.twap60.at(window_start)

    def realised_mean(self, start: float, end: float) -> float | None:
        return self.price.mean(start, end)


_feed: ChainlinkFeed | None = None


def feed(settings: Settings, wait: float = 0.0) -> ChainlinkFeed:
    """Process-wide feed, started on first use. `wait` blocks briefly for the first snapshot."""
    global _feed
    if _feed is None:
        _feed = ChainlinkFeed(settings).start()
    if wait:
        _feed.wait_ready(wait)
    return _feed
