from decimal import Decimal

import pytest

from hl_usdc_bot.hyperliquid import (
    MalformedReserveState,
    MalformedSpotPayload,
    parse_reserve_state,
    parse_spot_prices,
)

# Captured verbatim from POST https://api.hyperliquid.xyz/info
# {"type":"borrowLendReserveState","token":0} on 2026-09-04.
REAL_PAYLOAD = {
    "borrowYearlyRate": "0.05",
    "supplyYearlyRate": "0.0288466867",
    "balance": "141894615.9698856771",
    "utilization": "0.6410374822",
    "oraclePx": "1.0",
    "ltv": "0.0",
    "totalSupplied": "395290801.9401631355",
    "totalBorrowed": "253396220.4155763984",
}


def test_parses_every_numeric_field_as_decimal():
    state = parse_reserve_state(REAL_PAYLOAD)

    assert state.utilization == Decimal("0.6410374822")
    assert state.borrow_yearly_rate == Decimal("0.05")
    assert state.supply_yearly_rate == Decimal("0.0288466867")
    assert state.total_supplied == Decimal("395290801.9401631355")
    assert state.total_borrowed == Decimal("253396220.4155763984")
    assert state.available == Decimal("141894615.9698856771")


def test_preserves_full_precision_rather_than_rounding_through_float():
    state = parse_reserve_state(REAL_PAYLOAD)

    # A float round-trip would lose these trailing digits.
    assert str(state.total_supplied) == "395290801.9401631355"


def test_rejects_payload_missing_a_required_field():
    incomplete = {k: v for k, v in REAL_PAYLOAD.items() if k != "utilization"}

    with pytest.raises(MalformedReserveState):
        parse_reserve_state(incomplete)


def test_rejects_non_numeric_field():
    with pytest.raises(MalformedReserveState):
        parse_reserve_state({**REAL_PAYLOAD, "utilization": "not-a-number"})


def test_rejects_non_mapping_payload():
    with pytest.raises(MalformedReserveState):
        parse_reserve_state(["unexpected", "list"])


# Captured verbatim from POST https://api.hyperliquid.xyz/info
# {"type":"spotMetaAndAssetCtxs"} on 2026-09-09, trimmed to the pairs that matter.
# @142 is what the Hyperliquid UI labels BTC/USDC and @151 what it labels ETH/USDC;
# the underlying tokens are the Unit-bridged UBTC and UETH.
REAL_SPOT_PAYLOAD = [
    {
        "tokens": [
            {"name": "USDC", "index": 0, "szDecimals": 8, "weiDecimals": 8, "fullName": None},
            {"name": "PURR", "index": 1, "szDecimals": 0, "weiDecimals": 5, "fullName": None},
            {"name": "UBTC", "index": 197, "szDecimals": 5, "weiDecimals": 10,
             "fullName": "Unit Bitcoin"},
            {"name": "UETH", "index": 221, "szDecimals": 4, "weiDecimals": 9,
             "fullName": "Unit Ethereum"},
            {"name": "USDH", "index": 360, "szDecimals": 2, "weiDecimals": 8, "fullName": "USDH"},
        ],
        "universe": [
            {"tokens": [1, 0], "name": "PURR/USDC", "index": 0, "isCanonical": True},
            {"tokens": [197, 0], "name": "@142", "index": 142, "isCanonical": False},
            {"tokens": [221, 0], "name": "@151", "index": 151, "isCanonical": False},
            {"tokens": [197, 360], "name": "@234", "index": 234, "isCanonical": False},
        ],
    },
    [
        {"coin": "PURR/USDC", "midPx": "0.119575", "markPx": "0.11928", "prevDayPx": "0.11337"},
        {"coin": "@142", "midPx": "79063.5", "markPx": "79064.0", "prevDayPx": "78383.0"},
        {"coin": "@151", "midPx": "2503.65", "markPx": "2503.7", "prevDayPx": "2480.7"},
        # A real pair with no volume: the mid is null, only the mark is quoted.
        {"coin": "@234", "midPx": None, "markPx": "76644.0", "prevDayPx": "76644.0"},
    ],
]


def without_universe_entry(name: str):
    """The same payload with one pair delisted, to prove resolution is by name."""
    meta, ctxs = REAL_SPOT_PAYLOAD
    trimmed = {**meta, "universe": [u for u in meta["universe"] if u["name"] != name]}
    return [trimmed, ctxs]


def test_resolves_btc_and_eth_from_their_token_pair():
    prices = parse_spot_prices(REAL_SPOT_PAYLOAD)

    assert prices.btc == Decimal("79063.5")
    assert prices.eth == Decimal("2503.65")


def test_spot_prices_keep_full_precision_rather_than_rounding_through_float():
    prices = parse_spot_prices(REAL_SPOT_PAYLOAD)

    assert str(prices.btc) == "79063.5"


def test_a_null_mid_falls_back_to_the_mark():
    meta, ctxs = REAL_SPOT_PAYLOAD
    thin = [{"coin": c["coin"], "midPx": None, "markPx": c["markPx"]} for c in ctxs]

    prices = parse_spot_prices([meta, thin])

    assert prices.btc == Decimal("79064.0")


def test_a_delisted_pair_yields_none_rather_than_another_pairs_price():
    # The failure this guards against is showing UBTC/USDH's stale 76644 as if it
    # were BTC/USDC, which resolving by index rather than by name would allow.
    prices = parse_spot_prices(without_universe_entry("@142"))

    assert prices.btc is None
    assert prices.eth == Decimal("2503.65")


def test_a_pair_with_no_quotable_price_yields_none():
    meta, _ = REAL_SPOT_PAYLOAD
    unquoted = [{"coin": "@142", "midPx": None, "markPx": None}]

    prices = parse_spot_prices([meta, unquoted])

    assert prices.btc is None


def test_a_non_numeric_price_yields_none_rather_than_a_wrong_number():
    meta, _ = REAL_SPOT_PAYLOAD
    broken = [{"coin": "@142", "midPx": "n/a", "markPx": "n/a"}]

    prices = parse_spot_prices([meta, broken])

    assert prices.btc is None


def test_rejects_a_payload_that_is_not_the_meta_and_contexts_pair():
    with pytest.raises(MalformedSpotPayload):
        parse_spot_prices({"universe": []})
