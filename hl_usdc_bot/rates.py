"""Rate maths and human formatting for the USDC reserve.

The borrow curve is published by Hyperliquid as:

    borrow_apy = 0.05 + 4.75 * max(0, utilization - 0.80)

so the rate is pinned at the 5% floor until utilization reaches the kink at
80%, then climbs steeply. Everything here is Decimal in, Decimal out.
"""

from decimal import ROUND_HALF_UP, Decimal

KINK = Decimal("0.80")
BASE_APY = Decimal("0.05")
SLOPE = Decimal("4.75")

_CENTS = Decimal("0.01")


def borrow_apy_model(utilization: Decimal) -> Decimal:
    """Borrow APY implied by the published curve at this utilization."""
    excess = max(Decimal("0"), utilization - KINK)
    return BASE_APY + SLOPE * excess


def headroom_to_kink(total_supplied: Decimal, total_borrowed: Decimal) -> Decimal:
    """USDC that must still be borrowed before the rate starts climbing.

    Negative once utilization is already past the kink.
    """
    return KINK * total_supplied - total_borrowed


def fmt_pct(fraction: Decimal) -> str:
    """0.6410374822 -> '64.10%'."""
    return f"{(fraction * 100).quantize(_CENTS, rounding=ROUND_HALF_UP)}%"


def fmt_usd(amount: Decimal) -> str:
    """395290801.94 -> '$395.29M'. Keeps the sign outside the dollar symbol."""
    sign = "-" if amount < 0 else ""
    magnitude = abs(amount)

    for threshold, suffix in (
        (Decimal("1e9"), "B"),
        (Decimal("1e6"), "M"),
        (Decimal("1e3"), "K"),
    ):
        if magnitude >= threshold:
            scaled = (magnitude / threshold).quantize(_CENTS, rounding=ROUND_HALF_UP)
            return f"{sign}${scaled}{suffix}"

    return f"{sign}${magnitude.quantize(_CENTS, rounding=ROUND_HALF_UP)}"


def fmt_price(amount: Decimal) -> str:
    """2503.65 -> '$2,503.65'. Comma-grouped and always to the cent.

    fmt_usd abbreviates, which is right for a pool balance and nonsense for a
    price: it would render Bitcoin at '$79.06K'.
    """
    return f"${amount.quantize(_CENTS, rounding=ROUND_HALF_UP):,f}"
