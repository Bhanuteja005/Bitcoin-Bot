# Polymarket BTC 5m desk — operating brief

Trades Polymarket's "Bitcoin Up or Down — 5 minute" markets from Claude Code.
Python (`src/polybot`), one CLI: `pm`. Trading rules and the analyst persona live in
`prompts/btc-5m-trader.md` — **read it before making any call.**

## Before anything else

**Nothing trades live by default.** `PM_MODE=dry_run` paper-trades against the real
order book. Live needs *both* `PM_MODE=live` and `PM_LIVE_CONFIRMED=true` in `.env`, set
by the user. Never set or suggest flipping either on the user's behalf.

**Never inline a secret.** The wallet private key lives only in `.env` (gitignored) and
is read only by `src/polybot/config.py`. Never print it, echo it, or ask the user to
paste it into chat. If they do, tell them to rotate it.

**Geoblock is a hard gate.** Live orders refuse unless `https://polymarket.com/api/geoblock`
says `blocked: false`. Never suggest VPNs, DNS overrides or proxies to get around a
Polymarket block — trading from a restricted location breaks Polymarket's terms and can
get the account's funds frozen.

## When the user says "buy", "sell", "scan", "trade", "what now"

You are the analyst. Commands (run from the repo root; `uv run pm …`):

| Intent | Command |
| --- | --- |
| Read the market | `uv run pm scan` |
| Buy (model's side) | `uv run pm buy auto` |
| Buy a specific side | `uv run pm buy up` / `uv run pm buy down` (refused if model disagrees) |
| User insists against the model | `uv run pm buy up --override` — only on explicit user instruction |
| Exit advice | `uv run pm manage` |
| Auto-exit loop till close | `uv run pm watch --auto-sell` |
| Autopilot, every window | `uv run pm auto` (run in background; `--advise` = calls only) |
| Sell now | `uv run pm sell up` / `uv run pm sell down` |
| Account | `uv run pm status`, `uv run pm pnl`, `uv run pm settle` |
| Strategy report (every trade, calibration) | `uv run pm report` |
| Emergency stop | `uv run pm kill on` |
| Connectivity | `uv run pm doctor` (add `--auth` to test wallet creds) |
| Resolution rules of the live market | `uv run pm rules` |

Flow for "buy": `pm scan` → read the brief against the prompt's playbook → give the
call in the prompt's OUTPUT FORMAT → if it is a trade, run `pm buy <side>` **immediately**
(the brief expires in 20s; `buy` re-fetches and re-checks anyway) → report the fill →
state the exit plan. If the model says NO_TRADE, say so; do not reach for `--override`.

Speed: `pm buy` fetches everything in parallel and sends in one go (~0.5s + exchange).
Do not run `pm scan` and then deliberate for a minute — decide in one pass.

Autopilot (`pm auto`) runs the same model and the same risk gates with no human in the
loop: it sleeps until the entry zone (last `PM_ENTRY_ZONE_S`, default 150s), scans every
second, enters on edge, manages the exit, settles, and moves to the next window. It stops
itself on the kill switch, the daily loss limit, or no cash. Start it with
`run_in_background` and report the output; never start it in live mode unless the user
asked for live autopilot in this session.

`scripts/sim_live.py` rehearses the autopilot against real BTC with a simulated book
(data in `data-sim/`). Its P&L is not evidence of profitability.

Exchange access is Polymarket's official V2 SDK (`polymarket-client`, imported as
`polymarket`). The old `py-clob-client` stopped working with CLOB V2 (Apr 2026) — do not
reintroduce it. `pm settle` redeems resolved live positions through the SDK.

## Rules that hold everywhere

- **Only `config.py` reads the environment.** Add settings there and pass `Settings` down.
- **`model.py` and `risk.py` stay pure** — no clock, no I/O. They are what the tests pin.
- **One route to the exchange: `broker.py`.** The mode gate, duplicate guard and journal
  live behind it. Do not post orders from anywhere else.
- **Failures are refusals.** A dead feed, a stale brief, missing liquidity → refuse and
  say why. Never substitute a guessed price.
- **A risk refusal is final.** Do not retry with a different stake/cap/flag to get past it.

## Verifying a change

```bash
uv run pytest -q
```
The safety tests matter most: dry-run default, kill switch, duplicate guard, FOK price
cap, one position per window, daily loss limit.

## Model note

Claude Opus 5.5 (`claude-opus-5-5`) is the analyst when driven from Claude Code.
