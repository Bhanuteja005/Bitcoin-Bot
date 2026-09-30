"""Pre-trade gates. Pure: all state is passed in, a list of refusals comes out.

An empty list means go. Any refusal is final for that order - the CLI does not
shrink the stake or widen the price to squeeze an order through.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Risk


@dataclass(frozen=True, slots=True)
class EntryState:
    kill: bool
    live: bool
    geoblock_ok: bool | None  # None = could not check
    cash_usd: float | None
    realised_today: float
    trades_today: int
    open_in_window: bool
    seconds_left: float
    min_order_usd: float = 1.0
    blackout: str | None = None


def check_entry(risk: Risk, s: EntryState, stake: float, max_price: float) -> list[str]:
    out: list[str] = []
    if s.kill:
        out.append("kill switch is ON (data/KILL exists)")
    if s.blackout:
        out.append(f"macro-release blackout {s.blackout} New York time: volatility can jump without warning")
    if s.live and s.geoblock_ok is not True:
        out.append("geoblock check did not pass: Polymarket does not allow orders from this location")
    if stake <= 0:
        out.append("stake must be positive")
    if stake > risk.max_stake_usd + 1e-9:
        out.append(f"stake ${stake:.2f} exceeds max stake ${risk.max_stake_usd:.2f}")
    if stake < s.min_order_usd - 1e-9:
        out.append(f"stake ${stake:.2f} is below Polymarket's ${s.min_order_usd:.2f} minimum for a market buy")
    if s.cash_usd is None:
        out.append("cash balance unknown")
    elif stake > s.cash_usd + 1e-9:
        out.append(f"stake ${stake:.2f} exceeds available cash ${s.cash_usd:.2f}")
    if -s.realised_today >= risk.daily_loss_limit_usd - 1e-9:
        out.append(f"daily loss limit hit (${-s.realised_today:.2f} lost today)")
    if s.trades_today >= risk.max_trades_per_day:
        out.append(f"max {risk.max_trades_per_day} trades per day reached")
    if s.open_in_window:
        out.append("already holding a position in this window: one position per window")
    if s.seconds_left < risk.min_seconds_left:
        out.append(f"{s.seconds_left:.0f}s left is below the {risk.min_seconds_left}s cutoff")
    if not (risk.min_entry_price <= max_price <= risk.max_entry_price):
        out.append(f"price cap {max_price:.2f} outside [{risk.min_entry_price:.2f}, {risk.max_entry_price:.2f}]")
    return out


def check_exit(kill_blocks_exits: bool, kill: bool, live: bool, geoblock_ok: bool | None) -> list[str]:
    # Exits are allowed under the kill switch by default: the switch exists to
    # stop new risk, and trapping someone in a losing position is not that.
    out: list[str] = []
    if kill and kill_blocks_exits:
        out.append("kill switch is ON")
    if live and geoblock_ok is not True:
        out.append("geoblock check did not pass")
    return out
