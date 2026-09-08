import json
from datetime import UTC, datetime
from decimal import Decimal

from google.api_core.exceptions import NotFound

from hl_usdc_bot.bands import Band
from hl_usdc_bot.state import BotState, GcsStateStore, LocalFileStateStore

SAMPLE = BotState(
    last_post_ts=datetime(2026, 9, 4, 6, 0, 11, tzinfo=UTC),
    last_utilization=Decimal("0.6410374822"),
    last_band=Band.NORMAL,
)


def test_round_trips_through_json_without_losing_precision():
    restored = BotState.from_dict(json.loads(json.dumps(SAMPLE.to_dict())))

    assert restored == SAMPLE


def test_a_corrupt_state_document_degrades_to_first_run_rather_than_crashing():
    assert BotState.from_dict("not a dict") == BotState.empty()


def test_an_unknown_band_name_degrades_to_first_run():
    restored = BotState.from_dict({"last_band": "APOCALYPSE", "last_post_ts": None})

    assert restored.last_band is None


def test_a_naive_timestamp_is_assumed_to_be_utc():
    restored = BotState.from_dict({"last_post_ts": "2026-09-04T06:00:11"})

    assert restored.last_post_ts == datetime(2026, 9, 4, 6, 0, 11, tzinfo=UTC)


def test_local_store_reports_first_run_when_no_file_exists(tmp_path):
    store = LocalFileStateStore(tmp_path / "absent.json")

    assert store.load() == BotState.empty()


def test_local_store_round_trips(tmp_path):
    store = LocalFileStateStore(tmp_path / "state.json")

    store.save(SAMPLE)

    assert store.load() == SAMPLE


def test_local_store_survives_a_truncated_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")

    assert LocalFileStateStore(path).load() == BotState.empty()


class FakeBlob:
    def __init__(self, content=None):
        self.content = content

    def download_as_text(self):
        if self.content is None:
            raise NotFound("no such object")
        return self.content

    def upload_from_string(self, data, content_type=None):
        self.content = data


class FakeBucket:
    def __init__(self, blob):
        self._blob = blob

    def blob(self, name):
        return self._blob


class FakeGcsClient:
    def __init__(self, blob):
        self._bucket = FakeBucket(blob)

    def bucket(self, name):
        return self._bucket


def test_gcs_store_reports_first_run_when_the_object_is_absent():
    store = GcsStateStore("bucket", "state.json", client=FakeGcsClient(FakeBlob()))

    assert store.load() == BotState.empty()


def test_gcs_store_round_trips():
    blob = FakeBlob()
    store = GcsStateStore("bucket", "state.json", client=FakeGcsClient(blob))

    store.save(SAMPLE)

    assert store.load() == SAMPLE


def test_gcs_store_writes_json():
    blob = FakeBlob()
    store = GcsStateStore("bucket", "state.json", client=FakeGcsClient(blob))

    store.save(SAMPLE)

    assert json.loads(blob.content)["last_band"] == "NORMAL"


class FakeKv:
    """Stands in for a Cloudflare KV binding, whose methods are async."""

    def __init__(self, value=None):
        self.value = value

    async def get(self, key):
        return self.value

    async def put(self, key, value):
        self.value = value


def test_kv_store_reports_first_run_when_the_key_is_absent():
    import asyncio

    from hl_usdc_bot.state import KvStateStore

    store = KvStateStore(FakeKv(None))

    assert asyncio.run(store.load()) == BotState.empty()


def test_kv_store_round_trips():
    import asyncio

    from hl_usdc_bot.state import KvStateStore

    kv = FakeKv()
    store = KvStateStore(kv)

    asyncio.run(store.save(SAMPLE))

    assert asyncio.run(store.load()) == SAMPLE


def test_kv_store_survives_a_corrupt_value():
    import asyncio

    from hl_usdc_bot.state import KvStateStore

    store = KvStateStore(FakeKv("{not json"))

    assert asyncio.run(store.load()) == BotState.empty()
