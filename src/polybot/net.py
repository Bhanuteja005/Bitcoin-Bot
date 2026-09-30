"""One pooled HTTP client and a parallel fan-out helper.

A scan needs ~6 independent requests (market, two books, spot, klines, open
price). Sequentially that is ~6 round trips; fanned out it is ~1. Connections
are kept alive for the life of the process.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

import httpx

_client: httpx.Client | None = None
_pool = ThreadPoolExecutor(max_workers=8)


class FeedError(RuntimeError):
    """A data source failed. Callers refuse to trade on it rather than guess."""


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(5.0, connect=3.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            headers={"User-Agent": "polybot/0.1"},
        )
    return _client


def get_json(url: str, params: dict | None = None) -> Any:
    try:
        r = client().get(url, params=params)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise FeedError(f"GET {url} failed: {type(e).__name__}: {e}") from e


def parallel(**calls: Callable[[], Any]) -> dict[str, Any]:
    """Run named zero-arg callables concurrently. Exceptions are returned, not raised,
    so one dead feed is reported alongside the ones that worked."""
    futures = {name: _pool.submit(fn) for name, fn in calls.items()}
    out: dict[str, Any] = {}
    for name, fut in futures.items():
        try:
            out[name] = fut.result()
        except Exception as e:  # noqa: BLE001 - surfaced to the caller as data
            out[name] = e
    return out
