"""`pm dashboard`: a web page to start/stop Bot B and read every trade it made.

The page is the parent process; the bot is a child `pm auto` it starts and stops, so
the exchange route stays `broker.py` behind the same risk gates. Stop sends Ctrl-C
(SIGINT), which the autopilot already handles: it stops entering, settles what has
resolved and exits. A position still open at that moment is left to resolve.

State that must survive a Railway redeploy lives in the data dir: `bot.state` says
whether the bot should be running (the page restarts it on boot), `bot.log` is its
output. Trades are read from the journal, the one record of what was bought and sold.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import journal
from .config import Settings

PAGE = Path(__file__).with_name("web") / "dashboard.html"
BUCKETS = ((0.0, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.01))
DUST = 0.01


def bot_args() -> list[str]:
    """Bot B: favourite 0.60-0.95 (PM_MIN/MAX_ENTRY_PRICE) with 120s left, fixed stake
    PM_MAX_STAKE_USD, stop PM_STOP_LOSS_PCT below entry, otherwise hold to the result."""
    return ["auto", "--strategy", "favourite", "--enter-at", "120", "--hold", "--fixed", "--forever"]


# ---------------------------------------------------------------- trades (pure)
def trades(rows: list[dict], mode: str) -> list[dict]:
    """One record per (window, side) position: cost in, money out, P&L once closed."""
    by: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.get("mode") != mode or r.get("status") not in ("filled", "settled"):
            continue
        k = (r.get("slug", ""), r.get("outcome", ""))
        t = by.setdefault(k, {"slug": k[0], "side": k[1], "ts": r.get("ts", 0), "cost": 0.0, "shares": 0.0,
                              "sold_shares": 0.0, "proceeds": 0.0, "payout": None, "exit": "", "exit_price": None,
                              "closed_ts": None, "fair": None, "secs_left": None})
        kind = r.get("kind")
        if kind == "entry":
            t["cost"] += float(r.get("usd") or 0)
            t["shares"] += float(r.get("shares") or 0)
            t["fair"] = r.get("fair", t["fair"])
            t["secs_left"] = r.get("secs_left", t["secs_left"])
        elif kind == "exit":
            t["proceeds"] += float(r.get("usd") or 0)
            t["sold_shares"] += float(r.get("shares") or 0)
            t["exit"] = r.get("reason") or "sold"
            t["exit_price"] = r.get("price")
            t["closed_ts"] = r.get("ts")
        elif kind == "settle":
            t["payout"] = (t["payout"] or 0.0) + float(r.get("usd") or 0)
            t["settle_price"] = r.get("price")
            t["closed_ts"] = r.get("ts")
    out = []
    for t in by.values():
        if t["shares"] <= 0:
            continue
        t["entry_price"] = t["cost"] / t["shares"]
        tail = t["slug"].rsplit("-", 1)[-1]
        t["window_end"] = int(tail) + 300 if tail.isdigit() else None
        sold_out = t["sold_shares"] >= t["shares"] - DUST
        t["closed"] = t["payout"] is not None or sold_out
        if t["closed"]:
            t["pnl"] = t["proceeds"] + (t["payout"] or 0.0) - t["cost"]
            if t["exit"] == "stop-loss":
                t["result"] = "stopped"
            elif t["exit"]:
                t["result"] = "sold"
            else:
                t["result"] = "won" if (t["payout"] or 0) > 0 else "lost"
        else:
            t["pnl"], t["result"] = None, "open"
        out.append(t)
    out.sort(key=lambda t: t["ts"])
    cum = 0.0
    for t in out:
        if t["closed"]:
            cum += t["pnl"]
        t["cum_pnl"] = cum
    return out


def stats(ts: list[dict], now: float) -> dict:
    closed = [t for t in ts if t["closed"]]
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = dd = cum = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    today = journal.today(now)
    staked = sum(t["cost"] for t in closed)
    buckets = []
    for lo, hi in BUCKETS:
        b = [t for t in closed if lo <= t["entry_price"] < hi]
        buckets.append({"label": f"{max(lo, 0.60):.2f}–{min(hi, 1.0):.2f}", "n": len(b),
                        "wins": sum(1 for t in b if t["pnl"] > 0),
                        "pnl": sum(t["pnl"] for t in b),
                        "breakeven": (sum(t["entry_price"] for t in b) / len(b)) if b else None})
    return {
        "trades": len(closed), "open": len(ts) - len(closed), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(closed) if closed else None,
        "pnl": sum(pnls), "staked": staked, "roi": sum(pnls) / staked if staked else None,
        "today_pnl": sum(t["pnl"] for t in closed if journal.today(t["closed_ts"] or t["ts"]) == today),
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "best": max(pnls) if pnls else None, "worst": min(pnls) if pnls else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses and sum(losses) < 0 else None,
        "max_drawdown": dd,
        "results": {k: sum(1 for t in closed if t["result"] == k) for k in ("won", "lost", "stopped", "sold")},
        "buckets": buckets,
    }


# ---------------------------------------------------------------- bot process
class Bot:
    def __init__(self, s: Settings):
        self.s = s
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.stopping = False
        self.last_exit: str | None = None
        self.crashes = 0
        self.crashed_at: float | None = None
        self.lock = threading.Lock()
        s.data_dir.mkdir(parents=True, exist_ok=True)

    @property
    def log_path(self) -> Path:
        return self.s.data_dir / "bot.log"

    @property
    def state_path(self) -> Path:
        return self.s.data_dir / "bot.state"

    def wanted(self) -> bool:
        return self.state_path.exists() and self.state_path.read_text().strip() == "running"

    def running(self) -> bool:
        with self.lock:
            if self.proc is None:
                return False
            code = self.proc.poll()
            if code is None:
                return True
            if self.stopping:
                pass  # Stop was pressed
            elif code == 0:
                # A deliberate exit: out of cash, kill switch or a blocked location.
                self.last_exit = "bot stopped itself - out of cash, kill switch or a blocked location; see the log"
                self.state_path.write_text("stopped")
            else:
                # A crash: the watchdog restarts it (state stays "running").
                self.crashes += 1
                self.crashed_at = time.time()
                self.last_exit = f"bot crashed (exit code {code}, crash #{self.crashes}); restarting automatically"
            self.proc, self.stopping = None, False
            return False

    def restart_delay(self) -> float:
        return min(300.0, 10.0 * 2 ** min(self.crashes - 1, 5))

    def watchdog(self) -> None:
        """Restart a crashed bot while it is meant to run; forget old crashes after 30 min up."""
        while True:
            time.sleep(5)
            try:
                if self.running():
                    if self.crashes and self.started_at and time.time() - self.started_at > 1800:
                        self.crashes = 0
                elif self.wanted() and self.crashed_at and time.time() - self.crashed_at >= self.restart_delay():
                    self.crashed_at = None
                    with open(self.log_path, "a", encoding="utf-8") as f:
                        f.write(f"\n[dashboard] restarting after crash #{self.crashes}\n")
                    self.start()
            except Exception as e:  # noqa: BLE001 - the watchdog must outlive any error
                print(f"watchdog error: {e}", flush=True)

    def start(self) -> str:
        if self.running():
            return "already running"
        with self.lock:
            try:
                if self.log_path.stat().st_size > 20_000_000:  # keep one 20 MB backup
                    self.log_path.replace(self.log_path.with_suffix(".log.1"))
            except OSError:
                pass
            log = open(self.log_path, "a", encoding="utf-8")
            log.write(f"\n===== started {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())} =====\n")
            log.flush()
            kw = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            self.proc = subprocess.Popen([sys.executable, "-u", "-m", "polybot.cli", *bot_args()],
                                         stdout=log, stderr=subprocess.STDOUT, **kw)
            log.close()
            self.started_at, self.stopping, self.crashed_at = time.time(), False, None
            self.state_path.write_text("running")
        return "started"

    def stop(self) -> str:
        self.state_path.write_text("stopped")
        with self.lock:
            p = self.proc
            if p is None or p.poll() is not None:
                return "not running"
            self.stopping = True
            p.send_signal(signal.SIGINT if os.name != "nt" else signal.CTRL_BREAK_EVENT)

        def reap() -> None:
            # The bot finishes managing an open position before exiting: up to one window.
            try:
                p.wait(timeout=330)
            except subprocess.TimeoutExpired:
                p.kill()
        threading.Thread(target=reap, daemon=True).start()
        return "stopping"

    def log_tail(self, n: int) -> list[str]:
        if not self.log_path.exists():
            return []
        with open(self.log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        return lines[-n:]


# ---------------------------------------------------------------- live window
def latest_scan(data_dir: Path, now: float) -> dict | None:
    d = data_dir / "briefs"
    if not d.is_dir():
        return None
    newest = max((e.name for e in os.scandir(d) if e.name.endswith(".json")), default=None)
    if newest is None:
        return None
    try:
        b = json.loads((d / newest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    age = now - float(b.get("fetched_at", 0))
    if age > 600:
        return None
    dec = b.get("decision", {})
    up, dn = dec.get("up", {}), dec.get("down", {})
    return {"slug": b.get("slug"), "age": age, "seconds_left": max(0.0, float(b.get("seconds_left", 0)) - age),
            "btc": b.get("btc"), "open_price": b.get("open_price"), "source": b.get("source"),
            "up_ask": up.get("best_ask"), "down_ask": dn.get("best_ask"),
            "up_bid": up.get("best_bid"), "down_bid": dn.get("best_bid"), "fair_up": dec.get("fair_up")}


# ---------------------------------------------------------------- http
class App:
    def __init__(self, s: Settings):
        self.s, self.bot = s, Bot(s)
        self._cash: tuple[float, float | None, str | None] = (0.0, None, None)
        self._broker = None

    def cash(self) -> tuple[float | None, str | None]:
        at, val, err = self._cash
        if time.time() - at > 30:
            from .broker import Broker
            try:
                self._broker = self._broker or Broker(self.s)  # one signing client, not one per read
                val, err = self._broker.cash(), None
            except Exception as e:  # noqa: BLE001 - shown on the page, never fatal
                val, err = None, str(e)[:120]
            self._cash = (time.time(), val, err)
        return val, err

    def summary(self, mode: str) -> dict:
        now = time.time()
        ts = trades(journal.rows(self.s.data_dir), mode)
        cash, cash_err = self.cash()
        r = self.s.risk
        return {
            "now": now, "mode": "live" if self.s.is_live else "paper", "view": mode,
            "running": self.bot.running(), "stopping": self.bot.stopping, "started_at": self.bot.started_at,
            "last_exit": self.bot.last_exit,
            "kill": self.s.kill_file.exists(), "cash": cash, "cash_error": cash_err,
            "config": {"min_entry": r.min_entry_price, "max_entry": r.max_entry_price, "stop_pct": r.stop_loss_pct,
                       "stake": r.max_stake_usd, "enter_at": 120, "daily_loss_limit": r.daily_loss_limit_usd,
                       "max_trades_per_day": r.max_trades_per_day, "blackout": r.blackout_et},
            "stats": stats(ts, now), "trades": ts, "live": latest_scan(self.s.data_dir, now),
        }


def make_handler(app: App):
    secret = app.s.dashboard_password

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_a) -> None:  # keep the Railway log for the bot
            pass

        def _authed(self) -> bool:
            if not secret:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                except ValueError:
                    pw = ""
                if hmac.compare_digest(pw.encode(), secret.encode()):
                    return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Bot B"')
            self.end_headers()
            return False

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj).encode(), "application/json")

        def do_GET(self) -> None:  # noqa: N802
            if not self._authed():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            if u.path == "/":
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif u.path == "/api/summary":
                mode = q.get("mode", ["live" if app.s.is_live else "paper"])[0]
                self._json(app.summary("live" if mode == "live" else "paper"))
            elif u.path == "/api/log":
                self._json({"lines": app.bot.log_tail(min(int(q.get("n", ["300"])[0]), 2000))})
            elif u.path == "/healthz":
                self._send(200, b"ok", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if not self._authed():
                return
            # A cross-site form cannot set this header, so a page elsewhere cannot
            # press Start/Stop with the browser's saved password.
            if self.headers.get("X-Bot-Action") != "1":
                self._json({"error": "missing X-Bot-Action header"}, 403)
                return
            path = urlparse(self.path).path
            if path == "/api/start":
                self._json({"result": app.bot.start()})
            elif path == "/api/stop":
                self._json({"result": app.bot.stop()})
            else:
                self._json({"error": "not found"}, 404)

    return H


def serve(s: Settings) -> int:
    app = App(s)
    host = "0.0.0.0" if s.dashboard_password else "127.0.0.1"
    if not s.dashboard_password:
        print("PM_DASHBOARD_PASSWORD is not set: the dashboard listens on 127.0.0.1 only "
              "(set it to reach the page from Railway).", flush=True)
    if app.bot.wanted():
        print(f"bot was running before this restart: starting it again ({app.bot.start()})", flush=True)
    threading.Thread(target=app.bot.watchdog, daemon=True).start()

    def on_term(*_a) -> None:  # Railway sends SIGTERM on redeploy
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    srv = ThreadingHTTPServer((host, s.port), make_handler(app))
    print(f"dashboard on http://{host}:{s.port}  [{'LIVE' if s.is_live else 'PAPER'}]", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.bot.running():
            p = app.bot.proc
            app.bot.stop()
            # A redeploy is not the user pressing Stop: keep it running after the restart.
            (s.data_dir / "bot.state").write_text("running")
            try:
                p.wait(timeout=20)  # between windows the bot exits at once
            except Exception:  # noqa: BLE001
                pass
    return 0
