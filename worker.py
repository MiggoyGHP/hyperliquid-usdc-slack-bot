"""Cloudflare Workers entrypoint (Python Workers, open beta).

Deliberately thin. Everything with logic in it - decide, bands, rates, the
message builder, the state codec, the tick orchestration - lives in
`hl_usdc_bot` and is covered by the test suite. This file only adapts the
Workers runtime to those interfaces, because it cannot be imported by CPython
(`workers`, `js` and `pyodide` exist only inside the runtime).

Triggered by a Cron Trigger; see wrangler.jsonc.
"""

import json

from js import Object
from js import fetch as js_fetch
from pyodide.ffi import to_js as _to_js
from workers import Response, WorkerEntrypoint

from hl_usdc_bot.config import Config
from hl_usdc_bot.hyperliquid import API_URL, parse_reserve_state, parse_spot_prices
from hl_usdc_bot.runner import run_tick_async
from hl_usdc_bot.slack import SlackPostFailed
from hl_usdc_bot.state import KvStateStore


def _js(obj):
    """Python dict -> JS object, as fetch's init argument requires."""
    return _to_js(obj, dict_converter=Object.fromEntries)


async def _post_json(url, payload):
    return await js_fetch(
        url,
        _js(
            {
                "method": "POST",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload),
            }
        ),
    )


async def fetch_reserve(token=0):
    """Stands in for hyperliquid.fetch_reserve_state; `requests` has no Pyodide build."""
    response = await _post_json(API_URL, {"type": "borrowLendReserveState", "token": token})
    if response.status != 200:
        raise RuntimeError(f"Hyperliquid returned {response.status}")
    return parse_reserve_state(json.loads(await response.text()))


async def fetch_prices():
    """Stands in for hyperliquid.fetch_spot_prices, same reason as above."""
    response = await _post_json(API_URL, {"type": "spotMetaAndAssetCtxs"})
    if response.status != 200:
        raise RuntimeError(f"Hyperliquid returned {response.status}")
    return parse_spot_prices(json.loads(await response.text()))


async def post_webhook(payload, url, **_):
    """Stands in for slack.post_webhook, raising the same exception type."""
    response = await _post_json(url, payload)
    if response.status != 200:
        raise SlackPostFailed(f"Slack returned {response.status}: {await response.text()}")


async def tick(env):
    config = Config.from_bindings(env)
    store = KvStateStore(env.HL_BOT_STATE, key=config.state_object)
    return await run_tick_async(
        config,
        store,
        fetch_reserve=fetch_reserve,
        fetch_prices=fetch_prices,
        post=post_webhook,
    )


class Default(WorkerEntrypoint):
    async def scheduled(self, controller=None, env=None, ctx=None):
        result = await tick(env or self.env)
        print(
            f"utilization={result.utilization} band={result.band.name} "
            f"reason={result.reason.name} post={result.posted}"
        )

    async def fetch(self, request):
        """Status only - deliberately does NOT run a tick.

        An HTTP endpoint that posted to Slack would let anyone who found the
        URL spam the channel. Use `pywrangler dev` and the /__scheduled route
        to exercise the cron path locally instead.
        """
        return Response("hl-usdc-bot: alive, driven by cron trigger")
