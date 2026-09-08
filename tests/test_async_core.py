"""The orchestration is shared by CPython and the Worker.

`run_tick_async` awaits anything awaitable, so the same ordering guarantees -
in particular "save state only after Slack accepts" - hold on both runtimes.
"""

import asyncio
from datetime import timedelta

import pytest

from hl_usdc_bot.bands import Band
from hl_usdc_bot.runner import run_tick_async
from hl_usdc_bot.slack import SlackPostFailed
from hl_usdc_bot.state import BotState
from tests.test_decide import CONFIG, NOW, posted, reading


class AsyncStore:
    """An async store, as the KV binding is."""

    def __init__(self, state=None):
        self.state = state or BotState.empty()
        self.saves = []

    async def load(self):
        return self.state

    async def save(self, state):
        self.saves.append(state)
        self.state = state


class AsyncPoster:
    def __init__(self, fails=False):
        self.payloads = []
        self.fails = fails

    async def __call__(self, payload, url, **kwargs):
        if self.fails:
            raise SlackPostFailed("Slack said no")
        self.payloads.append(payload)


async def async_reading(utilization):
    async def fetch(**_):
        return reading(utilization)

    return fetch


def test_async_store_and_poster_are_awaited(capsys):
    store = AsyncStore()
    poster = AsyncPoster()

    async def fetch(**_):
        return reading("0.64")

    result = asyncio.run(
        run_tick_async(CONFIG, store, now=NOW, fetch_reserve=fetch, post=poster)
    )

    assert result.posted
    assert len(poster.payloads) == 1
    assert store.saves[0].last_band is Band.NORMAL


def test_async_state_is_not_saved_when_slack_rejects_the_post():
    store = AsyncStore()

    async def fetch(**_):
        return reading("0.64")

    with pytest.raises(SlackPostFailed):
        asyncio.run(
            run_tick_async(
                CONFIG, store, now=NOW, fetch_reserve=fetch, post=AsyncPoster(fails=True)
            )
        )

    assert store.saves == []


def test_async_suppression_leaves_state_untouched():
    store = AsyncStore(posted(timedelta(hours=1), "0.63", Band.NORMAL))

    async def fetch(**_):
        return reading("0.64")

    result = asyncio.run(
        run_tick_async(CONFIG, store, now=NOW, fetch_reserve=fetch, post=AsyncPoster())
    )

    assert not result.posted
    assert store.saves == []


def test_sync_callables_still_work_through_the_async_core():
    """The CPython path passes plain functions and a plain store."""

    class SyncStore:
        def __init__(self):
            self.state = BotState.empty()
            self.saves = []

        def load(self):
            return self.state

        def save(self, state):
            self.saves.append(state)

    store = SyncStore()
    posts = []

    result = asyncio.run(
        run_tick_async(
            CONFIG,
            store,
            now=NOW,
            fetch_reserve=lambda **_: reading("0.64"),
            post=lambda payload, url, **k: posts.append(payload),
        )
    )

    assert result.posted
    assert len(posts) == 1
    assert len(store.saves) == 1
