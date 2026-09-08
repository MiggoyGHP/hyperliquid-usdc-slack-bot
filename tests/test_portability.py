"""Constraints the Cloudflare Workers runtime imposes on the shared modules.

Workers run on Pyodide: no `requests`, no `google-cloud-storage`, and no
guarantee that the IANA timezone database is present.
"""

import subprocess
import sys
from datetime import datetime, timedelta

from hl_usdc_bot.slack import resolve_display_tz

BLOCK_AND_IMPORT = """
import sys

class Blocker:
    def find_module(self, name, path=None):
        if name.split(".")[0] in {"google", "requests", "flask", "tzdata"}:
            raise ImportError(f"blocked: {name}")
        return None

sys.meta_path.insert(0, Blocker())

from hl_usdc_bot.state import BotState, LocalFileStateStore
from hl_usdc_bot.decide import decide
from hl_usdc_bot.bands import Band, classify
from hl_usdc_bot.rates import borrow_apy_model
from hl_usdc_bot.hyperliquid import parse_reserve_state
from hl_usdc_bot.slack import build_message
from hl_usdc_bot.runner import run_tick_async
print("OK")
"""


def test_the_worker_code_path_imports_without_requests_or_google():
    result = subprocess.run(
        [sys.executable, "-c", BLOCK_AND_IMPORT],
        capture_output=True,
        text=True,
        cwd=".",
    )

    assert "OK" in result.stdout, result.stderr


def test_a_known_zone_resolves_to_its_real_offset():
    tz = resolve_display_tz("Asia/Manila", fallback_offset_hours=8)

    assert tz.utcoffset(datetime(2026, 9, 8)) == timedelta(hours=8)


def test_an_unavailable_zone_database_falls_back_to_a_fixed_offset():
    # Pyodide may ship without tzdata; the bot must still stamp a timestamp
    # rather than crash the whole tick.
    tz = resolve_display_tz("Not/ARealZone", fallback_offset_hours=8)

    assert tz.utcoffset(datetime(2026, 9, 8)) == timedelta(hours=8)
