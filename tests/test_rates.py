from decimal import Decimal

from hl_usdc_bot.rates import borrow_apy_model, fmt_pct, fmt_usd, headroom_to_kink

# Hyperliquid docs: borrow_apy = 0.05 + 4.75 * max(0, utilization - 0.80)


def test_borrow_apy_is_pinned_at_the_floor_below_the_kink():
    assert borrow_apy_model(Decimal("0.6410374822")) == Decimal("0.05")


def test_borrow_apy_is_still_at_the_floor_exactly_at_the_kink():
    assert borrow_apy_model(Decimal("0.80")) == Decimal("0.05")


def test_borrow_apy_climbs_above_the_kink():
    # 0.05 + 4.75 * 0.10 == 0.525
    assert borrow_apy_model(Decimal("0.90")) == Decimal("0.525")


def test_headroom_is_borrow_capacity_remaining_before_the_kink():
    # 0.80 * 1000 - 500 == 300
    assert headroom_to_kink(Decimal("1000"), Decimal("500")) == Decimal("300")


def test_headroom_goes_negative_once_past_the_kink():
    assert headroom_to_kink(Decimal("1000"), Decimal("850")) == Decimal("-50")


def test_fmt_pct_renders_two_decimal_places():
    assert fmt_pct(Decimal("0.6410374822")) == "64.10%"


def test_fmt_pct_rounds_half_up():
    assert fmt_pct(Decimal("0.79995")) == "80.00%"


def test_fmt_usd_uses_millions_for_large_amounts():
    assert fmt_usd(Decimal("395290801.9401631355")) == "$395.29M"


def test_fmt_usd_uses_billions_past_a_thousand_million():
    assert fmt_usd(Decimal("1250000000")) == "$1.25B"


def test_fmt_usd_uses_thousands_for_small_amounts():
    assert fmt_usd(Decimal("12345")) == "$12.35K"


def test_fmt_usd_keeps_a_negative_sign():
    assert fmt_usd(Decimal("-50000000")) == "-$50.00M"
