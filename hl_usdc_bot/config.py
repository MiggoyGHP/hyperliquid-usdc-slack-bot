"""Configuration, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


# Settings the Cloudflare Worker reads off its `env` bindings object.
BINDING_KEYS = (
    "SLACK_WEBHOOK_URL",
    "DRY_RUN",
    "HEARTBEAT_HOURS",
    "ESCALATED_INTERVAL_HOURS",
    "ESCALATE_AT",
    "TOKEN_INDEX",
    "DISPLAY_TIMEZONE",
    "DISPLAY_UTC_OFFSET_HOURS",
    "STATE_OBJECT",
)


class MissingConfig(RuntimeError):
    """A required setting was not provided."""


@dataclass(frozen=True)
class Config:
    slack_webhook_url: str

    # Post at most this often while the market is quiet...
    heartbeat_hours: int = 1
    # ...and this often once utilization reaches escalate_at.
    escalated_interval_hours: int = 1
    escalate_at: Decimal = Decimal("0.79")

    token_index: int = 0
    display_timezone: str = "Asia/Manila"
    # Used only when the runtime has no IANA database (Cloudflare Workers).
    display_utc_offset_hours: int = 8

    # Where state lives: a GCS bucket in production, a local file for development.
    state_bucket: str | None = None
    state_object: str = "state.json"
    state_file: str = "state.json"

    dry_run: bool = False

    @classmethod
    def from_bindings(cls, env) -> Config:
        """Build from Cloudflare Worker bindings.

        Deliberately routed through an explicit mapping so the Worker never
        touches os.environ or a .env file - neither exists there.
        """
        return cls.from_env(_bindings_to_mapping(env))

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        dotenv_path: str | Path = ".env",
    ) -> Config:
        """Build config from the environment, or from an explicit mapping.

        Passing `env` skips .env entirely, so a developer's real credentials
        can never leak into a test run. Real environment variables always win
        over the file - Cloud Run injects the webhook from Secret Manager and
        a stray .env must not shadow it.
        """
        if env is None:
            source = {**_read_dotenv(dotenv_path), **os.environ}
        else:
            source = env
        dry_run = _flag(source.get("DRY_RUN"))

        webhook = source.get("SLACK_WEBHOOK_URL", "")
        if not webhook and not dry_run:
            raise MissingConfig(
                "SLACK_WEBHOOK_URL is required. Set DRY_RUN=1 to print the payload instead."
            )

        return cls(
            slack_webhook_url=webhook,
            heartbeat_hours=int(source.get("HEARTBEAT_HOURS", "1")),
            escalated_interval_hours=int(source.get("ESCALATED_INTERVAL_HOURS", "1")),
            escalate_at=Decimal(source.get("ESCALATE_AT", "0.79")),
            token_index=int(source.get("TOKEN_INDEX", "0")),
            display_timezone=source.get("DISPLAY_TIMEZONE", "Asia/Manila"),
            display_utc_offset_hours=int(source.get("DISPLAY_UTC_OFFSET_HOURS", "8")),
            state_bucket=source.get("STATE_BUCKET") or None,
            state_object=source.get("STATE_OBJECT", "state.json"),
            state_file=source.get("STATE_FILE", "state.json"),
            dry_run=dry_run,
        )


def _bindings_to_mapping(env) -> dict[str, str]:
    """Cloudflare exposes bindings as attributes, and vars may not be strings."""
    mapping = {}
    for key in BINDING_KEYS:
        value = getattr(env, key, None)
        if value is not None:
            mapping[key] = str(value)
    return mapping


def _flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


_QUOTE_CHARS = '"\''


def _read_dotenv(path: str | Path) -> dict[str, str]:
    """Parse a local KEY=VALUE file. Absent file means no local overrides."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return {}

    values: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip(_QUOTE_CHARS)
    return values
