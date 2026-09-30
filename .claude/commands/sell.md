---
description: Check the open position and sell it (or explain why holding is better)
argument-hint: "[up|down] [--now]"
---
1. Run `uv run pm manage` and read the advice (fair value vs net bid).
2. If the user passed `--now` or named a side and asked to sell, run `uv run pm sell <side>`.
   Otherwise follow the advice: sell only if it says SELL, else explain why holding to resolution has higher expected value.
3. Report the result and the updated `uv run pm status`.

Arguments: $ARGUMENTS
