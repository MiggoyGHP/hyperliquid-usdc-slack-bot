from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hl_usdc_bot.bands import Band
from hl_usdc_bot.config import Config
from hl_usdc_bot.decide import decide
from hl_usdc_bot.slack import SlackPostFailed, build_message, post_webhook
from hl_usdc_bot.state import BotState
from tests.test_decide import CONFIG, NOW, posted, reading


def all_text(payload) -> str:
    """Every string anywhere in the payload, for substring assertions."""
    if isinstance(payload, dict):
        return " ".join(all_text(v) for v in payload.values())
    if isinstance(payload, list):
        return " ".join(all_text(v) for v in payload)
    return str(payload)


def message_for(utilization: str, previous: BotState, now=NOW, config=CONFIG):
    read = reading(utilization)
    return build_message(decide(read, previous, now, config), read, now, config)


def test_headline_carries_the_utilization():
    payload = message_for("0.6410374822", BotState.empty())

    assert "64.10%" in all_text(payload)


def test_normal_band_is_green():
    payload = message_for("0.64", BotState.empty())

    assert payload["attachments"][0]["color"] == "#2eb886"


def test_high_band_is_amber():
    payload = message_for("0.82", posted(timedelta(minutes=1), "0.78", Band.NORMAL))

    assert payload["attachments"][0]["color"] == "#daa038"


def test_critical_band_is_red():
    payload = message_for("0.95", posted(timedelta(minutes=1), "0.85", Band.HIGH))

    assert payload["attachments"][0]["color"] == "#c93c37"


def test_first_run_omits_the_delta():
    payload = message_for("0.64", BotState.empty())

    assert "Δ" not in all_text(payload)


def test_subsequent_run_shows_a_signed_delta():
    payload = message_for("0.6510", posted(timedelta(hours=6), "0.6410", Band.NORMAL))

    assert "+1.00 pp" in all_text(payload)


def test_a_falling_delta_keeps_its_minus_sign():
    payload = message_for("0.6310", posted(timedelta(hours=6), "0.6410", Band.NORMAL))

    assert "-1.00 pp" in all_text(payload)


def test_normal_band_reports_headroom_to_the_kink():
    # 0.80 * 400M - 0.64 * 400M == 64M
    payload = message_for("0.64", BotState.empty())

    text = all_text(payload)
    assert "Headroom to 80%" in text
    assert "$64.00M" in text


def test_past_the_kink_reports_the_excess_instead_of_headroom():
    payload = message_for("0.85", posted(timedelta(minutes=1), "0.78", Band.NORMAL))

    text = all_text(payload)
    assert "Headroom to 80%" not in text
    assert "Past kink by" in text


def test_crossing_the_kink_is_announced():
    payload = message_for("0.801", posted(timedelta(minutes=1), "0.78", Band.NORMAL))

    assert "Crossed 80%" in all_text(payload)


def test_recovering_is_announced():
    payload = message_for("0.78", posted(timedelta(minutes=1), "0.81", Band.HIGH))

    assert "Recovered" in all_text(payload)


def test_a_routine_heartbeat_has_no_crossing_banner():
    payload = message_for("0.64", posted(timedelta(hours=6), "0.63", Band.NORMAL))

    text = all_text(payload)
    assert "Crossed" not in text
    assert "Recovered" not in text


def test_timestamp_names_the_zone_explicitly_rather_than_an_ambiguous_abbreviation():
    payload = message_for("0.64", BotState.empty())

    text = all_text(payload)
    # 12:00 UTC is 20:00 in Manila. "PST" would collide with US Pacific.
    assert "20:00" in text
    assert "Asia/Manila" in text
    assert "PST" not in text


def test_fallback_text_is_present_for_notifications():
    payload = message_for("0.6410374822", BotState.empty())

    assert "64.10%" in payload["text"]


def test_supply_and_borrow_totals_are_reported():
    payload = message_for("0.64", BotState.empty())

    text = all_text(payload)
    assert "$400.00M" in text  # supplied
    assert "$256.00M" in text  # borrowed


class FakeSession:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return FakeResponse(self.status_code)


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "ok" if status_code == 200 else "invalid_payload"


def test_post_webhook_sends_the_payload_to_the_configured_url():
    session = FakeSession()

    post_webhook({"text": "hello"}, "https://hooks.slack.example/abc", session=session)

    url, body, _ = session.calls[0]
    assert url == "https://hooks.slack.example/abc"
    assert body == {"text": "hello"}


def test_post_webhook_raises_on_a_rejected_payload():
    session = FakeSession(status_code=400)

    with pytest.raises(SlackPostFailed):
        post_webhook({"text": "hello"}, "https://hooks.slack.example/abc", session=session)
