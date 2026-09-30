---
description: Scan the live BTC 5m window and buy if the model has edge
argument-hint: "[up|down|auto] [--usd N]"
---
Read `prompts/btc-5m-trader.md` if it is not already in context. Then, in one pass:

1. Run `uv run pm scan`.
2. Apply the entry playbook to the brief and write the call in the prompt's OUTPUT FORMAT.
3. If it is a trade, immediately run `uv run pm buy ${ARGUMENTS:-auto}` (default side: auto = model's side).
   If the user named a side the model does not support, say so and do not add `--override` unless they explicitly insist.
4. Report the fill (or the refusal, verbatim) and the exit plan.

Arguments: $ARGUMENTS
