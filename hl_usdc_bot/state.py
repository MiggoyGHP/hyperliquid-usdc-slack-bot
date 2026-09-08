"""What the bot remembers between ticks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from hl_usdc_bot.bands import Band

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class BotState:
    last_post_ts: datetime | None
    last_utilization: Decimal | None
    last_band: Band | None

    @classmethod
    def empty(cls) -> BotState:
        return cls(last_post_ts=None, last_utilization=None, last_band=None)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            # `is not None`, not truthiness: Band.NORMAL is 0 and a utilization
            # of 0 is a real reading - both are falsy.
            "last_post_ts": (
                self.last_post_ts.isoformat() if self.last_post_ts is not None else None
            ),
            "last_utilization": (
                str(self.last_utilization) if self.last_utilization is not None else None
            ),
            "last_band": self.last_band.name if self.last_band is not None else None,
        }

    @classmethod
    def from_dict(cls, raw: object) -> BotState:
        """Rebuild from stored JSON, degrading to empty() on anything unusable.

        A corrupt state file must not wedge the bot - losing one heartbeat's
        continuity is far better than never posting again.
        """
        if not isinstance(raw, dict):
            return cls.empty()

        return cls(
            last_post_ts=_parse_ts(raw.get("last_post_ts")),
            last_utilization=_parse_decimal(raw.get("last_utilization")),
            last_band=_parse_band(raw.get("last_band")),
        )


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _parse_band(value: object) -> Band | None:
    if not isinstance(value, str):
        return None
    try:
        return Band[value]
    except KeyError:
        return None


class LocalFileStateStore:
    """State in a JSON file. The development loop, and a fallback off Cloud Run."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> BotState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return BotState.empty()
        return BotState.from_dict(raw)

    def save(self, state: BotState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")


class GcsStateStore:
    """State as a single JSON object in a Cloud Storage bucket.

    A missing object means first run. Any other failure is allowed to
    propagate: treating a transient GCS error as "first run" would post a
    spurious heartbeat and throw away the band we were tracking.
    """

    def __init__(self, bucket: str, object_name: str = "state.json", client=None):
        # Imported here, not at module scope: the Cloudflare Worker imports
        # this module and has no google-cloud-storage available.
        from google.api_core.exceptions import NotFound

        self._not_found = NotFound
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._blob = client.bucket(bucket).blob(object_name)

    def load(self) -> BotState:
        try:
            raw = self._blob.download_as_text()
        except self._not_found:
            return BotState.empty()
        try:
            return BotState.from_dict(json.loads(raw))
        except json.JSONDecodeError:
            return BotState.empty()

    def save(self, state: BotState) -> None:
        self._blob.upload_from_string(
            json.dumps(state.to_dict(), indent=2), content_type="application/json"
        )


class KvStateStore:
    """State as a single JSON value in a Cloudflare KV namespace.

    The KV binding is async, so both methods are coroutines. `run_tick_async`
    awaits whatever the store returns, so this and the sync stores are
    interchangeable.
    """

    def __init__(self, kv, key: str = "state.json"):
        self._kv = kv
        self._key = key

    async def load(self) -> BotState:
        raw = await self._kv.get(self._key)
        if raw is None:
            return BotState.empty()
        try:
            return BotState.from_dict(json.loads(raw))
        except json.JSONDecodeError:
            return BotState.empty()

    async def save(self, state: BotState) -> None:
        await self._kv.put(self._key, json.dumps(state.to_dict()))


def build_store(config) -> LocalFileStateStore | GcsStateStore:
    """Pick the store implied by configuration."""
    if config.state_bucket:
        return GcsStateStore(config.state_bucket, config.state_object)
    return LocalFileStateStore(config.state_file)
