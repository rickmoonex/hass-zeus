"""
Tibber GraphQL API client for Zeus.

This is Zeus's own Tibber client that fetches both the energy price
(what you receive/pay for grid export) and the total price (energy + tax,
what you pay for consumption). The official Tibber HA integration only
exposes the total price via its service call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import TIBBER_API_ENDPOINT

_LOGGER = logging.getLogger(__name__)

# GraphQL query: fetch viewer name (for validation) and all homes
QUERY_VIEWER_INFO = """{
  viewer {
    name
    homes {
      id
      appNickname
      address {
        address1
        city
      }
    }
  }
}"""

# GraphQL query: fetch quarter-hourly prices with both energy and total
QUERY_PRICES = """{
  viewer {
    homes {
      id
      appNickname
      currentSubscription {
        priceInfo(resolution: QUARTER_HOURLY) {
          current {
            energy
            tax
            total
            startsAt
            level
            currency
          }
          today {
            energy
            tax
            total
            startsAt
            level
          }
          tomorrow {
            energy
            tax
            total
            startsAt
            level
          }
        }
      }
    }
  }
}"""


@dataclass(frozen=True)
class TibberPriceEntry:
    """A single price entry from the Tibber API."""

    start_time: datetime
    energy: float  # Energy price only (export/feed-in price)
    tax: float
    total: float  # Energy + tax (consumption price)
    level: str  # VERY_CHEAP, CHEAP, NORMAL, EXPENSIVE, VERY_EXPENSIVE
    currency: str


@dataclass
class TibberHome:
    """Represents a Tibber home with its price data."""

    home_id: str
    name: str
    prices: list[TibberPriceEntry]


class TibberApiError(Exception):
    """Base exception for Tibber API errors."""


class TibberAuthError(TibberApiError):
    """Authentication failed."""


class TibberApiClient:
    """
    Lightweight Tibber GraphQL API client.

    Uses a personal access token (PAT) for authentication. Tokens can be
    created at https://developer.tibber.com/settings/access-token.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
    ) -> None:
        """Initialize the Tibber API client."""
        self._session = session
        self._access_token = access_token

    async def _execute(self, query: str) -> dict[str, Any]:
        """Execute a GraphQL query against the Tibber API."""
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": "Zeus-HA-Integration/0.1",
        }
        payload = {"query": query, "variables": {}}

        try:
            async with self._session.post(
                TIBBER_API_ENDPOINT,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:  # noqa: PLR2004
                    msg = "Invalid Tibber access token"
                    raise TibberAuthError(msg)

                if resp.content_type != "application/json":
                    msg = f"Unexpected content type: {resp.content_type}"
                    raise TibberApiError(msg)

                data = await resp.json()

                if resp.status != 200:  # noqa: PLR2004
                    errors = data.get("errors", [])
                    error_msg = (
                        errors[0].get("message", str(resp.status))
                        if errors
                        else str(resp.status)
                    )
                    if any(
                        e.get("extensions", {}).get("code") == "UNAUTHENTICATED"
                        for e in errors
                    ):
                        raise TibberAuthError(error_msg)
                    msg = f"Tibber API error: {error_msg}"
                    raise TibberApiError(msg)

                if "errors" in data:
                    errors = data["errors"]
                    if any(
                        e.get("extensions", {}).get("code") == "UNAUTHENTICATED"
                        for e in errors
                    ):
                        raise TibberAuthError(errors[0].get("message", "Auth error"))
                    error_msgs = [e.get("message", "Unknown") for e in errors]
                    msg = f"Tibber GraphQL errors: {', '.join(error_msgs)}"
                    raise TibberApiError(msg)

                return data.get("data", {})

        except aiohttp.ClientError as err:
            msg = f"Connection error to Tibber API: {err}"
            raise TibberApiError(msg) from err

    async def async_validate_token(self) -> str:
        """Validate the access token and return the viewer name."""
        data = await self._execute(QUERY_VIEWER_INFO)
        viewer = data.get("viewer")
        if not viewer:
            msg = "No viewer data returned from Tibber API"
            raise TibberApiError(msg)
        return viewer.get("name", "Tibber User")

    async def async_get_homes(self) -> list[dict[str, Any]]:
        """Get the list of homes from the Tibber API."""
        data = await self._execute(QUERY_VIEWER_INFO)
        viewer = data.get("viewer", {})
        return viewer.get("homes", [])

    async def async_get_prices(self) -> dict[str, TibberHome]:
        """
        Fetch quarter-hourly prices for all homes.

        Returns a dict mapping home name -> TibberHome with price data.
        Each price entry contains both the energy price (for export
        decisions) and the total price (for consumption scheduling).
        """
        data = await self._execute(QUERY_PRICES)
        viewer = data.get("viewer", {})
        homes = viewer.get("homes", [])

        result: dict[str, TibberHome] = {}
        for home in homes:
            home_id = home.get("id", "")
            home_name = home.get("appNickname") or home.get("id", "Unknown")
            subscription = home.get("currentSubscription")
            if not subscription:
                _LOGGER.debug("No subscription for home %s", home_name)
                continue

            price_info = subscription.get("priceInfo")
            if not price_info:
                _LOGGER.debug("No priceInfo for home %s", home_name)
                continue

            prices: list[TibberPriceEntry] = []
            currency = "EUR"

            # Parse current price (has currency field)
            current = price_info.get("current")
            if current:
                currency = current.get("currency", "EUR")
                entry = _parse_price_entry(current, currency)
                if entry:
                    prices.append(entry)

            # Parse today's prices
            for slot in price_info.get("today", []):
                entry = _parse_price_entry(slot, currency)
                if entry:
                    prices.append(entry)

            # Parse tomorrow's prices (available after ~13:00 CET)
            for slot in price_info.get("tomorrow", []):
                entry = _parse_price_entry(slot, currency)
                if entry:
                    prices.append(entry)

            # Deduplicate by start_time (current slot also appears in today)
            seen: set[datetime] = set()
            unique_prices: list[TibberPriceEntry] = []
            for p in prices:
                if p.start_time not in seen:
                    seen.add(p.start_time)
                    unique_prices.append(p)

            # Sort by start time
            unique_prices.sort(key=lambda p: p.start_time)

            result[home_name] = TibberHome(
                home_id=home_id,
                name=home_name,
                prices=unique_prices,
            )

            _LOGGER.debug(
                "Fetched %d price slots for home %s (currency=%s)",
                len(unique_prices),
                home_name,
                currency,
            )

        return result


def _parse_price_entry(data: dict[str, Any], currency: str) -> TibberPriceEntry | None:
    """Parse a single price entry from the Tibber API response."""
    starts_at = data.get("startsAt")
    if not starts_at:
        return None

    start_time = dt_util.parse_datetime(starts_at)
    if start_time is None:
        return None

    try:
        return TibberPriceEntry(
            start_time=start_time,
            energy=float(data.get("energy", 0)),
            tax=float(data.get("tax", 0)),
            total=float(data.get("total", 0)),
            level=data.get("level", "NORMAL"),
            currency=currency,
        )
    except (ValueError, TypeError):
        _LOGGER.debug("Could not parse price entry: %s", data)
        return None
