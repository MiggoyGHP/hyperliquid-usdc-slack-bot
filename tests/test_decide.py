from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hl_usdc_bot.bands import Band
from hl_usdc_bot.config import Config
from hl_usdc_bot.decide import Reason, decide
from hl_usdc_bot.hyperliquid import ReserveState
from hl_usdc_bot.state import BotState

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CONFIG = Config(slack_webhook_url="https://hooks.slack.example/test", heartbeat_hours=6)


def reading(utilization: str) -> ReserveState:
    """A reserve state at a given utilization; other fields kept plausible."""
    util = Decimal(utilization)
    supplied = Decimal("400000000")
    borrowed = supplied * util
    return ReserveState(
        utilization=util,
        borrow_yearly_rate=Decimal("0.05"),
        supply_yearly_rate=Decimal("0.0288"),
        total_supplied=supplied,
        total_borrowed=borrowed,
        available=supplied - borrowed,
    )


def posted(ago: timedelta, utilization: str, band: Band) -> BotState:
    return BotState(
        last_post_ts=NOW - ago,
        last_utilization=Decimal(utilization),
        last_band=band,
    )


def test_first_ever_run_posts():
    decision = decide(reading("0.64"), BotState.empty(), NOW, CONFIG)

    assert decision.should_post
    assert decision.reason is Reason.FIRST_RUN
    assert decision.band is Band.NORMAL


def test_first_ever_run_has_no_delta_to_report():
    decision = decide(reading("0.64"), BotState.empty(), NOW, CONFIG)

    assert decision.delta_pp is None


def test_quiet_market_is_suppressed_before_the_heartbeat_is_due():
    decision = decide(
        reading("0.64"), posted(timedelta(hours=5), "0.63", Band.NORMAL), NOW, CONFIG
    )

    assert not decision.should_post
    assert decision.reason is Reason.SUPPRESSED


def test_quiet_market_posts_once_the_heartbeat_is_due():
    decision = decide(
        reading("0.64"), posted(timedelta(hours=6), "0.63", Band.NORMAL), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.reason is Reason.HEARTBEAT


def test_escalated_utilization_posts_every_hour():
    decision = decide(
        reading("0.79"), posted(timedelta(hours=1), "0.78", Band.NORMAL), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.reason is Reason.ESCALATED_HEARTBEAT


def test_escalated_utilization_is_still_suppressed_within_the_hour():
    decision = decide(
        reading("0.79"), posted(timedelta(minutes=30), "0.78", Band.NORMAL), NOW, CONFIG
    )

    assert not decision.should_post


def test_escalation_threshold_is_below_the_band_boundary():
    # 0.79 escalates cadence while the band is still NORMAL - that is the point.
    decision = decide(
        reading("0.79"), posted(timedelta(hours=1), "0.78", Band.NORMAL), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.band is Band.NORMAL


def test_just_below_the_escalation_threshold_keeps_the_slow_cadence():
    decision = decide(
        reading("0.789"), posted(timedelta(hours=1), "0.78", Band.NORMAL), NOW, CONFIG
    )

    assert not decision.should_post


def test_crossing_the_kink_posts_immediately_regardless_of_cadence():
    decision = decide(
        reading("0.801"), posted(timedelta(minutes=1), "0.78", Band.NORMAL), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.reason is Reason.BAND_UPGRADE
    assert decision.band is Band.HIGH


def test_entering_critical_posts_immediately():
    decision = decide(
        reading("0.91"), posted(timedelta(minutes=1), "0.85", Band.HIGH), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.reason is Reason.BAND_UPGRADE
    assert decision.band is Band.CRITICAL


def test_recovering_below_the_kink_posts_immediately():
    decision = decide(
        reading("0.78"), posted(timedelta(minutes=1), "0.81", Band.HIGH), NOW, CONFIG
    )

    assert decision.should_post
    assert decision.reason is Reason.BAND_DOWNGRADE
    assert decision.band is Band.NORMAL


def test_a_dip_inside_the_hysteresis_buffer_is_not_a_recovery():
    decision = decide(
        reading("0.795"), posted(timedelta(minutes=1), "0.81", Band.HIGH), NOW, CONFIG
    )

    assert not decision.should_post
    assert decision.band is Band.HIGH


def test_delta_is_reported_in_percentage_points():
    decision = decide(
        reading("0.6510"), posted(timedelta(hours=6), "0.6410", Band.NORMAL), NOW, CONFIG
    )

    assert decision.delta_pp == Decimal("1.00")


def test_delta_keeps_its_sign_when_utilization_falls():
    decision = decide(
        reading("0.6310"), posted(timedelta(hours=6), "0.6410", Band.NORMAL), NOW, CONFIG
    )

    assert decision.delta_pp == Decimal("-1.00")


def test_a_suppressed_tick_still_reports_the_current_band():
    decision = decide(
        reading("0.85"), posted(timedelta(minutes=5), "0.84", Band.HIGH), NOW, CONFIG
    )

    assert not decision.should_post
    assert decision.band is Band.HIGH


def test_state_with_a_timestamp_but_no_previous_band_is_treated_as_first_run():
    stale = BotState(last_post_ts=NOW - timedelta(hours=1), last_utilization=None, last_band=None)

    decision = decide(reading("0.64"), stale, NOW, CONFIG)

    assert decision.should_post
    assert decision.reason is Reason.FIRST_RUN


# Polling frequency and posting frequency are separate knobs. The workflow polls
# every 10 minutes to shorten the wait for a crossing; these two pin the property
# that makes that safe, so nobody reverts the cron believing it spams the channel.

# Both hold the previous state fixed across the grid, which is what really happens:
# a suppressed tick never advances state.


def test_polling_faster_than_the_heartbeat_does_not_multiply_posts():
    hourly = replace(CONFIG, heartbeat_hours=1)
    quiet = posted(timedelta(0), "0.63", Band.NORMAL)

    reasons = [
        decide(reading("0.64"), quiet, NOW + timedelta(minutes=m), hourly).reason
        for m in range(10, 61, 10)
    ]

    assert reasons == [Reason.SUPPRESSED] * 5 + [Reason.HEARTBEAT]


def test_a_crossing_posts_at_the_poll_that_sees_it_not_at_the_heartbeat():
    hourly = replace(CONFIG, heartbeat_hours=1)
    quiet = posted(timedelta(0), "0.78", Band.NORMAL)
    # Utilization crosses the kink between the first poll and the second.
    climbing = ["0.79", "0.801", "0.802", "0.803", "0.804", "0.805"]

    reasons = [
        decide(reading(u), quiet, NOW + timedelta(minutes=m), hourly).reason
        for m, u in zip(range(10, 61, 10), climbing)
    ]

    # 50 minutes before the heartbeat would have come due. That is the whole
    # point of the faster cron; an hourly poll would have missed this one.
    assert reasons[0] is Reason.SUPPRESSED
    assert reasons[1] is Reason.BAND_UPGRADE
