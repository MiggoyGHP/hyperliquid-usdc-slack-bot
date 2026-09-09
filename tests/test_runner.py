from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from hl_usdc_bot.bands import Band
from hl_usdc_bot.decide import Reason
from hl_usdc_bot.hyperliquid import SpotPrices
from hl_usdc_bot.runner import run_tick
from hl_usdc_bot.slack import SlackPostFailed
from hl_usdc_bot.state import BotState, LocalFileStateStore
from tests.test_decide import CONFIG, NOW, posted, reading


class RecordingPoster:
    def __init__(self, fails=False):
        self.payloads = []
        self.fails = fails

    def __call__(self, payload, url, **kwargs):
        if self.fails:
            raise SlackPostFailed("Slack said no")
        self.payloads.append(payload)


def store_at(tmp_path, state):
    store = LocalFileStateStore(tmp_path / "state.json")
    if state is not None:
        store.save(state)
    return store


def test_a_due_tick_posts_to_slack(tmp_path):
    poster = RecordingPoster()

    result = run_tick(
        CONFIG,
        store_at(tmp_path, None),
        now=NOW,
        fetch_reserve=lambda **_: reading("0.64"),
        post=poster,
    )

    assert result.posted
    assert len(poster.payloads) == 1


def test_a_due_tick_records_the_reading_for_next_time(tmp_path):
    store = store_at(tmp_path, None)

    run_tick(
        CONFIG, store, now=NOW, fetch_reserve=lambda **_: reading("0.64"), post=RecordingPoster()
    )

    saved = store.load()
    assert saved.last_post_ts == NOW
    assert saved.last_utilization == Decimal("0.64")
    assert saved.last_band is Band.NORMAL


def test_a_suppressed_tick_posts_nothing(tmp_path):
    poster = RecordingPoster()
    store = store_at(tmp_path, posted(timedelta(hours=1), "0.63", Band.NORMAL))

    result = run_tick(
        CONFIG, store, now=NOW, fetch_reserve=lambda **_: reading("0.64"), post=poster
    )

    assert not result.posted
    assert result.reason is Reason.SUPPRESSED
    assert poster.payloads == []


def test_a_suppressed_tick_leaves_the_stored_timestamp_alone(tmp_path):
    original = posted(timedelta(hours=1), "0.63", Band.NORMAL)
    store = store_at(tmp_path, original)

    run_tick(
        CONFIG, store, now=NOW, fetch_reserve=lambda **_: reading("0.64"), post=RecordingPoster()
    )

    # Otherwise the heartbeat clock would restart on every quiet tick and
    # the 6-hourly post would never come due.
    assert store.load().last_post_ts == original.last_post_ts


def test_state_is_not_advanced_when_slack_rejects_the_post(tmp_path):
    store = store_at(tmp_path, None)

    with pytest.raises(SlackPostFailed):
        run_tick(
            CONFIG,
            store,
            now=NOW,
            fetch_reserve=lambda **_: reading("0.64"),
            post=RecordingPoster(fails=True),
        )

    # Still first-run, so the next tick retries rather than silently
    # swallowing a band crossing.
    assert store.load() == BotState.empty()


def test_dry_run_does_not_call_slack(tmp_path):
    poster = RecordingPoster()

    result = run_tick(
        replace(CONFIG, dry_run=True),
        store_at(tmp_path, None),
        now=NOW,
        fetch_reserve=lambda **_: reading("0.64"),
        post=poster,
    )

    assert result.posted
    assert poster.payloads == []
    assert result.payload is not None


def test_dry_run_still_advances_state_so_cadence_can_be_exercised_locally(tmp_path):
    store = store_at(tmp_path, None)
    config = replace(CONFIG, dry_run=True)

    first = run_tick(config, store, now=NOW, fetch_reserve=lambda **_: reading("0.64"))
    second = run_tick(
        config, store, now=NOW + timedelta(hours=1), fetch_reserve=lambda **_: reading("0.64")
    )

    assert first.posted
    assert not second.posted


def test_a_crossing_is_posted_even_moments_after_the_last_post(tmp_path):
    poster = RecordingPoster()
    store = store_at(tmp_path, posted(timedelta(minutes=1), "0.78", Band.NORMAL))

    result = run_tick(
        CONFIG, store, now=NOW, fetch_reserve=lambda **_: reading("0.805"), post=poster
    )

    assert result.posted
    assert result.reason is Reason.BAND_UPGRADE
    assert "Crossed 80%" in str(poster.payloads[0])


class RecordingPrices:
    """A stand-in for hyperliquid.fetch_spot_prices that counts its calls."""

    def __init__(self, prices=None, fails=False):
        self.prices = prices or SpotPrices(btc=Decimal("79063.5"), eth=Decimal("2503.65"))
        self.fails = fails
        self.calls = 0

    def __call__(self, **_):
        self.calls += 1
        if self.fails:
            raise RuntimeError("Hyperliquid said no")
        return self.prices


def test_a_posted_message_carries_the_prices_captured_this_tick(tmp_path):
    poster = RecordingPoster()

    run_tick(
        CONFIG,
        store_at(tmp_path, None),
        now=NOW,
        fetch_reserve=lambda **_: reading("0.64"),
        fetch_prices=RecordingPrices(),
        post=poster,
    )

    assert "$79,063.50" in str(poster.payloads[0])


def test_a_failed_price_read_still_posts_the_utilization_alert(tmp_path):
    # Prices are context; the alert is about USDC. Losing one must not lose the other.
    poster = RecordingPoster()
    store = store_at(tmp_path, None)

    result = run_tick(
        CONFIG,
        store,
        now=NOW,
        fetch_reserve=lambda **_: reading("0.64"),
        fetch_prices=RecordingPrices(fails=True),
        post=poster,
    )

    assert result.posted
    assert "BTC spot" not in str(poster.payloads[0])
    assert store.load().last_post_ts == NOW


def test_a_suppressed_tick_never_reads_prices(tmp_path):
    # A quiet tick must cost exactly what it cost before prices existed.
    prices = RecordingPrices()

    run_tick(
        CONFIG,
        store_at(tmp_path, posted(timedelta(hours=1), "0.63", Band.NORMAL)),
        now=NOW,
        fetch_reserve=lambda **_: reading("0.64"),
        fetch_prices=prices,
        post=RecordingPoster(),
    )

    assert prices.calls == 0
