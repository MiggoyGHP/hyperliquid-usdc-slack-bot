"""Rendering a decision as a Slack Block Kit message, and delivering it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from decimal import ROUND_HALF_UP, Decimal

from hl_usdc_bot.bands import Band
from hl_usdc_bot.config import Config
from hl_usdc_bot.decide import Decision, Reason
from hl_usdc_bot.hyperliquid import ReserveState
from hl_usdc_bot.rates import KINK, fmt_pct, fmt_usd, headroom_to_kink

DEFAULT_TIMEOUT_SECONDS = 10

COLORS = {
    Band.NORMAL: "#2eb886",
    Band.HIGH: "#daa038",
    Band.CRITICAL: "#c93c37",
}

_CROSSED = {
    Band.HIGH: ":warning: *Crossed 80%* — borrow rate is now climbing above the 5% floor.",
    Band.CRITICAL: ":rotating_light: *Crossed 90%* — liquidity is thin, withdrawals may be constrained.",
}

_RECOVERED = {
    Band.NORMAL: ":white_check_mark: *Recovered below 80%* — borrow rate back at the 5% floor.",
    Band.HIGH: ":white_check_mark: *Recovered below 90%* — liquidity pressure easing.",
}


def resolve_display_tz(name: str, fallback_offset_hours: int = 8):
    """Timezone for rendering timestamps.

    Pyodide (Cloudflare Workers) may ship without the IANA database, so an
    unavailable zone degrades to a fixed offset rather than failing the tick.
    The Philippines observes no DST, so a fixed +08:00 is exact there.
    """
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=fallback_offset_hours))


class SlackPostFailed(RuntimeError):
    """Slack rejected the webhook post."""


def build_message(
    decision: Decision,
    reading: ReserveState,
    now: datetime,
    config: Config,
) -> dict:
    """The full webhook body for this decision."""
    utilization = fmt_pct(reading.utilization)
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"USDC utilization: {utilization}"},
        }
    ]

    banner = _banner(decision)
    if banner:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": banner}})

    blocks.append({"type": "section", "fields": _fields(decision, reading)})

    stamp = now.astimezone(
        resolve_display_tz(config.display_timezone, config.display_utc_offset_hours)
    )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Hyperliquid native USDC market  |  "
                        f"{stamp.strftime('%b %d, %Y %H:%M')} ({config.display_timezone})"
                    ),
                }
            ],
        }
    )

    return {
        "text": f"Hyperliquid USDC utilization {utilization} ({decision.band.name})",
        "attachments": [{"color": COLORS[decision.band], "blocks": blocks}],
    }


def _banner(decision: Decision) -> str | None:
    if decision.reason is Reason.BAND_UPGRADE:
        return _CROSSED.get(decision.band)
    if decision.reason is Reason.BAND_DOWNGRADE:
        return _RECOVERED.get(decision.band)
    return None


def _fields(decision: Decision, reading: ReserveState) -> list[dict]:
    fields = []

    if decision.delta_pp is not None:
        moved = decision.delta_pp.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        fields.append(_field("Δ since last post", f"{moved:+} pp"))

    fields.append(_field("Borrow APY", fmt_pct(reading.borrow_yearly_rate)))
    fields.append(_field("Supply APY", fmt_pct(reading.supply_yearly_rate)))
    fields.append(_field("Supplied", fmt_usd(reading.total_supplied)))
    fields.append(_field("Borrowed", fmt_usd(reading.total_borrowed)))
    fields.append(_field("Available", fmt_usd(reading.available)))

    headroom = headroom_to_kink(reading.total_supplied, reading.total_borrowed)
    if headroom > 0:
        fields.append(_field("Headroom to 80%", fmt_usd(headroom)))
    else:
        fields.append(_field("Past kink by", fmt_usd(-headroom)))

    return fields


def _field(label: str, value: str) -> dict:
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def post_webhook(
    payload: dict,
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session=None,
) -> None:
    """Deliver the payload, raising if Slack does not accept it.

    CPython only - the Worker posts through the runtime fetch instead, so
    `requests` is imported lazily to keep this module importable on Pyodide.
    """
    if session is None:
        import requests as session
    response = session.post(url, json=payload, timeout=timeout)
    if response.status_code != 200:
        raise SlackPostFailed(f"Slack returned {response.status_code}: {response.text}")
