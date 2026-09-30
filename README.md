# polybot — Polymarket BTC 5-minute desk

Scan, buy, manage and sell Polymarket's **Bitcoin Up or Down — 5 minute** markets
from Claude Code (or your terminal). Paper-trades by default.

```
src/polybot/
  config.py      only place that reads .env; risk mandate defaults
  window.py      5-minute window / slug arithmetic (pure)
  model.py       fair value, fees, edge, entry + exit decisions (pure)
  risk.py        pre-trade gates (pure)
  feeds.py       BTC spot, candles, window-open price (Binance)
  polymarket.py  Gamma market lookup, CLOB order books, geoblock, positions
  brief.py       one parallel fetch -> model -> brief (saved to data/briefs/)
  broker.py      the only route to the exchange (paper sim, or V2 SDK FOK + settlement check)
  journal.py     append-only data/journal.jsonl; positions, cash, P&L replayed from it
  cli.py         `pm` command
prompts/btc-5m-trader.md   the trader prompt Claude follows
.claude/commands/          /buy /sell /scan /desk
tests/                     model, risk, paper broker, full CLI flow
```

## Setup

```bash
uv sync
cp .env.example .env        # fill in wallet only when going live
uv run pm doctor            # connectivity: binance, gamma, clob, geoblock
uv run pytest -q
```

## Use from Claude Code

Open Claude Code in this folder and type `/buy`, `/sell`, `/scan`, `/desk` — or just
say "buy", "should I buy up?", "sell it", "status". See `CLAUDE.md`.

## Autopilot

```bash
uv run pm auto              # trade every window until stopped (paper unless live is enabled)
uv run pm auto --advise     # print the calls only, never send an order
uv run python scripts/sim_live.py --rounds 2   # rehearsal: real BTC, simulated book
```

## How the edge works

Settlement is a 60s Chainlink TWAP; the model prices the closing average (variance
σ²(t−60) + σ²·60/3, and inside the last minute only the unprinted part can move). The market pays $1 to the side that wins; if
the model says Up is worth 0.80 and the book sells it at 0.70 + 0.01 fee, edge is 0.09.
Early in a window both are ~0.50 and the bot correctly does nothing. Exits: sell only
when the bid pays more than fair value, otherwise hold to resolution.

## Bot B on Railway

`Dockerfile` runs Bot B: buy the favourite (0.60-0.95) with 2 minutes left, $`PM_MAX_STAKE_USD`
fixed, sell if the bid falls `PM_STOP_LOSS_PCT` below entry, otherwise hold to the result.
`--forever` pauses on the daily limits and exits only when cash runs out, the kill switch is
on (`/data/KILL`), or geoblock fails. Add a volume at `/data`; set the variables in Railway
(paper unless `PM_MODE=live` and `PM_LIVE_CONFIRMED=true`).

## Going live (checklist)

1. `uv run pm doctor` shows **ok** for gamma, clob and geoblock (`blocked=False`).
2. `.env` has `PM_PRIVATE_KEY` and `PM_FUNDER_ADDRESS`.
3. `uv run pm doctor --auth` shows your cash (pUSD).
4. You have made at least one trade in the Polymarket UI (sets token allowances; the SDK
   also recovers missing allowances itself).
5. `uv run pm rules` matches the model's assumption (60s TWAP strike and close).
6. 30+ paper trades with positive realised P&L (`uv run pm pnl`).
7. Set `PM_MODE=live` and `PM_LIVE_CONFIRMED=true` yourself.
