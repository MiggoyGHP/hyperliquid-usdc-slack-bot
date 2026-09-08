from decimal import Decimal

import pytest

from hl_usdc_bot.config import Config, MissingConfig

WEBHOOK = "https://hooks.slack.example/T0/B0/secret"


def test_a_missing_webhook_fails_fast():
    with pytest.raises(MissingConfig):
        Config.from_env({})


def test_dry_run_does_not_need_a_webhook():
    config = Config.from_env({"DRY_RUN": "1"})

    assert config.dry_run
    assert config.slack_webhook_url == ""


def test_cadence_settings_are_typed():
    config = Config.from_env(
        {"SLACK_WEBHOOK_URL": WEBHOOK, "HEARTBEAT_HOURS": "12", "ESCALATE_AT": "0.85"}
    )

    assert config.heartbeat_hours == 12
    assert config.escalate_at == Decimal("0.85")


def test_an_explicit_env_mapping_ignores_any_dotenv_file(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SLACK_WEBHOOK_URL=https://leaked.example/should-not-be-used\n")
    monkeypatch.chdir(tmp_path)

    # Tests pass explicit mappings, so a developer's real .env can never leak
    # into a test run.
    with pytest.raises(MissingConfig):
        Config.from_env({})


def test_the_real_environment_reads_a_dotenv_file(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(f"SLACK_WEBHOOK_URL={WEBHOOK}\nHEARTBEAT_HOURS=3\n")
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("HEARTBEAT_HOURS", raising=False)

    config = Config.from_env(dotenv_path=dotenv)

    assert config.slack_webhook_url == WEBHOOK
    assert config.heartbeat_hours == 3


def test_a_real_environment_variable_beats_the_dotenv_file(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("SLACK_WEBHOOK_URL=https://stale.example/old\n")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)

    config = Config.from_env(dotenv_path=dotenv)

    # Cloud Run injects the real thing; a stray .env must never shadow it.
    assert config.slack_webhook_url == WEBHOOK


def test_a_missing_dotenv_file_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)

    config = Config.from_env(dotenv_path=tmp_path / "absent.env")

    assert config.slack_webhook_url == WEBHOOK


class FakeBindings:
    """Cloudflare exposes bindings as attributes on env, not as a mapping."""

    def __init__(self, **values):
        for key, value in values.items():
            setattr(self, key, value)


def test_bindings_are_read_off_the_env_object():
    config = Config.from_bindings(
        FakeBindings(SLACK_WEBHOOK_URL=WEBHOOK, HEARTBEAT_HOURS="12", ESCALATE_AT="0.85")
    )

    assert config.slack_webhook_url == WEBHOOK
    assert config.heartbeat_hours == 12
    assert config.escalate_at == Decimal("0.85")


def test_absent_bindings_fall_back_to_defaults():
    config = Config.from_bindings(FakeBindings(SLACK_WEBHOOK_URL=WEBHOOK))

    # Hourly heartbeat is the default cadence.
    assert config.heartbeat_hours == 1
    assert config.escalate_at == Decimal("0.79")
    assert config.display_utc_offset_hours == 8


def test_bindings_never_consult_the_real_environment_or_a_dotenv_file(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_HOURS", "99")

    config = Config.from_bindings(FakeBindings(SLACK_WEBHOOK_URL=WEBHOOK))

    # The Worker has no os.environ worth reading; only bindings count.
    assert config.heartbeat_hours == 1


def test_non_string_binding_values_are_coerced():
    # Wrangler vars can arrive as JS numbers.
    config = Config.from_bindings(FakeBindings(SLACK_WEBHOOK_URL=WEBHOOK, HEARTBEAT_HOURS=12))

    assert config.heartbeat_hours == 12


def test_a_worker_without_a_webhook_binding_fails_fast():
    with pytest.raises(MissingConfig):
        Config.from_bindings(FakeBindings())
