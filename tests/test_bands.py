from decimal import Decimal

from hl_usdc_bot.bands import Band, classify


def test_low_utilization_is_normal():
    assert classify(Decimal("0.64"), prev=None) is Band.NORMAL


def test_crossing_the_kink_enters_high():
    assert classify(Decimal("0.801"), prev=Band.NORMAL) is Band.HIGH


def test_exactly_at_the_kink_enters_high():
    assert classify(Decimal("0.80"), prev=Band.NORMAL) is Band.HIGH


def test_ninety_percent_enters_critical():
    assert classify(Decimal("0.90"), prev=Band.HIGH) is Band.CRITICAL


def test_hysteresis_holds_high_within_the_buffer():
    # 0.795 is below the 0.80 entry but above the 0.785 exit.
    assert classify(Decimal("0.795"), prev=Band.HIGH) is Band.HIGH


def test_dropping_past_the_buffer_releases_high():
    assert classify(Decimal("0.784"), prev=Band.HIGH) is Band.NORMAL


def test_hysteresis_holds_critical_within_the_buffer():
    assert classify(Decimal("0.89"), prev=Band.CRITICAL) is Band.CRITICAL


def test_dropping_past_the_buffer_releases_critical_to_high():
    assert classify(Decimal("0.884"), prev=Band.CRITICAL) is Band.HIGH


def test_a_flapping_reading_does_not_change_band_repeatedly():
    band = classify(Decimal("0.799"), prev=None)
    assert band is Band.NORMAL

    band = classify(Decimal("0.801"), prev=band)
    assert band is Band.HIGH

    # Falls back under 0.80 but stays inside the buffer: no change, so no alert.
    band = classify(Decimal("0.795"), prev=band)
    assert band is Band.HIGH


def test_a_collapse_from_critical_can_skip_straight_to_normal():
    assert classify(Decimal("0.50"), prev=Band.CRITICAL) is Band.NORMAL
