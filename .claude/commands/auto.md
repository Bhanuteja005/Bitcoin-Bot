---
description: Start the autopilot (paper unless live is enabled) or advise-only mode
argument-hint: "[--advise] [--rounds N]"
---
Run `uv run pm status` first and state the mode. Then start `uv run pm auto $ARGUMENTS` with run_in_background.
If the mode is LIVE, confirm the user asked for live autopilot in this session before starting.
Report each trade, refusal and settlement as it appears in the output, and the day's realised P&L when it stops.
