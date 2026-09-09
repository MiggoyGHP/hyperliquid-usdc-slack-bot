"""Reading the USDC reserve and the spot majors from the Hyperliquid info endpoint.

    POST https://api.hyperliquid.xyz/info
    {"type": "borrowLendReserveState", "token": 0}   -- the lending reserve
    {"type": "spotMetaAndAssetCtxs"}                 -- every spot market

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

# The markets the Hyperliquid UI labels BTC/USDC and ETH/USDC, named by the
# tokens they trade rather than by their '@142'/'@151' pair indices. UBTC and
# UETH are the Unit-bridged majors; there is no plainly-named BTC or ETH spot
# token on the venue.
SPOT_PAIRS = {"btc": ("UBTC", "USDC"), "eth": ("UETH", "USDC")}


class MalformedReserveState(ValueError):
    """The endpoint returned something we cannot trust as a reserve state."""


class MalformedSpotPayload(ValueError):
    """The spot endpoint returned something other than a [meta, contexts] pair."""


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


@dataclass(frozen=True)
class SpotPrices:
    """Spot mids for the majors, as captured alongside a reserve reading.

    Either leg may be None: the prices are context on a utilization alert, so an
    unresolvable market costs one field rather than the whole message.
    """

    btc: Decimal | None = None
    eth: Decimal | None = None


def parse_spot_prices(payload: object) -> SpotPrices:
    """Pull the BTC and ETH spot mids out of a spotMetaAndAssetCtxs response.

    Pairs are matched on their token names, never on the '@142'/'@151' indices
    the response happens to use today. An index that quietly came to mean a
    different market would put a plausible but wrong number in the alert -- and
    UBTC/USDH, a stale pair quoting the same asset, sits eight rows away. Name
    resolution turns that into a missing field, which is visible.
    """
    if not isinstance(payload, (list, tuple)) or len(payload) != 2:
        raise MalformedSpotPayload(f"expected a [meta, contexts] pair, got {payload!r:.60}")

    meta, contexts = payload
    if not isinstance(meta, Mapping) or not isinstance(contexts, (list, tuple)):
        raise MalformedSpotPayload("expected a [meta, contexts] pair")

    token_names = {
        token["index"]: token["name"]
        for token in meta.get("tokens", [])
        if isinstance(token, Mapping) and "index" in token and "name" in token
    }
    pair_names = {}
    for entry in meta.get("universe", []):
        if not isinstance(entry, Mapping):
            continue
        tokens = tuple(token_names.get(i) for i in entry.get("tokens", []))
        pair_names[tokens] = entry.get("name")

    quotes = {
        quote["coin"]: quote
        for quote in contexts
        if isinstance(quote, Mapping) and "coin" in quote
    }

    return SpotPrices(
        **{
            leg: _quoted_price(quotes.get(pair_names.get(pair)))
            for leg, pair in SPOT_PAIRS.items()
        }
    )


def _quoted_price(quote: object) -> Decimal | None:
    """The mid, or the mark when a pair is too thin to have a mid quoted."""
    if not isinstance(quote, Mapping):
        return None
    for key in ("midPx", "markPx"):
        raw = quote.get(key)
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            continue
    return None


def fetch_spot_prices(
    *,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    session: requests.Session | None = None,
) -> SpotPrices:
    """Fetch and parse the live spot mids for the majors."""
    http = session or requests
    response = http.post(API_URL, json={"type": "spotMetaAndAssetCtxs"}, timeout=timeout)
    response.raise_for_status()
    return parse_spot_prices(response.json())
