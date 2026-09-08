from decimal import Decimal

import pytest

from hl_usdc_bot.app import create_app
from hl_usdc_bot.bands import Band
from hl_usdc_bot.decide import Reason
from hl_usdc_bot.runner import TickResult
from tests.test_decide import CONFIG


def client_for(runner):
    app = create_app(config=CONFIG, store=object(), runner=runner)
    app.config.update(TESTING=True)
    return app.test_client()


def a_post(**overrides):
    defaults = dict(
        posted=True,
        reason=Reason.HEARTBEAT,
        band=Band.NORMAL,
        utilization=Decimal("0.64"),
        payload={"text": "hi"},
    )
    return TickResult(**{**defaults, **overrides})


def test_healthz_is_ok_without_touching_the_market():
    calls = []

    def runner(*args, **kwargs):
        calls.append(1)
        return a_post()

    response = client_for(runner).get("/healthz")

    assert response.status_code == 200
    assert calls == []


def test_tick_runs_the_bot_and_reports_what_happened():
    response = client_for(lambda *a, **k: a_post()).post("/tick")

    assert response.status_code == 200
    assert response.get_json() == {
        "posted": True,
        "reason": "HEARTBEAT",
        "band": "NORMAL",
        "utilization": "0.64",
    }


def test_a_suppressed_tick_is_still_a_success():
    response = client_for(
        lambda *a, **k: a_post(posted=False, reason=Reason.SUPPRESSED, payload=None)
    ).post("/tick")

    assert response.status_code == 200
    assert response.get_json()["posted"] is False


def test_a_failing_tick_returns_500_so_scheduler_retries():
    def runner(*args, **kwargs):
        raise RuntimeError("hyperliquid unreachable")

    response = client_for(runner).post("/tick")

    assert response.status_code == 500
    assert "hyperliquid unreachable" in response.get_json()["error"]


def test_tick_rejects_get_so_a_crawler_cannot_trigger_a_post():
    response = client_for(lambda *a, **k: a_post()).get("/tick")

    assert response.status_code == 405
