"""
Forecast.Solar REST API client for Zeus.

Fetches solar production estimates directly from the Forecast.Solar API
instead of relying on the HA forecast_solar integration. This gives Zeus
full control over the data (watts per period) and avoids coupling to
another integration's internal API.

API docs: https://doc.forecast.solar/api:estimate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import FORECAST_SOLAR_API_BASE

_LOGGER = logging.getLogger(__name__)

# HTTP status codes
_HTTP_OK = 200
_HTTP_RATE_LIMITED = 429


class ForecastSolarApiError(Exception):
    """Raised when the Forecast.Solar API returns an error."""


@dataclass
class SolarPlaneConfig:
    """Configuration for a single solar plane (panel array)."""

    declination: int  # 0 (horizontal) to 90 (vertical)
    azimuth: int  # -180 to 180 (-180=N, -90=E, 0=S, 90=W, 180=N)
    kwp: float  # Installed kWp


@dataclass
class ForecastSolarResult:
    """Result from Forecast.Solar API call."""

    watts: dict[datetime, float]  # Average watts for each period
    wh_period: dict[datetime, float]  # Wh for each period
    wh_cumulative: dict[datetime, float]  # Cumulative Wh over the day
    wh_day: dict[str, float]  # Total Wh per day (date string -> Wh)


class ForecastSolarClient:
    """Client for the Forecast.Solar REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        latitude: float,
        longitude: float,
        planes: list[SolarPlaneConfig],
        api_key: str | None = None,
    ) -> None:
        """Initialize the Forecast.Solar client."""
        self._session = session
        self._latitude = latitude
        self._longitude = longitude
        self._planes = planes
        self._api_key = api_key

    def _build_url(self) -> str:
        """Build the API URL from configured planes."""
        lat = round(self._latitude, 4)
        lon = round(self._longitude, 4)

        # Build the planes path segment(s)
        # Single plane: /:dec/:az/:kwp
        # Multi plane:  /:dec1/:az1/:kwp1/:dec2/:az2/:kwp2/...
        plane_parts = []
        for plane in self._planes:
            plane_parts.extend(
                [
                    str(plane.declination),
                    str(plane.azimuth),
                    str(plane.kwp),
                ]
            )
        planes_path = "/".join(plane_parts)

        if self._api_key:
            return (
                f"{FORECAST_SOLAR_API_BASE}/{self._api_key}"
                f"/estimate/{lat}/{lon}/{planes_path}"
            )
        return f"{FORECAST_SOLAR_API_BASE}/estimate/{lat}/{lon}/{planes_path}"

    async def async_get_estimate(self) -> ForecastSolarResult:
        """Fetch solar production estimate from Forecast.Solar."""
        url = self._build_url()
        _LOGGER.debug("Fetching solar forecast from %s", url)

        try:
            async with self._session.get(
                url,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == _HTTP_RATE_LIMITED:
                    msg = "Forecast.Solar rate limit exceeded"
                    raise ForecastSolarApiError(msg)
                if resp.status != _HTTP_OK:
                    text = await resp.text()
                    msg = f"Forecast.Solar API returned {resp.status}: {text[:200]}"
                    raise ForecastSolarApiError(msg)

                data: dict[str, Any] = await resp.json()

        except aiohttp.ClientError as err:
            msg = f"Failed to connect to Forecast.Solar: {err}"
            raise ForecastSolarApiError(msg) from err

        return self._parse_response(data)

    @staticmethod
    def _parse_response(data: dict[str, Any]) -> ForecastSolarResult:
        """Parse the JSON response into a ForecastSolarResult."""
        api_msg = data.get("message", {})
        if api_msg.get("code", 0) != 0:
            msg = f"Forecast.Solar error: {api_msg.get('text', 'unknown')}"
            raise ForecastSolarApiError(msg)

        result = data.get("result", {})

        # Rate limit info for logging
        ratelimit = api_msg.get("ratelimit", {})
        if ratelimit:
            _LOGGER.debug(
                "Forecast.Solar rate limit: %s/%s remaining (period: %ss)",
                ratelimit.get("remaining"),
                ratelimit.get("limit"),
                ratelimit.get("period"),
            )

        watts = _parse_datetime_dict(result.get("watts", {}))
        wh_period = _parse_datetime_dict(result.get("watt_hours_period", {}))
        wh_cumulative = _parse_datetime_dict(result.get("watt_hours", {}))

        # wh_day keys are date strings like "2026-02-12"
        wh_day = {k: float(v) for k, v in result.get("watt_hours_day", {}).items()}

        return ForecastSolarResult(
            watts=watts,
            wh_period=wh_period,
            wh_cumulative=wh_cumulative,
            wh_day=wh_day,
        )


def _parse_datetime_dict(raw: dict[str, Any]) -> dict[datetime, float]:
    """
    Parse a dict of 'YYYY-MM-DD HH:MM:SS' -> value into datetime -> float.

    Forecast.Solar returns naive datetimes in the timezone of the configured
    location, which matches HA's default timezone. We attach the timezone so
    all downstream comparisons work with aware datetimes.
    """
    parsed: dict[datetime, float] = {}
    for key, value in raw.items():
        try:
            dt = dt_util.parse_datetime(key.replace(" ", "T"))
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
                parsed[dt] = float(value)
        except (ValueError, TypeError):
            _LOGGER.debug("Skipping unparseable forecast key: %s", key)
    return parsed
