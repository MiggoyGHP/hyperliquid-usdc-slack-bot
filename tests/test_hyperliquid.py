from decimal import Decimal

import pytest

from hl_usdc_bot.hyperliquid import MalformedReserveState, parse_reserve_state

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
