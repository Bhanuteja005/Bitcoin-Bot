"""5-minute window arithmetic. Pure: pass the time in, get the window out.

Polymarket's BTC Up/Down 5m markets are keyed by the unix second the window
opens, e.g. `btc-updown-5m-1790679600` covers 1790679600 .. 1790679900. The
window start is always a multiple of 300.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

WINDOW_SECONDS = 300
SLUG_PREFIX = "btc-updown-5m-"


@dataclass(frozen=True, slots=True)
class Window:
    start: int
    end: int

    @property
    def slug(self) -> str:
        return f"{SLUG_PREFIX}{self.start}"

    def seconds_left(self, now: float) -> float:
        return max(0.0, self.end - now)

    def elapsed(self, now: float) -> float:
        return min(WINDOW_SECONDS, max(0.0, now - self.start))

    def label(self) -> str:
        s = datetime.fromtimestamp(self.start, UTC)
        e = datetime.fromtimestamp(self.end, UTC)
        return f"{s:%H:%M}-{e:%H:%M} UTC"


def window_at(ts: float) -> Window:
    start = int(ts) // WINDOW_SECONDS * WINDOW_SECONDS
    return Window(start, start + WINDOW_SECONDS)


def next_window(ts: float) -> Window:
    return window_at(window_at(ts).end)


def parse_slug(slug: str) -> Window:
    if not slug.startswith(SLUG_PREFIX):
        raise ValueError(f"not a BTC 5m slug: {slug}")
    start = int(slug.removeprefix(SLUG_PREFIX))
    if start % WINDOW_SECONDS:
        raise ValueError(f"slug start {start} is not on a 5-minute boundary")
    return Window(start, start + WINDOW_SECONDS)


def in_blackout(ts: float, spec: str) -> str | None:
    """The 'HH:MM-HH:MM' range (New York time) that `ts` falls in, else None.

    Checked at the window level: an entry is blocked if any part of the window
    overlaps a blackout range, since the release lands mid-window either way.
    """
    if not spec.strip():
        return None
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    w = window_at(ts)
    start = datetime.fromtimestamp(w.start, ny)
    end = datetime.fromtimestamp(w.end - 1, ny)
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        lo = start.replace(hour=int(a[:2]), minute=int(a[3:5]), second=0)
        hi = start.replace(hour=int(b[:2]), minute=int(b[3:5]), second=0)
        if start < hi and end >= lo:
            return part.strip()
    return None
