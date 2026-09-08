"""Cloud Run entrypoint.

Deliberately thin: Cloud Scheduler POSTs /tick, this hands straight off to
run_tick. Authorisation is Cloud Run's job (deploy with
--no-allow-unauthenticated), not this module's.
"""

from __future__ import annotations

import logging

from flask import Flask, jsonify

from hl_usdc_bot.config import Config
from hl_usdc_bot.runner import run_tick
from hl_usdc_bot.state import build_store

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)


def create_app(config: Config | None = None, store=None, runner=run_tick) -> Flask:
    config = config or Config.from_env()
    store = store if store is not None else build_store(config)

    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.post("/tick")
    def tick():
        try:
            result = runner(config, store)
        except Exception as exc:  # noqa: BLE001 - surface as 500 so Scheduler retries
            log.exception("tick failed")
            return jsonify(error=str(exc)), 500

        return jsonify(
            posted=result.posted,
            reason=result.reason.name,
            band=result.band.name,
            utilization=str(result.utilization),
        )

    return app
