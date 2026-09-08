"""Run one tick from the command line.

    DRY_RUN=1 STATE_FILE=./state.json python -m hl_usdc_bot.tick_once

The development loop: reads the live market, prints the Slack payload it
would send, and advances local state so cadence rules can be exercised.
"""

from __future__ import annotations

import json
import logging
import sys

from hl_usdc_bot.config import Config
from hl_usdc_bot.hyperliquid import fetch_reserve_state
from hl_usdc_bot.runner import run_tick
from hl_usdc_bot.state import build_store


def main(
    config: Config | None = None,
    store=None,
    *,
    now=None,
    fetch_reserve=fetch_reserve_state,
) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    config = config or Config.from_env()
    store = store if store is not None else build_store(config)

    result = run_tick(config, store, now=now, fetch_reserve=fetch_reserve)

    if result.payload is None:
        print(f"No post: {result.reason.name} (utilization {result.utilization})")
    else:
        print(json.dumps(result.payload, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
