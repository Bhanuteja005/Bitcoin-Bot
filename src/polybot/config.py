"""The only module that reads the environment.

Everything else receives a `Settings` object. Keeping env access in one place is
what makes the risk limits auditable: there is exactly one spot where a limit
can come from, and one spot to read to know what the bot will do.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> dict[str, str]:
    # Tiny parser on purpose: python-dotenv is one more import on every
    # invocation, and startup time is the latency floor for a CLI trade.
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] in "\"'" and val[0] in val[1:]:
            val = val[1:val.index(val[0], 1)]
        else:
            # Inline comment: `KEY=1.0   # note`. A '#' only starts a comment after whitespace.
            val = re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key.strip()] = val
    return out


@dataclass(frozen=True, slots=True)
class Risk:
    """Hard limits. The risk layer rejects breaches; it never resizes into range."""

    bankroll_usd: float = 1.0
    max_stake_usd: float = 1.0
    daily_loss_limit_usd: float = 1.0
    max_trades_per_day: int = 20
    min_edge: float = 0.04  # model prob minus (ask + fee), per share
    max_entry_price: float = 0.90  # above this, payoff is too thin for basis risk
    # Entries above `high_price` win often but pay little; published bot logs show
    # 90%+ hit rates netting ~nothing there. Demand a bigger edge to take them.
    # An "edge" this large means the model disagrees with a liquid market by more than
    # it plausibly can: bad or mixed data, not an opportunity. Refuse it.
    max_edge: float = 0.15
    # |z| below this is noise (the prompt's rule): a small lead with minutes left is a coin flip
    # the book prices better than the model. The 30-Sep-2026 live loss entered at z +0.26.
    min_abs_z: float = 0.5
    high_price: float = 0.80
    high_price_min_edge: float = 0.06
    # With the whole bankroll on every $1 trade, a cheap long shot is a likely wipe-out
    # even when its expected value is positive, and tail probabilities are where the
    # Binance/Chainlink basis makes the model least reliable. Only buy near-even or favoured sides.
    min_entry_price: float = 0.40
    min_seconds_left: int = 15  # below this, a fill may land after the close
    slippage_cents: float = 0.02  # worst-price cap above the quoted ask
    basis_usd: float = 8.0  # Binance-vs-Chainlink uncertainty when pricing off Binance
    basis_chainlink_usd: float = 3.0  # residual uncertainty when pricing off Chainlink itself
    stop_fair: float = 0.25  # sell once the held side's chance of winning falls to this
    stop_min_bid: float = 0.03  # below this bid there is nothing left to save: hold
    # Autopilot stop as a share of the entry price: 0.40 sells once the held side's bid is
    # 40% below what was paid (0.80 -> 0.48). 0 = off. `pm auto --stop-pct` overrides it.
    stop_loss_pct: float = 0.0
    kelly_fraction: float = 0.25  # stake = this fraction of the Kelly bet (quarter-Kelly)
    max_bankroll_frac: float = 0.25  # never more than this share of current cash on one trade
    min_order_usd: float = 1.0  # Polymarket's minimum market buy
    # No entries around scheduled US macro releases (times in New York time): CPI/NFP/
    # jobless claims at 08:30, FOMC at 14:00 with the press conference at 14:30. Vol
    # jumps within a minute and trailing realised vol cannot see it coming.
    blackout_et: str = "08:25-08:45,13:55-14:45"
    entry_zone_s: int = 150  # autopilot only looks for entries with <= this many seconds left
    # Settlement reading(s) of the close: average of the last N seconds (0 = snapshot).
    # Polymarket's changelog (Aug 14, 2026) and the rules' source (the 60s TWAP stream)
    # put the close at the 60s TWAP. Set PM_SETTLE_WINDOWS=60,300 to also demand edge
    # under the literal "whole range" reading of the rules text.
    settle_windows: tuple[int, ...] = (60,)
    strike_twap_s: int = 60  # strike = the 60s TWAP stream's value at the window open; 0 = snapshot


@dataclass(frozen=True, slots=True)
class Calibration:
    """Learned correction on top of the random-walk model, fitted by `pm learn`:

        P(Up) = sigmoid(a + b_model * logit(model) + b_market * logit(market mid))

    The default (0, 1, 0) is the identity: the raw model. A fit that leans on the market
    (b_market > 0) is the data saying the book knew more than the model did."""

    a: float = 0.0
    b_model: float = 1.0
    b_market: float = 0.0

    @property
    def identity(self) -> bool:
        return self.a == 0.0 and self.b_model == 1.0 and self.b_market == 0.0


@dataclass(frozen=True, slots=True)
class Settings:
    mode: str = "dry_run"  # dry_run | live
    live_confirmed: bool = False
    private_key: str = field(default="", repr=False)
    funder: str = ""
    # Builder API key (key, secret, passphrase): lets the SDK relay gasless redeems, so
    # winnings are claimed automatically. Set PM_BUILDER_API_KEY/_SECRET/_PASSPHRASE in .env.
    builder_key: tuple[str, str, str] | None = field(default=None, repr=False)
    chain_id: int = 137
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_host: str = "https://data-api.polymarket.com"
    geoblock_url: str = "https://polymarket.com/api/geoblock"
    binance_host: str = "https://api.binance.com"
    data_dir: Path = ROOT / "data"
    # `pm dashboard`: web page with Start/Stop. Without a password it only listens on
    # 127.0.0.1; with one it listens on every interface (Railway) behind HTTP basic auth.
    dashboard_password: str = field(default="", repr=False)
    port: int = 8080
    risk: Risk = field(default_factory=Risk)
    calibration: Calibration = field(default_factory=Calibration)

    @property
    def is_live(self) -> bool:
        return self.mode == "live" and self.live_confirmed

    @property
    def kill_file(self) -> Path:
        return self.data_dir / "KILL"

    @property
    def has_wallet(self) -> bool:
        return bool(self.private_key and self.funder)


def load(env_file: Path | None = None) -> Settings:
    env = {**_load_dotenv(env_file or ROOT / ".env"), **os.environ}

    def get(name: str, default: str = "") -> str:
        return env.get(f"PM_{name}", default).strip()

    def num(name: str, default: float) -> float:
        raw = get(name)
        return float(raw) if raw else default

    base, d = Risk(), Settings()
    risk = Risk(
        bankroll_usd=num("BANKROLL_USD", base.bankroll_usd),
        max_stake_usd=num("MAX_STAKE_USD", base.max_stake_usd),
        daily_loss_limit_usd=num("DAILY_LOSS_LIMIT_USD", base.daily_loss_limit_usd),
        max_trades_per_day=int(num("MAX_TRADES_PER_DAY", base.max_trades_per_day)),
        min_edge=num("MIN_EDGE", base.min_edge),
        max_entry_price=num("MAX_ENTRY_PRICE", base.max_entry_price),
        min_entry_price=num("MIN_ENTRY_PRICE", base.min_entry_price),
        max_edge=num("MAX_EDGE", base.max_edge),
        min_abs_z=num("MIN_Z", base.min_abs_z),
        high_price=num("HIGH_PRICE", base.high_price),
        high_price_min_edge=num("HIGH_PRICE_MIN_EDGE", base.high_price_min_edge),
        min_seconds_left=int(num("MIN_SECONDS_LEFT", base.min_seconds_left)),
        slippage_cents=num("SLIPPAGE", base.slippage_cents),
        stop_fair=num("STOP_FAIR", base.stop_fair),
        stop_min_bid=num("STOP_MIN_BID", base.stop_min_bid),
        stop_loss_pct=num("STOP_LOSS_PCT", base.stop_loss_pct),
        kelly_fraction=num("KELLY_FRACTION", base.kelly_fraction),
        max_bankroll_frac=num("MAX_BANKROLL_FRAC", base.max_bankroll_frac),
        entry_zone_s=int(num("ENTRY_ZONE_S", base.entry_zone_s)),
        blackout_et=get("BLACKOUT_ET", base.blackout_et),
        settle_windows=tuple(int(x) for x in get("SETTLE_WINDOWS").split(",") if x.strip()) or base.settle_windows,
        strike_twap_s=int(num("STRIKE_TWAP_S", base.strike_twap_s)),
    )
    data_dir = Path(get("DATA_DIR") or ROOT / "data")
    mode = get("MODE", "dry_run").lower()
    if mode not in {"dry_run", "live"}:
        raise ValueError(f"PM_MODE must be dry_run or live, got {mode!r}")
    return Settings(
        mode=mode,
        live_confirmed=get("LIVE_CONFIRMED").lower() == "true",
        private_key=get("PRIVATE_KEY"),
        funder=get("FUNDER_ADDRESS"),
        builder_key=(lambda k: k if all(k) else None)(
            (get("BUILDER_API_KEY"), get("BUILDER_SECRET"), get("BUILDER_PASSPHRASE"))),
        clob_host=get("CLOB_HOST", d.clob_host),
        gamma_host=get("GAMMA_HOST", d.gamma_host),
        data_host=get("DATA_HOST", d.data_host),
        binance_host=get("BINANCE_HOST", d.binance_host),
        data_dir=data_dir,
        dashboard_password=get("DASHBOARD_PASSWORD"),
        port=int(env.get("PORT", "").strip() or 8080),  # Railway sets PORT
        risk=risk,
        calibration=Calibration() if get("CALIBRATION").lower() == "off" else load_calibration(data_dir),
    )



CALIBRATION_FILE = "calibration.json"


def load_calibration(data_dir: Path) -> Calibration:
    """The correction `pm learn` last accepted, or the identity if there is none.
    PM_CALIBRATION=off ignores it."""
    path = data_dir / CALIBRATION_FILE
    if not path.exists():
        return Calibration()
    d = json.loads(path.read_text(encoding="utf-8"))
    return Calibration(a=d["a"], b_model=d["b_model"], b_market=d["b_market"])
