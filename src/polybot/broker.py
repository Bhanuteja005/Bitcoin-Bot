"""The one door to the exchange.

Every order - paper or live - goes through `Broker.buy` / `Broker.sell`. The
mode gate, the duplicate guard and the journal all live here, so there is no
second path that can place an order without them.

Live orders are signed by Polymarket's official V2 SDK (`polymarket-client`). We
never hand-roll signing. The SDK is imported lazily so a `scan` does not pay for
its startup; `warm()` loads it in the background while a brief is fetched.
"""

from __future__ import annotations

import hashlib
import math
import json
import time
from dataclasses import dataclass

from . import journal
from .config import Settings
from .model import fill_price, fill_price_sell
from .net import _pool
from .polymarket import Market

DUPLICATE_WINDOW_S = 20


@dataclass(frozen=True, slots=True)
class Fill:
    ok: bool
    mode: str
    side: str
    outcome: str
    shares: float
    usd: float
    avg_price: float
    order_id: str
    detail: str


def items(paginator):
    """Flatten an SDK paginator: iterating one yields Pages, each holding `.items`."""
    for page in paginator:
        yield from getattr(page, "items", (page,))


def _on_tick(price: float, tick: float, up: bool) -> str:
    """Price as a string on the market's tick grid. The exchange rejects anything else,
    and float arithmetic (0.97 - 0.02 = 0.9499999...) produces it. Rounding down keeps a
    buy cap no higher, and a sell floor no stricter, than intended."""
    steps = price / tick
    n = math.ceil(steps - 1e-9) if up else math.floor(steps + 1e-9)
    decimals = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
    return f"{n * tick:.{decimals}f}"


def _key(slug: str, outcome: str, side: str, amount: float) -> str:
    return hashlib.sha256(f"{slug}|{outcome}|{side}|{amount:.4f}".encode()).hexdigest()[:16]


class Broker:
    def __init__(self, settings: Settings):
        self.s = settings
        self._clob = None
        self._warming = None

    @property
    def mode(self) -> str:
        return "live" if self.s.is_live else "paper"

    # ---- live client -------------------------------------------------------
    def clob(self):
        """Polymarket's official V2 SDK client (`polymarket-client`).

        `wallet` is always passed explicitly: if omitted, the SDK would deploy a
        fresh Deposit Wallet on-chain, which is not something a trade command
        should ever do as a side effect.
        """
        if self._clob is not None:
            return self._clob
        if self._warming is not None and not self._warming.done():
            return self._warming.result()
        if not self.s.has_wallet:
            raise RuntimeError("PM_PRIVATE_KEY and PM_FUNDER_ADDRESS must be set in .env for live trading")
        from polymarket import SecureClient
        from polymarket.auth import BuilderApiKey

        k = self.s.builder_key
        api_key = BuilderApiKey(key=k[0], secret=k[1], passphrase=k[2]) if k else None
        self._clob = SecureClient.create(private_key=self.s.private_key, wallet=self.s.funder, api_key=api_key)
        return self._clob

    def cash(self) -> float | None:
        if not self.s.is_live:
            return self.s.risk.bankroll_usd + journal.replay(journal.rows(self.s.data_dir)).paper_cash_delta
        return self.clob().get_balance_allowance(asset_type="COLLATERAL").balance / 1e6

    def live_shares(self, condition_id: str, token: str) -> float:
        for p in items(self.clob().list_positions(condition_id=condition_id)):
            if str(p.asset_id) == token:
                return float(p.current_size)
        return 0.0

    def redeem(self, condition_id: str) -> str:
        handle = self.clob().redeem_positions(condition_id=condition_id)
        return str(handle.wait())

    def claim_all(self) -> list[str]:
        """Redeem every resolved position the wallet holds, so winnings are cash again
        before the next trade. Gasless redeems need the builder key from .env."""
        if not self.s.is_live or not self.s.has_wallet:
            return []
        todo = {str(p.condition_id): p for p in items(self.clob().list_positions()) if p.redeemable}
        if todo and not self.s.builder_key:
            return [f"{len(todo)} position(s) to claim, but PM_BUILDER_API_KEY/_SECRET/_PASSPHRASE "
                    "are not in .env - claim in the Polymarket UI"]
        notes = []
        for cid, p in todo.items():
            try:
                notes.append(f"claimed {p.slug} {p.outcome} {float(p.current_size):.3f}sh: {self.redeem(cid)}")
            except Exception as e:  # noqa: BLE001 - one failed claim must not stop the rest
                notes.append(f"claim {p.slug} failed ({e!s:.120}) - claim it in the Polymarket UI")
        return notes

    def prefetch(self, m: Market) -> None:
        """Load the SDK's per-market order metadata (tick, neg-risk, fee rates) before the
        entry zone. Uncached, it costs ~1.6s at order time, inside the critical path.
        Uses the SDK's metadata cache directly; any failure just means no head start."""
        if not self.s.is_live:
            return
        try:
            c = self.clob()
            for token in (m.token_up, m.token_down):
                c._ctx.order_metadata.resolve_market(c._ctx, token_id=token)
        except Exception:  # noqa: BLE001 - optimisation only
            pass

    def warm(self) -> None:
        """Start loading the signing client in the background (live only)."""
        if self.s.is_live and self.s.has_wallet and self._clob is None:
            self._warming = _pool.submit(self.clob)

    # ---- guards ------------------------------------------------------------
    def _recent_duplicate(self, key: str) -> bool:
        cutoff = time.time() - DUPLICATE_WINDOW_S
        return any(r.get("key") == key and r["ts"] >= cutoff and r.get("status") in {"filled", "submitted"}
                   for r in journal.rows(self.s.data_dir)[-50:])

    def _record(self, kind: str, m: Market, outcome: str, key: str, fill: Fill, **extra) -> Fill:
        journal.append(self.s.data_dir, kind, mode=fill.mode, slug=m.slug, outcome=outcome,
                       token=m.token(outcome), shares=round(fill.shares, 6), usd=round(fill.usd, 6),
                       price=round(fill.avg_price, 6), order_id=fill.order_id,
                       status="filled" if fill.ok else "rejected", detail=fill.detail, key=key, **extra)
        return fill

    # ---- orders ------------------------------------------------------------
    def buy(self, m: Market, outcome: str, usd: float, max_price: float,
            asks: list[tuple[float, float]], fee_per_share: float, force: bool = False,
            meta: dict | None = None) -> Fill:
        key = _key(m.slug, outcome, "BUY", usd)
        if not force and self._recent_duplicate(key):
            return Fill(False, self.mode, "BUY", outcome, 0, 0, 0, "", "duplicate of an order placed <20s ago (use --force if intended)")
        if self.s.is_live:
            fill = self._live(m, outcome, "BUY", usd, max_price)
        else:
            fill = self._paper_buy(outcome, usd, max_price, asks, fee_per_share)
        return self._record("entry", m, outcome, key, fill, max_price=max_price, **(meta or {}))

    def sell(self, m: Market, outcome: str, shares: float, min_price: float,
             bids: list[tuple[float, float]], fee_per_share: float, reason: str = "manual") -> Fill:
        key = _key(m.slug, outcome, "SELL", shares)
        if self._recent_duplicate(key):
            return Fill(False, self.mode, "SELL", outcome, 0, 0, 0, "", "duplicate of a sell placed <20s ago")
        if self.s.is_live:
            # The taker fee on a buy is taken in shares, so the wallet can hold slightly
            # fewer than the order reported. A FOK sell for more than is held is refused
            # outright, so sell what the chain says we have.
            try:
                held = self.token_balance(m.token(outcome))
                if held < shares:
                    shares = math.floor(held * 100) / 100
            except Exception:  # noqa: BLE001 - fall back to the journal's count
                pass
            if shares <= 0:
                fill = Fill(False, "live", "SELL", outcome, 0, 0, 0, "", "no shares held on-chain")
            else:
                fill = self._live(m, outcome, "SELL", shares, min_price)
        else:
            fill = self._paper_sell(outcome, shares, min_price, bids, fee_per_share)
        return self._record("exit", m, outcome, key, fill, min_price=min_price, reason=reason)

    def token_balance(self, token: str) -> float:
        r = self.clob().get_balance_allowance(asset_type="CONDITIONAL", token_id=token)
        return r.balance / 1e6

    # FOK semantics in paper mode too: all or nothing within the price limit.
    def _paper_buy(self, outcome, usd, max_price, asks, fee) -> Fill:
        within = [(p, s) for p, s in asks if p <= max_price + 1e-9]
        fp = fill_price(within, usd)
        if fp is None:
            return Fill(False, "paper", "BUY", outcome, 0, 0, 0, "", f"FOK not fillable: <${usd:.2f} of asks at or below {max_price:.2f}")
        avg, _ = fp
        shares = usd / (avg + fee)  # fee taken in shares, so the same $ buys fewer
        return Fill(True, "paper", "BUY", outcome, shares, usd, avg, f"paper-{int(time.time()*1000)}", "simulated FOK fill")

    def _paper_sell(self, outcome, shares, min_price, bids, fee) -> Fill:
        within = [(p, s) for p, s in bids if p >= min_price - 1e-9]
        fp = fill_price_sell(within, shares)
        if fp is None:
            return Fill(False, "paper", "SELL", outcome, 0, 0, 0, "", f"FOK not fillable: <{shares:.2f} shares bid at or above {min_price:.2f}")
        avg, _ = fp
        return Fill(True, "paper", "SELL", outcome, shares, shares * (avg - fee), avg, f"paper-{int(time.time()*1000)}", "simulated FOK fill")

    def _live(self, m: Market, outcome: str, side: str, amount: float, limit: float) -> Fill:
        from polymarket import PolymarketError, TransactionFailedError
        from polymarket import TimeoutError as SettleTimeout

        c = self.clob()
        token = m.token(outcome)
        try:
            # FOK with a price bound: fills entirely at or better than `limit`, or not at all.
            if side == "BUY":
                resp = c.place_market_order(token_id=token, side="BUY", amount=f"{amount:.2f}",
                                            max_price=_on_tick(limit, m.tick, up=False), order_type="FOK")
            else:
                resp = c.place_market_order(token_id=token, side="SELL", shares=f"{amount:.4f}",
                                            min_price=_on_tick(limit, m.tick, up=False), order_type="FOK")
        except PolymarketError as e:  # rejections are data: recorded, not raised
            return Fill(False, "live", side, outcome, 0, 0, 0, "", f"{type(e).__name__}: {e}")
        if not resp.ok:
            return Fill(False, "live", side, outcome, 0, 0, 0, "", f"{resp.code}: {resp.message}")

        if resp.status == "delayed":
            resp = self._await_delayed(c, resp)
            if resp is None:
                return Fill(False, "live", side, outcome, 0, 0, 0, "", "delayed and not matched within 5s")
        making, taking = float(resp.making_amount), float(resp.taking_amount)
        shares, usd = (taking, making) if side == "BUY" else (making, taking)
        if shares <= 0:
            return Fill(False, "live", side, outcome, 0, 0, 0, resp.order_id, f"accepted as {resp.status} with no fill")

        # "matched" is not "settled": bot builders report matched FAK/FOK fills
        # that never arrived on-chain. Wait for settlement before calling it filled.
        detail = f"{resp.status}; settled"
        try:
            c.wait_for_order_fill_settlement(resp, timeout_s=15)
        except TransactionFailedError as e:
            return Fill(False, "live", side, outcome, 0, 0, 0, resp.order_id, f"matched but settlement FAILED: {e}")
        except SettleTimeout:
            detail = f"{resp.status}; settlement not confirmed within 15s - verify with `pm status`"
        if side == "BUY":
            # Last check before counting it: the shares must be in the wallet. Bot builders
            # report "matched" buys whose shares never arrived.
            held = 0.0
            for _ in range(10):
                try:
                    held = self.token_balance(token)
                except Exception:  # noqa: BLE001 - keep polling until the deadline
                    held = 0.0
                if held > 0:
                    break
                time.sleep(0.5)
            if held <= 0:
                return Fill(False, "live", side, outcome, 0, 0, 0, resp.order_id,
                            f"{resp.status} but NO shares arrived in the wallet within 5s - check the Polymarket UI")
            detail += f"; {held:.4f} shares confirmed in wallet"
            shares = min(shares, held)
        return Fill(True, "live", side, outcome, shares, usd, usd / shares, resp.order_id, detail)

    @staticmethod
    def _await_delayed(c, resp):
        """A delayed order is pending matching. Poll it briefly; never assume."""
        for _ in range(10):
            time.sleep(0.5)
            try:
                o = c.get_order(order_id=resp.order_id)
            except Exception:  # noqa: BLE001 - keep polling until the deadline
                continue
            matched = float(o.size_matched)
            if matched > 0:
                usd = matched * float(o.price)
                making, taking = (usd, matched) if o.side == "BUY" else (matched, usd)
                return resp.model_copy(update={"making_amount": making, "taking_amount": taking, "status": "matched"})
            if o.status.lower() not in {"live", "delayed"}:
                return None
        return None

    # ---- order management (live only) ---------------------------------------
    def open_orders(self) -> list[dict]:
        if not self.s.is_live:
            return []
        return [o.model_dump(mode="json") for o in items(self.clob().list_open_orders())]

    def cancel(self, order_id: str | None) -> dict:
        if not self.s.is_live:
            return {"note": "paper mode has no resting orders (all orders are FOK)"}
        c = self.clob()
        r = c.cancel_all() if order_id in (None, "all") else c.cancel_order(order_id=order_id)
        return r.model_dump(mode="json")
