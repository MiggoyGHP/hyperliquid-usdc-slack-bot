"""The whole behaviour of the bot, as one pure function.

No network, no clock, no filesystem: `now` and the previous state are passed
in. That is what makes the full cadence-by-band matrix testable directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum, auto

from hl_usdc_bot.bands import Band, classify
from hl_usdc_bot.config import Config
from hl_usdc_bot.hyperliquid import ReserveState
from hl_usdc_bot.state import BotState


class Reason(Enum):
    FIRST_RUN = auto()
    BAND_UPGRADE = auto()
    BAND_DOWNGRADE = auto()
    ESCALATED_HEARTBEAT = auto()
    HEARTBEAT = auto()
    SUPPRESSED = auto()


@dataclass(frozen=True)
class Decision:
    should_post: bool
    band: Band
    reason: Reason
    delta_pp: Decimal | None
    previous_band: Band | None


def decide(
    reading: ReserveState,
    previous: BotState,
    now: datetime,
    config: Config,
) -> Decision:
    """Whether this tick warrants a Slack post, and why."""
    band = classify(reading.utilization, previous.last_band)
    delta_pp = (
        None
        if previous.last_utilization is None
        else (reading.utilization - previous.last_utilization) * 100
    )

    def decision(should_post: bool, reason: Reason) -> Decision:
        return Decision(
            should_post=should_post,
            band=band,
            reason=reason,
            delta_pp=delta_pp,
            previous_band=previous.last_band,
        )

    if previous.last_post_ts is None or previous.last_band is None:
        return decision(True, Reason.FIRST_RUN)

    if band > previous.last_band:
        return decision(True, Reason.BAND_UPGRADE)
    if band < previous.last_band:
        return decision(True, Reason.BAND_DOWNGRADE)

    escalated = reading.utilization >= config.escalate_at
    interval = timedelta(
        hours=config.escalated_interval_hours if escalated else config.heartbeat_hours
    )

    if now - previous.last_post_ts >= interval:
        return decision(
            True, Reason.ESCALATED_HEARTBEAT if escalated else Reason.HEARTBEAT
        )

    return decision(False, Reason.SUPPRESSED)
