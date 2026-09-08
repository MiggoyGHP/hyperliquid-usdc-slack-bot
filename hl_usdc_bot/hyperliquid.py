"""Reading the USDC reserve from the Hyperliquid info endpoint.

    POST https://api.hyperliquid.xyz/info
    {"type": "borrowLendReserveState", "token": 0}

Token 0 is USDC. Every numeric field comes back as a string, so parsing goes
straight to Decimal without a float in between.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests

API_URL = "https://api.hyperliquid.xyz/info"
USDC_TOKEN_INDEX = 0
DEFAULT_TIMEOUT_SECONDS = 10


class MalformedReserveState(ValueError):
    """The endpoint returned something we cannot trust as a reserve state."""


@dataclass(frozen=True)
class ReserveState:
    utilization: Decimal
    borrow_yearly_rate: Decimal
    supply_yearly_rate: Decimal
    total_supplied: Decimal
    total_borrowed: Decimal
    available: Decimal


_FIELDS = {
    "utilization": "utilization",
    "borrow_yearly_rate": "borrowYearlyRate",
    "supply_yearly_rate": "supplyYearlyRate",
    "total_supplied": "totalSupplied",
    "total_borrowed": "totalBorrowed",
    "available": "balance",
}


def parse_reserve_state(payload: object) -> ReserveState:
    """Validate and convert a raw info-endpoint payload."""
    if not isinstance(payload, Mapping):
        raise MalformedReserveState(f"expected a JSON object, got {type(payload).__name__}")

    values = {}
    for attr, key in _FIELDS.items():
        if key not in payload:
            raise MalformedReserveState(f"missing field {key!r}")
        try:
            values[attr] = Decimal(str(payload[key]))
        except (InvalidOperation, TypeError) as exc:
            raise MalformedReserveState(f"field {key!r} is not numeric: {payload[key]!r}") from exc

    return ReserveState(**values)


def fetch_reserve_state(
    token: int = USDC_TOKEN_INDEX,
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> ReserveState:
    """Fetch and parse the live reserve state for a token."""
    http = session or requests
    response = http.post(
        API_URL,
        json={"type": "borrowLendReserveState", "token": token},
        timeout=timeout,
    )
    response.raise_for_status()
    return parse_reserve_state(response.json())
