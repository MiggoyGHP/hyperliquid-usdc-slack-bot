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
from hl_usdc_bot.hyperliquid import fetch_reserve_state, fetch_spot_prices
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


async def _read_prices(fetch_prices):
    """The spot majors, best-effort.

    Unlike the Slack post, a failure here must not abort the tick: the alert is
    about USDC utilization, and losing it to save two decorative fields would be
    a bad trade. The tick posts without them and says so in the log.
    """
    try:
        return await _resolve(fetch_prices())
    except Exception:  # noqa: BLE001 - any price failure degrades, never blocks
        log.warning("spot price read failed; posting without prices", exc_info=True)
        return None


def run_tick(
    config: Config,
    store,
    *,
    now: datetime | None = None,
    fetch_reserve=fetch_reserve_state,
    fetch_prices=fetch_spot_prices,
    post=post_webhook,
) -> TickResult:
    """Run a single tick synchronously. Raises so the caller retries."""
    return asyncio.run(
        run_tick_async(
            config,
            store,
            now=now,
            fetch_reserve=fetch_reserve,
            fetch_prices=fetch_prices,
            post=post,
        )
    )


async def run_tick_async(
    config: Config,
    store,
    *,
    now: datetime | None = None,
    fetch_reserve=None,
    fetch_prices=None,
    post=None,
) -> TickResult:
    """The one orchestration, shared by CPython and the Cloudflare Worker.

    Keeping this single means the ordering guarantee below - state advances
    only after Slack accepts - cannot drift between the two runtimes.
    """
    if fetch_reserve is None:
        fetch_reserve = fetch_reserve_state
    if fetch_prices is None:
        fetch_prices = fetch_spot_prices
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

    # Read only on a tick that will post: a suppressed tick then costs exactly
    # what it cost before prices existed, and adds no failure surface.
    payload = build_message(decision, reading, now, config, await _read_prices(fetch_prices))

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
