"""One tick: read the market, decide, post if warranted, remember."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hl_usdc_bot.bands import Band
from hl_usdc_bot.config import Config
from hl_usdc_bot.decide import Reason, decide
from hl_usdc_bot.hyperliquid import fetch_reserve_state
from hl_usdc_bot.slack import build_message, post_webhook
from hl_usdc_bot.state import BotState

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TickResult:
    posted: bool
    reason: Reason
    band: Band
    utilization: Decimal
    payload: dict | None


async def _resolve(value):
    """Await the value if the collaborator is async.

    Cloudflare KV and the Workers fetch are coroutines; the CPython stores and
    `requests` are not. Both plug into the same orchestration this way.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def run_tick(
    config: Config,
    store,
    *,
    now: datetime | None = None,
    fetch_reserve=fetch_reserve_state,
    post=post_webhook,
) -> TickResult:
    """Run a single tick synchronously. Raises so the caller retries."""
    return asyncio.run(
        run_tick_async(config, store, now=now, fetch_reserve=fetch_reserve, post=post)
    )


async def run_tick_async(
    config: Config,
    store,
    *,
    now: datetime | None = None,
    fetch_reserve=None,
    post=None,
) -> TickResult:
    """The one orchestration, shared by CPython and the Cloudflare Worker.

    Keeping this single means the ordering guarantee below - state advances
    only after Slack accepts - cannot drift between the two runtimes.
    """
    if fetch_reserve is None:
        fetch_reserve = fetch_reserve_state
    if post is None:
        post = post_webhook

    now = now or datetime.now(UTC)

    reading = await _resolve(fetch_reserve(token=config.token_index))
    previous = await _resolve(store.load())
    decision = decide(reading, previous, now, config)

    log.info(
        "utilization=%s band=%s reason=%s post=%s",
        reading.utilization,
        decision.band.name,
        decision.reason.name,
        decision.should_post,
    )

    if not decision.should_post:
        return TickResult(False, decision.reason, decision.band, reading.utilization, None)

    payload = build_message(decision, reading, now, config)

    if not config.dry_run:
        # Only advance state once Slack has accepted the message; a failure
        # here must leave the previous band intact so the next tick retries.
        await _resolve(post(payload, config.slack_webhook_url))

    await _resolve(
        store.save(
            BotState(
                last_post_ts=now,
                last_utilization=reading.utilization,
                last_band=decision.band,
            )
        )
    )

    return TickResult(True, decision.reason, decision.band, reading.utilization, payload)
