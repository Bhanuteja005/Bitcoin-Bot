"""`pm` - the desk's command line. Claude Code drives this; so can you.

    pm scan                      brief for the live window (model fair value vs book)
    pm buy up|down|auto          FOK market buy, price-capped, through every risk gate
    pm sell up|down              FOK market sell of the position
    pm manage                    hold/sell advice for open positions
    pm watch [--auto-sell]       re-check every second until the window closes
    pm auto [--advise]           autopilot: enter on edge, exit on the sell rule, every window
    pm status | pnl | settle | orders | cancel | kill on|off | doctor
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import signal
import sys
import threading
import time

from . import brief as briefmod
from . import chainlink, feeds, journal, polymarket
from .broker import Broker, items
from .config import Settings, load
from .model import (blended_sigma, calibrate, evaluate_exit, fair_up_range, favourite_decision, kelly_stake,
                    pct_stop_level)
from .net import FeedError, get_json, parallel
from .risk import EntryState, check_entry, check_exit
from .window import in_blackout, next_window, parse_slug, window_at

GEOBLOCK_TTL_S = 600
# Set by Ctrl-C / SIGINT (the dashboard's Stop): no new entries, an open position is still
# managed to its close, then the autopilot exits. A second Ctrl-C stops at once.
STOP = threading.Event()


DUST_SHARES = 0.01

def _p(*a) -> None:
    print(*a, flush=True)


# ---------------------------------------------------------------- helpers
def geoblock_ok(s: Settings) -> tuple[bool | None, str]:
    cache = s.data_dir / "geoblock.json"
    try:
        c = json.loads(cache.read_text())
        if time.time() - c["at"] < GEOBLOCK_TTL_S:
            return c["ok"], c["detail"]
    except (OSError, ValueError, KeyError):
        pass
    try:
        g = polymarket.geoblock(s.geoblock_url)
    except FeedError as e:
        return None, f"unreachable ({e})"
    ok = g.get("blocked") is False
    detail = f"blocked={g.get('blocked')} country={g.get('country')} region={g.get('region')}"
    s.data_dir.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"at": time.time(), "ok": ok, "detail": detail}))
    return ok, detail


def settle_positions(s: Settings) -> list[str]:
    """Book the result of positions whose window has ended.

    Paper settles from the resolved Polymarket market, else from Binance (flagged
    approximate). Live settles only from Polymarket's own resolution: it keeps the
    daily loss limit honest, while the on-chain claim is still done in the UI.
    """
    notes = []
    book = journal.replay(journal.rows(s.data_dir))
    now = time.time()
    for (mode, slug, outcome), pos in book.positions.items():
        w = parse_slug(slug)
        if now < w.end + 3:
            continue
        source, winner = None, None
        try:
            m = polymarket.market_by_slug(s.gamma_host, slug)
            if m.closed and m.outcome_prices and max(m.outcome_prices) >= 0.99:
                winner, source = ("Up" if m.outcome_prices[0] > m.outcome_prices[1] else "Down"), "polymarket"
        except FeedError:
            pass
        if winner is None and mode == "live":
            notes.append(f"{slug}: live result not published by Polymarket yet")
            continue
        if winner is None:
            # Next best: the Chainlink values the market settles on, if still buffered.
            cl = chainlink.feed(s)
            k = cl.strike(w.start)
            closes = [cl.twap60.at(w.end) if L == 60 else cl.realised_mean(w.end - L, w.end)
                      for L in s.risk.settle_windows]
            if k is not None and all(c is not None for c in closes):
                verdicts = {"Up" if c >= k else "Down" for c in closes}
                if len(verdicts) == 1:
                    winner, source = verdicts.pop(), "chainlink"
        if winner is None:
            try:
                SL = s.risk.strike_twap_s
                o = feeds.twap(s.binance_host, w.start - SL, SL)[0] if SL else feeds.open_at(s.binance_host, w.start)
                closes = [feeds.twap(s.binance_host, w.end - L, L)[0] if L else feeds.open_at(s.binance_host, w.end)
                          for L in s.risk.settle_windows]
                verdicts = {"Up" if c >= o else "Down" for c in closes}
                if len(verdicts) != 1:
                    notes.append(f"{slug}: settlement readings disagree; waiting for Polymarket's result")
                    continue
                winner, source = verdicts.pop(), "binance-approx"
            except FeedError as e:
                notes.append(f"{slug}: cannot settle yet ({e})")
                continue
        payout = 1.0 if outcome == winner else 0.0
        journal.append(s.data_dir, "settle", mode=mode, slug=slug, outcome=outcome, token=pos.token,
                       shares=pos.shares, usd=pos.shares * payout, price=payout, status="settled",
                       detail=f"winner {winner} via {source}")
        notes.append(f"settled {slug} {outcome}: {'WON' if payout else 'LOST'} "
                     f"${pos.shares * payout - pos.cost:+.2f} (winner {winner}, {source})")
        if mode == "live" and payout and s.is_live:
            # Winning shares only turn back into cash once redeemed on-chain; without
            # this the next trade finds no cash and the risk gate refuses it.
            try:
                notes.append(f"  redeemed winnings: {Broker(s).redeem(m.condition_id)}")
            except Exception as e:  # noqa: BLE001 - never lose the settlement over a failed claim
                notes.append(f"  redeem failed ({e!s:.120}); claim it in the Polymarket UI")
    return notes


# ---------------------------------------------------------------- commands
def cmd_scan(s: Settings, a) -> int:
    chainlink.feed(s, wait=8)
    w = next_window(time.time()) if a.next else None
    if a.next:
        _p(f"next window {w.slug} opens in {w.start - time.time():.0f}s; open price is unknown until then.")
        m = polymarket.market_by_slug(s.gamma_host, w.slug)
        _p(f"  {m.question} | accepting {m.accepting_orders} | up {m.token_up[:10]}… down {m.token_down[:10]}…")
        return 0
    b = briefmod.build(s, a.usd)
    if a.json:
        _p(json.dumps({"id": b.id, "action": b.decision.action, "max_price": b.decision.max_price,
                       "fair_up": b.decision.fair_up, "seconds_left": b.seconds_left}))
    else:
        _p(briefmod.render(b))
    return 0


def entry_refusals(s: Settings, broker: Broker, b, usd: float, cap: float,
                   cash: float | None = None) -> list[str]:
    """Run every pre-trade gate against current state; journal any refusal.
    `cash`: a balance already read this round (it only changes when we trade)."""
    book = journal.replay(journal.rows(s.data_dir))
    day = journal.today()
    geo_ok, _ = geoblock_ok(s) if s.is_live else (None, "paper")
    state = EntryState(
        kill=s.kill_file.exists(), live=s.is_live, geoblock_ok=geo_ok,
        cash_usd=cash if cash is not None else broker.cash(),
        realised_today=book.realised_by_day.get(day, 0.0), trades_today=book.trades_by_day.get(day, 0),
        open_in_window=any(k[0] == broker.mode and k[1] == b.window.slug for k in book.positions),
        seconds_left=b.seconds_left, blackout=in_blackout(b.window.start, s.risk.blackout_et))
    refusals = check_entry(s.risk, state, usd, cap)
    if refusals:
        journal.append(s.data_dir, "reject", mode=broker.mode, slug=b.window.slug, usd=usd,
                       status="rejected", detail="; ".join(refusals))
    return refusals


def cmd_buy(s: Settings, a) -> int:
    for n in settle_positions(s):
        _p(n)
    broker = Broker(s)
    broker.warm()  # signing client loads while the brief fetches
    chainlink.feed(s)  # no wait: a buy is time-critical; Binance covers until Chainlink connects
    t0 = time.time()
    b = briefmod.build(s, a.usd)
    d = b.decision
    side = a.side.capitalize()
    if side == "Auto":
        if d.action == "NO_TRADE":
            _p(briefmod.render(b))
            _p("NO TRADE: the model finds no edge on either side. Nothing sent.")
            return 2
        side = "Up" if d.action == "BUY_UP" else "Down"
    q = d.up if side == "Up" else d.down
    model_side = {"BUY_UP": "Up", "BUY_DOWN": "Down"}.get(d.action)

    if a.max_price is not None:
        cap = a.max_price
    elif model_side == side:
        cap = d.max_price
    elif a.override:
        cap = round((q.worst_fill or q.best_ask or 1.0) + s.risk.slippage_cents, 4)
    else:
        _p(briefmod.render(b))
        _p(f"REFUSED: model does not support buying {side} ({d.action}). "
           "Pass --override to trade against the model (risk gates still apply).")
        return 2

    refusals = entry_refusals(s, broker, b, a.usd, cap)
    if refusals:
        _p(briefmod.render(b))
        _p("REFUSED by risk gate:")
        for r in refusals:
            _p(f"  - {r}")
        return 3
    if b.age() > briefmod.BRIEF_MAX_AGE_S:
        _p("REFUSED: brief went stale before the order could be sent.")
        return 3

    books = b.books[side]
    fill = broker.buy(b.market, side, a.usd, cap, books[1], q.fee_per_share, force=a.force,
                      meta={"fair": round(q.fair, 4), "edge": round(q.edge or 0, 4),
                            "secs_left": round(b.seconds_left, 1), "source": b.source})
    ms = (time.time() - t0) * 1000
    if fill.ok:
        _p(f"[{fill.mode.upper()}] BOUGHT {fill.shares:.3f} {side} @ {fill.avg_price:.3f} for ${fill.usd:.2f} "
           f"(cap {cap:.2f}, fair {q.fair:.3f}) in {ms:.0f}ms  order {fill.order_id}")
        _p(f"  window {b.window.slug} closes in {b.window.seconds_left(time.time()):.0f}s. "
           f"Pays ${fill.shares:.2f} if {side} wins. Run `pm manage` / `pm watch` for exits.")
        if fill.shares < b.market.min_size:
            _p(f"  note: {fill.shares:.2f} shares is below the {b.market.min_size:g}-share minimum; an early "
               "sell may be rejected by the exchange - plan to hold to resolution.")
        return 0
    _p(f"[{fill.mode.upper()}] NOT FILLED ({ms:.0f}ms): {fill.detail}")
    return 4


def _position_for(s: Settings, slug: str, outcome: str):
    mode = "live" if s.is_live else "paper"
    return journal.replay(journal.rows(s.data_dir)).positions.get((mode, slug, outcome))


def cmd_sell(s: Settings, a) -> int:
    broker = Broker(s)
    slug = a.slug or window_at(time.time()).slug
    side = a.side.capitalize()
    m = polymarket.market_by_slug(s.gamma_host, slug)
    pos = _position_for(s, slug, side)
    held = broker.live_shares(m.condition_id, m.token(side)) if s.is_live else (pos.shares if pos else 0.0)
    shares = held if a.shares in (None, "all") else float(a.shares)
    if not held or shares <= 0 or shares > held + 1e-6:
        _p(f"nothing to sell: holding {held or 0:.4f} {side} in {slug}")
        return 2
    geo_ok, _ = geoblock_ok(s) if s.is_live else (None, "")
    refusals = check_exit(False, s.kill_file.exists(), s.is_live, geo_ok)
    if refusals:
        _p("REFUSED: " + "; ".join(refusals))
        return 3
    bids, _ = polymarket.book(s.clob_host, m.token(side))
    if not bids:
        _p("no bids on the book: cannot sell, hold to resolution")
        return 4
    floor = a.min_price if a.min_price is not None else max(m.tick, round(bids[0][0] - s.risk.slippage_cents, 4))
    fee = m.fees.per_share(bids[0][0])
    fill = broker.sell(m, side, shares, floor, bids, fee)
    if fill.ok:
        cost = (pos.avg_price * shares) if pos else 0.0
        _p(f"[{fill.mode.upper()}] SOLD {fill.shares:.3f} {side} @ {fill.avg_price:.3f} → ${fill.usd:.2f} "
           f"(P&L ${fill.usd - cost:+.2f})  order {fill.order_id}")
        return 0
    _p(f"[{fill.mode.upper()}] NOT FILLED: {fill.detail}")
    return 4


def _manage_once(s: Settings, auto_sell: bool, stop_bid: float = 0.0, stop_pct: float = 0.0) -> bool:
    """Returns True while there is still an open position in a live window."""
    mode = "live" if s.is_live else "paper"
    now = time.time()
    # A remainder below DUST_SHARES (left by a sell that rounds to the tick) cannot be
    # sold - the exchange rejects it - so it is not a position to manage.
    open_pos = [p for (md, slug, _), p in journal.replay(journal.rows(s.data_dir)).positions.items()
                if md == mode and parse_slug(slug).end > now and p.shares >= DUST_SHARES]
    if not open_pos:
        _p("no open position in a live window.")
        return False
    for pos in open_pos:
        w = parse_slug(pos.slug)
        m = polymarket.market_by_slug(s.gamma_host, pos.slug)
        calls, sources = briefmod.price_inputs(s, w, time.time())
        r = parallel(c=lambda: feeds.klines(s.binance_host, "1m", 61),
                     book=lambda: polymarket.book(s.clob_host, m.token(pos.outcome)),
                     **calls)
        if any(isinstance(v, Exception) for v in r.values()):
            _p(f"feed error, holding: {[str(v) for v in r.values() if isinstance(v, Exception)]}")
            continue
        sigma = blended_sigma([c.close for c in r["c"]])
        left = w.seconds_left(time.time())
        lo, hi = fair_up_range(r["spot"], r["open_price"], left, sigma, briefmod.settle_tuple(s, r),
                               briefmod.basis_for(s, sources))
        # Entries demand edge under every settlement reading; exits use the midpoint.
        # Valuing a held position at its worst reading made the bot sell below what
        # the position was likely worth and pay a second taker fee for it.
        if not s.calibration.identity:
            held = r["book"]
            book_mid = (held[0][0][0] + held[1][0][0]) / 2 if held[0] and held[1] else None
            m_up = None if book_mid is None else (book_mid if pos.outcome == "Up" else 1 - book_mid)
            lo, hi = calibrate(lo, m_up, s.calibration), calibrate(hi, m_up, s.calibration)
        mid = (lo + hi) / 2
        fair = mid if pos.outcome == "Up" else 1 - mid
        adv = evaluate_exit(fair, r["book"][0], pos.shares, pos.avg_price, m.fees,
                            stop_fair=s.risk.stop_fair, stop_min_bid=s.risk.stop_min_bid)
        pnl = f"${adv.unrealised_usd:+.2f}" if adv.unrealised_usd is not None else "n/a"
        _p(f"{time.strftime('%H:%M:%S')} {pos.outcome} {pos.shares:.3f}sh @ {pos.avg_price:.3f} | BTC "
           f"{r['spot'] - r['open_price']:+.1f} vs strike | {left:.0f}s left | fair {fair:.3f} bid {adv.bid or 0:.3f} "
           f"| {adv.action} ({pnl}) - {adv.reasons[0]}")
        # Bid stop: sell once the held side's bid is <= stop_bid, even in hold mode. The book
        # overprices comebacks in these markets (see `pm learn`), so a cheap exit beats riding
        # to $0. Backtest on 65 windows of the favourite strategy: +$4.07 vs +$2.80 holding.
        # Percent stop: sell once the bid has lost `stop_pct` of the entry price.
        level = max(stop_bid, pct_stop_level(pos.avg_price, stop_pct) or 0.0)
        bid_stop = level > 0 and adv.bid is not None and adv.bid <= level
        if (bid_stop or (adv.action == "SELL" and auto_sell)) and left > 3:
            reason = "stop-loss" if bid_stop or adv.reasons[0].startswith("STOP-LOSS") else "take-profit"
            if bid_stop:
                _p(f"  bid stop: {pos.outcome} bid {adv.bid:.2f} <= {level:.2f} (entry {pos.avg_price:.2f}), selling")
            fill = Broker(s).sell(m, pos.outcome, pos.shares, max(m.tick, adv.bid - s.risk.slippage_cents),
                                  r["book"][0], m.fees.per_share(adv.bid), reason=reason)
            _p(f"  auto-sell: {'SOLD @ %.3f → $%.2f' % (fill.avg_price, fill.usd) if fill.ok else 'failed: ' + fill.detail}")
    return True


def cmd_manage(s: Settings, a) -> int:
    _manage_once(s, a.auto_sell)
    return 0


def cmd_watch(s: Settings, a) -> int:
    try:
        while _manage_once(s, a.auto_sell):
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass
    for n in settle_positions(s):
        _p(n)
    return 0


def cmd_status(s: Settings, a) -> int:
    for n in settle_positions(s):
        _p(n)
    broker = Broker(s)
    book = journal.replay(journal.rows(s.data_dir))
    day = journal.today()
    _p(f"mode        {'LIVE' if s.is_live else 'PAPER (dry_run)'}"
       + ("" if s.is_live or s.mode == "dry_run" else "  [PM_MODE=live but PM_LIVE_CONFIRMED is not true]"))
    _p(f"wallet      {'configured, funder ' + s.funder[:6] + '…' + s.funder[-4:] if s.has_wallet else 'not configured (.env)'}")
    _p(f"kill        {'ON' if s.kill_file.exists() else 'off'}")
    if s.is_live:
        ok, detail = geoblock_ok(s)
        _p(f"geoblock    {'OK' if ok else 'BLOCKED/UNKNOWN'} ({detail})")
    try:
        _p(f"cash        ${broker.cash():.2f}")
    except Exception as e:  # noqa: BLE001
        _p(f"cash        unavailable: {e}")
    _p(f"today       realised ${book.realised_by_day.get(day, 0):+.2f}  trades {book.trades_by_day.get(day, 0)}"
       f"  (limit -${s.risk.daily_loss_limit_usd:.2f}, max {s.risk.max_trades_per_day})")
    _p(f"limits      stake ≤ ${s.risk.max_stake_usd:.2f}  min edge {s.risk.min_edge:.2f}  "
       f"entry {s.risk.min_entry_price:.2f}-{s.risk.max_entry_price:.2f}  cutoff {s.risk.min_seconds_left}s")
    for (mode, slug, outcome), p in book.positions.items():
        _p(f"position    [{mode}] {slug} {outcome} {p.shares:.3f}sh cost ${p.cost:.2f} avg {p.avg_price:.3f}")
    w = window_at(time.time())
    _p(f"window      {w.slug} ({w.label()}) {w.seconds_left(time.time()):.0f}s left")
    return 0


def cmd_pnl(s: Settings, a) -> int:
    rows = journal.rows(s.data_dir)
    book = journal.replay(rows)
    for r in rows[-a.n:]:
        if r["kind"] in {"entry", "exit", "settle", "reject"}:
            _p(f"{time.strftime('%m-%d %H:%M:%S', time.localtime(r['ts']))} {r.get('mode', ''):5} {r['kind']:6} "
               f"{r.get('slug', '')[-10:]} {r.get('outcome', ''):4} {r.get('shares', 0):7.3f}sh "
               f"${r.get('usd', 0):6.2f} @ {r.get('price', 0):.3f} {r.get('status', '')} {r.get('detail', '')[:60]}")
    total = sum(book.realised_by_day.values())
    _p(f"realised total ${total:+.2f} | by day: " + ", ".join(f"{d} {v:+.2f}" for d, v in sorted(book.realised_by_day.items())))
    return 0


def cmd_settle(s: Settings, a) -> int:
    notes = settle_positions(s)
    _p("\n".join(notes) or "nothing to settle.")
    for n in Broker(s).claim_all():
        _p(n)
    return 0


def cmd_learn(s: Settings, a) -> int:
    from . import learn

    if not s.calibration.identity:
        c = s.calibration
        _p(f"active calibration: {c.a:+.3f} + {c.b_model:.3f}*model + {c.b_market:.3f}*market")
    _p("\n".join(learn.run(s, time.time(), write=not a.dry)))
    return 0


def cmd_report(s: Settings, a) -> int:
    """Every trade, then win rate, expected vs realised P&L, exit value and calibration."""
    from . import report

    mode = "live" if s.is_live else "paper"
    ts = [t for t in report.trades(journal.rows(s.data_dir), mode) if t.ts >= a.since]
    winners: dict[str, str] = {}
    for slug in {t.slug for t in ts if t.winner is None and t.exit_kind != "open"}:
        try:
            m = polymarket.market_by_slug(s.gamma_host, slug)
            if m.closed and m.outcome_prices and max(m.outcome_prices) >= 0.99:
                winners[slug] = "Up" if m.outcome_prices[0] > m.outcome_prices[1] else "Down"
        except FeedError:
            pass
    r = report.summarise(ts, winners)
    _p(f"{'time (local)':12} {'round':10} {'side':4} {'stake':>7} {'price':>6} {'model':>6} "
       f"{'exit':12} {'paid':>7} {'P&L':>8}  result")
    for t in ts:
        res = {True: "side won", False: "side lost", None: "-"}[t.won_market]
        fair = f"{t.fair:.2f}" if t.fair is not None else "-"
        _p(f"{time.strftime('%m-%d %H:%M', time.localtime(t.ts)):12} {t.slug[-10:]:10} {t.outcome:4} "
           f"${t.cost:6.2f} {t.price:6.3f} {fair:>6} {t.exit_kind:12} ${t.proceeds:6.2f} {t.pnl:+8.2f}  {res}")
    n = r["trades"]
    if not n:
        _p("no closed trades yet.")
        return 0
    _p("")
    _p(f"trades {n} (open {r['open']}) | wins {r['wins']} ({r['wins'] / n:.0%}) | staked ${r['staked']:.2f}")
    _p(f"realised P&L ${r['realised']:+.2f} ({r['realised'] / r['staked']:+.1%} of staked) | "
       + (f"expected from model edge ${r['expected']:+.2f} (the gap is luck, or a miscalibrated model)"
          if any(t.fair is not None for t in ts) else "expected: n/a (trades predate model logging)"))
    if r["tp_n"]:
        _p(f"take-profit exits: {r['tp_n']}, vs holding to the result: ${r['tp_saved']:+.2f}")
    if r["stop_n"]:
        _p(f"stop-loss exits:   {r['stop_n']}, vs holding to the result: ${r['stop_saved']:+.2f}")
    if r["brier_model"] is not None:
        _p(f"calibration: Brier model {r['brier_model']:.3f} vs market price {r['brier_market']:.3f} "
           "(lower is better; the model must beat the market to have a real edge)")
        _p("  model chance -> times that side won: "
           + ", ".join(f"{k}: {w}/{c}" for k, (c, w) in r["buckets"].items()))
    if n < 500:
        _p(f"sample: {n} trades - far below the ~500 needed before these numbers are reliable.")
    return 0


def cmd_rules(s: Settings, a) -> int:
    """Print the live market's resolution rules, to check the TWAP/strike assumption."""
    m = polymarket.market_by_slug(s.gamma_host, a.slug or window_at(time.time()).slug)
    _p(m.question)
    _p(m.rules or "(no rules text on the market)")
    return 0


def cmd_orders(s: Settings, a) -> int:
    _p(json.dumps(Broker(s).open_orders(), indent=1))
    return 0


def cmd_cancel(s: Settings, a) -> int:
    _p(json.dumps(Broker(s).cancel(a.order_id), indent=1))
    return 0


def cmd_kill(s: Settings, a) -> int:
    s.data_dir.mkdir(parents=True, exist_ok=True)
    if a.state == "on":
        s.kill_file.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    elif s.kill_file.exists():
        s.kill_file.unlink()
    _p(f"kill switch {'ON' if s.kill_file.exists() else 'off'}")
    return 0


def cmd_doctor(s: Settings, a) -> int:
    checks = {
        "binance": lambda: feeds.spot(s.binance_host),
        "gamma": lambda: len(get_json(f"{s.gamma_host}/markets", {"slug": window_at(time.time()).slug})),
        "clob": lambda: get_json(f"{s.clob_host}/time"),
        "geoblock": lambda: polymarket.geoblock(s.geoblock_url),
    }
    for name, fn in checks.items():
        t = time.time()
        try:
            out = fn()
            _p(f"  ok   {name:9} {(time.time() - t) * 1000:5.0f}ms  {out!s:.100}")
        except Exception as e:  # noqa: BLE001
            _p(f"  FAIL {name:9} {e!s:.140}")
    _p(f"  wallet   {'configured' if s.has_wallet else 'missing PM_PRIVATE_KEY / PM_FUNDER_ADDRESS'}; "
       f"mode {s.mode}; live_confirmed {s.live_confirmed}")
    if s.has_wallet and a.auth:
        try:
            live = dataclasses.replace(s, mode="live", live_confirmed=True)  # read-only: balance only
            _p(f"  auth     ok, cash ${Broker(live).cash():.2f}")
        except Exception as e:  # noqa: BLE001
            _p(f"  auth     FAIL {e!s:.200}")
    return 0


# ---------------------------------------------------------------- autopilot
PERMANENT = ("kill switch", "daily loss", "available cash", "max ", "already holding", "geoblock", "cash balance")
# With --forever only these end the autopilot: out of money, kill switch, or a blocked location.
FINAL = ("kill switch", "available cash", "geoblock check did not pass")


def _brief_line(b) -> str:
    d = b.decision
    ua, da = d.up.best_ask, d.down.best_ask
    return (f"{time.strftime('%H:%M:%S')} {b.window.slug[-10:]} {b.seconds_left:4.0f}s | BTC {b.btc - b.open_price:+7.1f} "
            f"z {d.z:+.2f} | fair Up {d.up.fair:.3f}-{1 - d.down.fair:.3f} | ask Up {ua or 0:.2f} Down {da or 0:.2f} | "
            f"edge Up {d.up.edge if d.up.edge is not None else float('nan'):+.3f} "
            f"Down {d.down.edge if d.down.edge is not None else float('nan'):+.3f} | {d.action}")


def _auto_round(s: Settings, broker: Broker, w, a) -> str:
    """Trade one window: wait for the entry zone, enter on edge, manage to the close.
    Returns 'traded', 'advised', 'skipped' or 'stop:<reason>'."""
    last_log = 0.0
    market, cash = None, None
    while True:
        if STOP.is_set():
            return "stop:stopped by user"
        left = w.seconds_left(time.time())
        if left <= s.risk.min_seconds_left:
            _p(f"{w.slug}: no entry this round (no edge inside the zone).")
            return "skipped"
        if market is None:
            # Everything that does not change within a round is loaded once, before the
            # entry zone, so the decision-to-order path is only books + sign + send.
            try:
                market = polymarket.market_by_slug(s.gamma_host, w.slug)
                broker.prefetch(market)
                cash = broker.cash() or 0.0
            except Exception as e:  # noqa: BLE001 - retried next loop
                _p(f"{time.strftime('%H:%M:%S')} round setup failed, retrying: {e!s:.120}")
                market = None
                time.sleep(2)
                continue
        if left > s.risk.entry_zone_s:
            time.sleep(min(left - s.risk.entry_zone_s, 5.0))
            continue
        bo = in_blackout(w.start, s.risk.blackout_et)
        if bo:
            _p(f"{w.slug}: skipped - macro-release blackout {bo} New York time.")
            return "skipped"
        # Liquidity is checked for the largest stake sizing could pick; the actual
        # (smaller or equal) stake then fills at the same or a better average price.
        probe = a.usd if a.fixed else max(s.risk.min_order_usd, min(a.usd, s.risk.max_bankroll_frac * cash))
        try:
            b = briefmod.build(s, probe, w, market=market)
        except FeedError as e:
            _p(f"{time.strftime('%H:%M:%S')} feed error, not trading on it: {e!s:.120}")
            time.sleep(2)
            continue
        d = b.decision
        if a.strategy == "favourite":
            d = favourite_decision(d, b.seconds_left, s.risk, a.enter_at, b.market.tick)
        if d.action == "NO_TRADE":
            if time.time() - last_log >= a.log_every:
                _p(_brief_line(b))
                last_log = time.time()
            time.sleep(a.interval)
            continue

        _p(briefmod.render(b))
        if a.strategy == "favourite":
            _p(f"STRATEGY favourite: {d.reasons[0]}")
        side = "Up" if d.action == "BUY_UP" else "Down"
        if a.advise:
            _p(f"CALL: BUY {side.upper()} up to {d.max_price:.2f} (advice only - nothing sent)")
            return "advised"
        q = d.up if side == "Up" else d.down
        stake = a.usd if a.fixed else kelly_stake(q.fair, q.avg_fill + q.fee_per_share, cash, s.risk)
        if stake <= 0:
            _p(f"sizing: quarter-Kelly stake is below the ${s.risk.min_order_usd:.0f} minimum at this bankroll; skipping")
            time.sleep(a.interval)
            continue
        refusals = entry_refusals(s, broker, b, stake, d.max_price, cash=cash)
        if refusals:
            _p("risk gate: " + "; ".join(refusals))
            hard = next((r for r in refusals if any(k in r for k in PERMANENT)), None)
            if hard and "already holding" in hard:
                # A restart mid-window lands here holding a position: manage it, don't abandon it.
                _p("already holding this window's position: managing it to the close")
                _manage_until_close(s, w, a)
                return "traded"
            if hard:
                return "stop:" + hard
            time.sleep(a.interval)
            continue
        asks = b.books[side][1]
        if not s.is_live:
            # Paper fills against a book fetched now, not the one the decision was made on:
            # a live order also meets the book as it is when it arrives, and other bots
            # take stale cheap asks first. Filling on the brief's book overstated results.
            try:
                asks = polymarket.book(s.clob_host, b.market.token(side))[1]
            except FeedError as e:
                _p(f"feed error re-reading the book, not filling on a stale one: {e!s:.100}")
                time.sleep(a.interval)
                continue
        fill = broker.buy(b.market, side, stake, d.max_price, asks, q.fee_per_share,
                          meta={"fair": round(q.fair, 4), "edge": round(q.edge, 4), "secs_left": round(b.seconds_left, 1),
                                "source": b.source})
        if not fill.ok and fill.order_id:
            # The exchange took the order but the fill could not be confirmed (settlement
            # failed or shares not seen yet). Retrying could buy twice: sit this window out.
            _p(f"[{fill.mode.upper()}] order {fill.order_id} accepted but not confirmed: {fill.detail} - "
               "not retrying this window; check `pm status` / the Polymarket UI")
            return "skipped"
        if not fill.ok:
            ms = (time.time() - b.fetched_at) * 1000
            _p(f"[{fill.mode.upper()}] not filled ({ms:.0f}ms after the book was read): {fill.detail} - retrying on next scan")
            time.sleep(a.interval)
            continue
        _p(f"[{fill.mode.upper()}] BOUGHT {fill.shares:.3f} {side} @ {fill.avg_price:.3f} for ${fill.usd:.2f} "
           f"(fair {q.fair:.3f}); managing until the close")
        _manage_until_close(s, w, a)
        return "traded"


def _manage_until_close(s: Settings, w, a) -> None:
    """Check the held position every second until it is sold or the window closes. Any
    error - a dead feed, an SDK or network exception, a refused sell - is logged and
    retried next second: dropping out here would leave the position without its stop."""
    while w.seconds_left(time.time()) > 1:
        try:
            if not _manage_once(s, auto_sell=not a.hold, stop_bid=a.stop_bid, stop_pct=a.stop_pct):
                return  # sold, or nothing left to manage
        except Exception as e:  # noqa: BLE001 - see docstring
            _p(f"{time.strftime('%H:%M:%S')} error while managing, holding and retrying: {type(e).__name__}: {e!s:.120}")
        time.sleep(1.0)


def _relearn(s: Settings) -> Settings:
    """Refit the calibration between windows; switch to it only if `learn` accepted it."""
    from . import learn
    from .config import load_calibration

    try:
        notes = learn.run(s, time.time(), write=True)
    except Exception as e:  # noqa: BLE001 - learning must never stop trading
        _p(f"learn failed, keeping the current model: {e!s:.120}")
        return s
    _p("learn: " + " | ".join(n.strip() for n in notes if n.startswith(("held-out", "  learned model", "  market", "  current model", "accepted", "learned model is not", "need at least"))))
    cal = load_calibration(s.data_dir)
    return s if cal == s.calibration else dataclasses.replace(s, calibration=cal)


def _nap(seconds: float) -> None:
    """Sleep, waking early once a stop is requested."""
    end = time.time() + seconds
    while not STOP.is_set() and time.time() < end:
        time.sleep(min(1.0, end - time.time()))


def _on_stop(_signum, _frame) -> None:
    if STOP.is_set():
        raise KeyboardInterrupt
    STOP.set()
    _p("stop requested: no new trades; an open position is managed until its window closes")


def _prune_briefs(s: Settings) -> None:
    """Saved scans pile up (~15k files a day); keep the last `keep_briefs_days`."""
    if s.risk.keep_briefs_days <= 0:
        return
    cutoff = time.time() - s.risk.keep_briefs_days * 86400
    d = s.data_dir / "briefs"
    try:
        for f in d.glob("*.json"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError as e:
        _p(f"brief cleanup failed: {e!s:.100}")


def cmd_auto(s: Settings, a) -> int:
    STOP.clear()
    old = {}
    if threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):
                old[name] = signal.signal(getattr(signal, name), _on_stop)
    try:
        return _auto_loop(s, a)
    finally:
        for name, h in old.items():
            signal.signal(getattr(signal, name), h)


def _auto_loop(s: Settings, a) -> int:
    broker = Broker(s)
    broker.warm()
    cl = chainlink.feed(s, wait=10)
    _p(f"price feed: {'Chainlink live (the settlement source)' if cl.fresh() else 'Binance fallback - Chainlink ' + (cl.error or 'not connected')}")
    label = "ADVISE-ONLY" if a.advise else ("LIVE" if s.is_live else "PAPER")
    sizing = f"stake ${a.usd:.2f} fixed" if a.fixed else (
        f"quarter-Kelly stake ${s.risk.min_order_usd:.0f}-${a.usd:.2f} (<= {s.risk.max_bankroll_frac:.0%} of cash)")
    _p(f"autopilot [{label}] {sizing}, entries in the last {s.risk.entry_zone_s}s of each window, "
       f"min edge {s.risk.min_edge:.2f}. Ctrl-C to stop.")
    done = 0
    try:
        while a.rounds == 0 or done < a.rounds:
            try:
                for n in settle_positions(s):
                    _p(n)
            except Exception as e:  # noqa: BLE001 - settlement is retried every window
                _p(f"settlement check failed, retrying next window: {e!s:.120}")
            try:
                for n in broker.claim_all():
                    _p(n)
            except Exception as e:  # noqa: BLE001 - a failed claim never stops trading
                _p(f"claim check failed: {e!s:.100}")
            w = window_at(time.time())
            if w.seconds_left(time.time()) <= s.risk.min_seconds_left:
                w = next_window(time.time())
            try:
                result = _auto_round(s, broker, w, a)
            except Exception as e:  # noqa: BLE001 - one bad round must not end an unattended run
                _p(f"{time.strftime('%H:%M:%S')} round {w.slug} failed: {type(e).__name__}: {e!s:.160} - "
                   "continuing with the next window")
                result = "error"
            done += 1
            if STOP.is_set():
                _p("autopilot stopped by user.")
                break
            if done % 12 == 0:
                _prune_briefs(s)
            if a.learn_every and done % a.learn_every == 0 and not result.startswith("stop:"):
                s = _relearn(s)
            if result.startswith("stop:"):
                reason = result[5:]
                if a.forever and not any(k in reason for k in FINAL):
                    # A daily limit resets at 00:00 UTC; wait it out instead of exiting.
                    _p(f"autopilot paused: {reason} - checking again next window")
                    _nap(max(0.0, w.end - time.time()) + 1.0)
                    continue
                _p(f"autopilot stopping: {reason}")
                break
            _nap(max(0.0, w.end - time.time()) + 1.0)
    except KeyboardInterrupt:
        _p("autopilot stopped by user.")
    time.sleep(3)
    for n in settle_positions(s):
        _p(n)
    book = journal.replay(journal.rows(s.data_dir))
    _p(f"today realised ${book.realised_by_day.get(journal.today(), 0.0):+.2f}")
    return 0


def cmd_dashboard(s: Settings, a) -> int:
    from . import dashboard

    return dashboard.serve(s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("scan", help="brief for the current window")
    p.add_argument("--usd", type=float, default=None)
    p.add_argument("--next", action="store_true", help="show the next window's market instead")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("buy", help="FOK market buy")
    p.add_argument("side", choices=["up", "down", "auto"])
    p.add_argument("--usd", type=float, default=None)
    p.add_argument("--max-price", type=float, default=None)
    p.add_argument("--override", action="store_true", help="allow a buy the model does not support")
    p.add_argument("--force", action="store_true", help="skip the 20s duplicate guard")

    p = sub.add_parser("sell", help="FOK market sell")
    p.add_argument("side", choices=["up", "down"])
    p.add_argument("--shares", default="all")
    p.add_argument("--min-price", type=float, default=None)
    p.add_argument("--slug", default=None)

    for name in ("manage", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--auto-sell", action="store_true")
        p.add_argument("--interval", type=float, default=1.0)

    p = sub.add_parser("auto", help="autopilot: trade every window until stopped")
    p.add_argument("--usd", type=float, default=None)
    p.add_argument("--rounds", type=int, default=0, help="0 = run until stopped")
    p.add_argument("--advise", action="store_true", help="print calls only, never send orders")
    p.add_argument("--fixed", action="store_true", help="always stake --usd instead of quarter-Kelly sizing")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between scans in the entry zone")
    p.add_argument("--learn-every", type=int, default=0, help="refit the calibration every N windows (0 = never)")
    p.add_argument("--strategy", choices=["model", "favourite"], default="model",
                   help="model = edge vs fair value; favourite = buy the favoured side late (paper test)")
    p.add_argument("--enter-at", type=float, default=120.0, help="favourite: enter once this many seconds are left")
    p.add_argument("--hold", action="store_true", help="no take-profit or model stop-loss; hold to resolution")
    p.add_argument("--stop-bid", type=float, default=0.0,
                   help="sell once the held side's bid falls to this (works with --hold; 0 = off)")
    p.add_argument("--stop-pct", type=float, default=None,
                   help="sell once the bid is this share below the entry price (0.40 = -40%%); "
                        "default PM_STOP_LOSS_PCT, 0 = off")
    p.add_argument("--forever", action="store_true",
                   help="daily loss/trade limits pause until the next UTC day instead of stopping; "
                        "stops only when cash runs out, the kill switch is on, or geoblock fails")
    p.add_argument("--log-every", type=float, default=10.0)

    sub.add_parser("dashboard", help="web page: start/stop Bot B, trades, charts, log (PORT, PM_DASHBOARD_PASSWORD)")
    sub.add_parser("status")
    p = sub.add_parser("pnl")
    p.add_argument("-n", type=int, default=20)
    sub.add_parser("settle")
    p = sub.add_parser("report", help="every trade plus win rate, expected vs realised P&L, calibration")
    p.add_argument("--since", type=float, default=0.0, help="unix time; only trades after it")
    p = sub.add_parser("learn", help="fit the model's calibration on every saved scan and its real outcome")
    p.add_argument("--dry", action="store_true", help="report only; never save a calibration")
    p = sub.add_parser("rules", help="print the market's resolution rules")
    p.add_argument("--slug", default=None)
    sub.add_parser("orders")
    p = sub.add_parser("cancel")
    p.add_argument("order_id", nargs="?", default="all")
    p = sub.add_parser("kill")
    p.add_argument("state", choices=["on", "off"])
    p = sub.add_parser("doctor")
    p.add_argument("--auth", action="store_true", help="also derive API creds and read the live balance")

    # Windows consoles default to cp1252, which cannot print the brief's symbols.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    a = ap.parse_args(argv)
    s = load()
    if getattr(a, "usd", "x") is None:
        a.usd = s.risk.max_stake_usd
    if getattr(a, "stop_pct", "x") is None:
        a.stop_pct = s.risk.stop_loss_pct
    handler = globals()[f"cmd_{a.cmd}"]
    try:
        return handler(s, a)
    except FeedError as e:
        _p(f"FEED ERROR - refusing to act on missing data: {e}")
        return 5


if __name__ == "__main__":
    sys.exit(main())
