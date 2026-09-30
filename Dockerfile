# Bot B on Railway: favourite strategy, -40% stop, runs every window until cash runs out.
# Paper unless PM_MODE=live AND PM_LIVE_CONFIRMED=true are set in Railway's variables.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev

# Journal, scans and the kill switch live on a Railway volume mounted at /data.
ENV PATH="/app/.venv/bin:$PATH" PM_DATA_DIR=/data

# `pm doctor` first so the deploy log shows connectivity and the geoblock verdict.
# Stake is PM_MAX_STAKE_USD, stop is PM_STOP_LOSS_PCT; --forever pauses on daily limits
# and exits only when cash runs out, the kill switch is on, or geoblock fails.
CMD ["sh", "-c", "pm doctor; exec pm auto --strategy favourite --enter-at 120 --hold --fixed --forever"]
