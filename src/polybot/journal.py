"""Append-only JSONL record of every entry, exit, settlement and rejection.

Paper positions and P&L are derived from this file, so the dry-run desk behaves
like the live one: it has a cash balance, it can be out of money, and a losing
streak trips the daily loss limit exactly as it would with real funds.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _path(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "journal.jsonl"


def append(data_dir: Path, kind: str, **fields) -> dict:
    row = {"ts": round(time.time(), 3), "kind": kind, **fields}
    with _path(data_dir).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")
    return row


def rows(data_dir: Path) -> list[dict]:
    p = _path(data_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass(slots=True)
class Position:
    mode: str
    slug: str
    outcome: str
    token: str
    shares: float = 0.0
    cost: float = 0.0  # USD paid, fees included

    @property
    def avg_price(self) -> float:
        return self.cost / self.shares if self.shares else 0.0


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


@dataclass(slots=True)
class Book:
    positions: dict[tuple[str, str, str], Position]
    realised_by_day: dict[str, float]
    trades_by_day: dict[str, int]
    paper_cash_delta: float  # realised P&L minus cost of open paper positions


def replay(all_rows: list[dict]) -> Book:
    positions: dict[tuple[str, str, str], Position] = {}
    realised: dict[str, float] = defaultdict(float)
    trades: dict[str, int] = defaultdict(int)
    cash = 0.0
    for r in all_rows:
        kind, mode = r["kind"], r.get("mode", "paper")
        if kind not in {"entry", "exit", "settle"} or r.get("status") not in {"filled", "settled"}:
            continue
        key = (mode, r["slug"], r["outcome"])
        pos = positions.setdefault(key, Position(mode, r["slug"], r["outcome"], r.get("token", "")))
        day = _day(r["ts"])
        if kind == "entry":
            pos.shares += r["shares"]
            pos.cost += r["usd"]
            trades[day] += 1
            if mode == "paper":
                cash -= r["usd"]
        else:  # exit or settle: shares leave at r['price'], proceeds net of fee in r['usd']
            sold = min(r["shares"], pos.shares)
            basis = pos.avg_price * sold
            pnl = r["usd"] - basis
            realised[day] += pnl
            pos.cost -= basis
            pos.shares -= sold
            if mode == "paper":
                cash += r["usd"]
    open_positions = {k: p for k, p in positions.items() if p.shares > 1e-9}
    return Book(open_positions, dict(realised), dict(trades), cash)


def today(ts: float | None = None) -> str:
    return _day(ts if ts is not None else time.time())
