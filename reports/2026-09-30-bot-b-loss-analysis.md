# Bot B (favourite strategy): trade and loss analysis, 30 Sep 2026

Paper trading only, $1 per trade. Data: `data-fav/journal.jsonl` and `data-fav/briefs/`, with
results taken from Polymarket's official resolution.

## Where things stand

| | Trades | Won | Lost | P&L |
|---|---|---|---|---|
| **Bot B (favourite)** | 21 closed | 16 (76%) | 5 | **−$0.84** |
| Bot A (learned model) | 0 | – | – | $0.00 (no window met its rules) |
| Live account | 4 | 2 | 2 | −$1.11 (cash $0.82, not trading) |

**Bot B's rule:** buy the favoured side (price 0.60–0.95) with about 2 minutes left, $1 stake.
Winners are held to the result. Losers are sold once the bid falls to the stop level:
- trades 1–11: no stop
- trades 12–20: stop at a bid of 0.20
- trades 21 onward: stop at a bid of 0.30

## All 21 trades

"Lead" is how far BTC was from the opening price, in dollars, in the favourite's direction at entry.
z is that lead measured against normal BTC noise.

| # | Side | Price | Model fair | \|z\| | Lead | Exit | Winner | P&L |
|---|---|---|---|---|---|---|---|---|
| 1 | Up | 0.87 | 0.79 | 0.57 | $76 | held | Up | +0.14 |
| 2 | Down | 0.81 | 0.76 | 0.57 | $95 | held | Down | +0.22 |
| 3 | Down | 0.95 | 0.97 | 1.45 | $227 | held | Down | +0.05 |
| 4 | Up | 0.90 | 0.88 | 0.95 | $148 | held | Up | +0.10 |
| **5** | Up | 0.68 | 0.62 | **0.25** | **$37** | held | **Down** | **−1.00** |
| 6 | Up | 0.83 | 0.77 | 0.59 | $72 | held | Up | +0.19 |
| **7** | Down | 0.75 | 0.72 | **0.47** | **$55** | held | **Up** | **−1.00** |
| 8 | Up | 0.62 | 0.57 | 0.14 | $16 | held | Up | +0.57 |
| 9 | Up | 0.67 | 0.60 | 0.21 | $23 | held | Up | +0.46 |
| 10 | Down | 0.91 | 0.83 | 0.78 | $86 | held | Down | +0.09 |
| 11 | Up | 0.91 | 0.84 | 0.80 | $82 | held | Up | +0.09 |
| **12** | Down | 0.74 | 0.72 | **0.47** | **$47** | stop @ 0.17 | **Up** | **−0.79** |
| 13 | Up | 0.89 | 0.79 | 0.67 | $82 | held | Up | +0.12 |
| 14 | Down | 0.67 | 0.57 | 0.15 | $17 | held | Down | +0.46 |
| 15 | Up | 0.94 | 0.86 | 0.89 | $79 | held | Up | +0.06 |
| 16 | Down | 0.72 | 0.65 | 0.32 | $29 | held | Down | +0.36 |
| 17 | Up | 0.79 | 0.77 | 0.60 | $49 | held | Up | +0.25 |
| 18 | Down | 0.93 | 0.89 | 0.99 | $79 | held | Down | +0.07 |
| **19** | Down | 0.80 | **0.61** | **0.23** | **$19** | stop @ 0.20 | **Up** | **−0.77** |
| 20 | Up | 0.81 | 0.77 | 0.59 | $46 | held | Up | +0.22 |
| **21** | Down | 0.77 | 0.76 | **0.58** | **$32** | stop @ 0.22 | pending | **−0.73** |

## Why the 5 trades lost

1. **Every loss had a small BTC lead, $19–55.** BTC hadn't moved far from the opening price, so a
   normal wiggle in the last 2 minutes could flip the result.
   - **Lead ≥ $60: 10 trades, 10 won (100%).**
   - **Lead < $60: 11 trades, 6 won, 5 lost (55%).** At an average price of about 0.72, that loses money.
2. **The move was weak compared with normal noise.** The losses had |z| of 0.23–0.58.
   - **|z| ≥ 0.59: 12 trades, 12 won.**
   - That z threshold sits right against #21 (0.58), so it's a fragile cut-off. The $ lead gives a
     wider gap between wins and losses.
3. **Every loss was bought at 0.80 or less** (0.68, 0.74, 0.75, 0.77, 0.80). Every trade at 0.81 or more
   won (11/11). A cheap price just reflects the small lead, so this is the same signal as points 1–2.
4. **The model's warning didn't reliably separate wins from losses.** The average model fair minus price
   was about −0.06 for both. Only #19 had a large warning (−0.19), and it lost.
5. **Direction:** 4 of the 5 losses were **Down** bets that Up won. BTC was drifting up during the
   session. Treat this as noise, not a rule.

## Did the stop-loss help?

| Trade | Stopped side | Actual winner | Verdict |
|---|---|---|---|
| #12 | Down | Up | ✅ right: kept $0.21 instead of $0 |
| #19 | Down | Up | ✅ right: kept $0.23 instead of $0 |
| #21 | Down | pending | check with `PM_DATA_DIR=data-fav uv run pm report` |

In both confirmed cases the stopped side went on to lose. Fast drops fill below the trigger: #21 hit 0.30
but sold at 0.22, because the price fell between one-second checks.

## Recommended next change (not applied yet)

**Only buy the favourite when BTC's lead is ≥ $60 (or |z| ≥ 0.6).** On these 21 trades, that rule would
have taken about 10–12 trades with no losses, at roughly +$1.0–1.2, instead of −$0.84.

**Caution:** the cut-off was chosen by looking at these same 21 trades, so it is certainly overfitted.
Before trusting it:
1. Backtest it on the older saved scans in `data/briefs/` (80+ windows), which weren't used to choose it.
2. Run it on paper for 100+ new trades.

Two things to know about the stricter rule:
- It buys only when the lead is large, which means high prices of 0.85–0.95. Each win then pays only
  $0.05–0.15, so one loss wipes out 7–20 wins. It needs a win rate of about 93% or better.
- This matches the research: favourites win slightly more often than their price says, but only by a few cents.

## How to restart (the bots stop when the Claude Code chat closes)

Run from the repo root in your own terminal. Keep each window open.

```bash
# Bot A: learned model, relearns every hour
PM_BLACKOUT_ET= PM_DAILY_LOSS_LIMIT_USD=100 PM_MAX_TRADES_PER_DAY=300 \
  uv run pm auto --fixed --usd 1 --learn-every 12

# Bot B: favourite 0.60–0.95, ~2 min left, sell if the bid falls to 0.30
PM_DATA_DIR="$PWD/data-fav" PM_MIN_ENTRY_PRICE=0.60 PM_MAX_ENTRY_PRICE=0.95 PM_BLACKOUT_ET= \
  PM_DAILY_LOSS_LIMIT_USD=100 PM_MAX_TRADES_PER_DAY=300 \
  uv run pm auto --strategy favourite --enter-at 120 --hold --stop-bid 0.30 --fixed --usd 1

# Reports
uv run pm report                         # Bot A
PM_DATA_DIR="$PWD/data-fav" uv run pm report   # Bot B
uv run pm learn --dry                    # how the learned model scores now
```

`.env` is in paper mode (`PM_MODE=dry_run`). Live trading needs you to set `PM_MODE=live` yourself.

## What changed today (not committed to git)

- `risk`/`model`: added a |z| ≥ 0.5 filter, a reversal filter (`recent_move`), and `PM_MAX_EDGE=0.10` set in `.env`.
- `pm learn` (`src/polybot/learn.py`) fits a correction on the model plus the market price and saves it
  to `data/calibration.json` only if it beats the model on unseen windows. Current fit:
  0 − 0.26 × model + 1.44 × market.
- Every saved scan now also records the raw model chance (`raw_fair_up`).
- Autopilot options: `--strategy favourite`, `--enter-at`, `--hold`, `--stop-bid`, `--learn-every`.
- Positions under 0.01 shares are ignored. The claimer (`pm settle`) needs `PM_BUILDER_API_KEY`,
  `PM_BUILDER_SECRET` and `PM_BUILDER_PASSPHRASE` in `.env`.
- 72 tests pass (`uv run pytest -q`).
