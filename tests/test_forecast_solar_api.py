"""Tests for the Zeus Forecast.Solar API client."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.zeus.forecast_solar_api import (
    ForecastSolarApiError,
    ForecastSolarClient,
    ForecastSolarResult,
    SolarPlaneConfig,
    _parse_datetime_dict,
)


def _make_client(
    planes: list[SolarPlaneConfig] | None = None,
    api_key: str | None = None,
) -> ForecastSolarClient:
    """Create a ForecastSolarClient with a mock session."""
    session = MagicMock(spec=aiohttp.ClientSession)
    return ForecastSolarClient(
        session=session,
        latitude=52.37,
        longitude=4.89,
        planes=planes or [SolarPlaneConfig(declination=35, azimuth=0, kwp=6.5)],
        api_key=api_key,
    )


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------


def test_build_url_single_plane():
    """URL for a single plane without API key."""
    client = _make_client()
    url = client._build_url()  # noqa: SLF001
    assert url == "https://api.forecast.solar/estimate/52.37/4.89/35/0/6.5"


def test_build_url_with_api_key():
    """URL with API key prefix."""
    client = _make_client(api_key="my-secret-key")
    url = client._build_url()  # noqa: SLF001
    assert url == (
        "https://api.forecast.solar/my-secret-key/estimate/52.37/4.89/35/0/6.5"
    )


def test_build_url_multi_plane():
    """URL for multiple planes."""
    planes = [
        SolarPlaneConfig(declination=35, azimuth=0, kwp=4.0),
        SolarPlaneConfig(declination=20, azimuth=90, kwp=2.5),
    ]
    client = _make_client(planes=planes)
    url = client._build_url()  # noqa: SLF001
    assert url == "https://api.forecast.solar/estimate/52.37/4.89/35/0/4.0/20/90/2.5"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

SAMPLE_RESPONSE: dict[str, Any] = {
    "result": {
        "watts": {
            "2026-02-12 08:00:00": 100,
            "2026-02-12 08:30:00": 300,
            "2026-02-12 09:00:00": 800,
            "2026-02-12 09:30:00": 1200,
            "2026-02-12 10:00:00": 1500,
        },
        "watt_hours_period": {
            "2026-02-12 08:00:00": 50,
            "2026-02-12 08:30:00": 100,
        },
        "watt_hours": {
            "2026-02-12 08:00:00": 50,
            "2026-02-12 08:30:00": 150,
        },
        "watt_hours_day": {
            "2026-02-12": 5000,
        },
    },
    "message": {
        "code": 0,
        "text": "ok",
        "ratelimit": {
            "remaining": 10,
            "limit": 12,
            "period": 3600,
        },
    },
}


def test_parse_response_success():
    """Parse a valid API response."""
    result = ForecastSolarClient._parse_response(SAMPLE_RESPONSE)  # noqa: SLF001

    assert isinstance(result, ForecastSolarResult)
    assert len(result.watts) == 5
    assert len(result.wh_period) == 2
    assert len(result.wh_cumulative) == 2
    assert result.wh_day == {"2026-02-12": 5000.0}

    # Check that a specific value was parsed correctly
    watts_values = list(result.watts.values())
    assert 1500.0 in watts_values

    # All datetime keys should be timezone-aware
    for dt_key in result.watts:
        assert dt_key.tzinfo is not None


def test_parse_response_error_code():
    """API error code should raise ForecastSolarApiError."""
    data = {
        "result": {},
        "message": {"code": 1, "text": "Rate limit exceeded"},
    }
    with pytest.raises(ForecastSolarApiError, match="Rate limit exceeded"):
        ForecastSolarClient._parse_response(data)  # noqa: SLF001


def test_parse_response_empty_result():
    """Empty result should return empty dicts."""
    data = {
        "result": {},
        "message": {"code": 0, "text": "ok"},
    }
    result = ForecastSolarClient._parse_response(data)  # noqa: SLF001
    assert len(result.watts) == 0
    assert len(result.wh_period) == 0
    assert len(result.wh_day) == 0


# ---------------------------------------------------------------------------
# _parse_datetime_dict
# ---------------------------------------------------------------------------


def test_parse_datetime_dict_valid():
    """Valid datetime strings are parsed as timezone-aware datetimes."""
    raw = {
        "2026-02-12 08:00:00": 100,
        "2026-02-12 09:30:00": 500,
    }
    parsed = _parse_datetime_dict(raw)
    assert len(parsed) == 2
    # All values should be floats and keys should be timezone-aware
    for k, v in parsed.items():
        assert isinstance(v, float)
        assert k.tzinfo is not None


def test_parse_datetime_dict_invalid_key_skipped():
    """Invalid datetime keys are silently skipped."""
    raw = {
        "not-a-date": 100,
        "2026-02-12 09:00:00": 500,
    }
    parsed = _parse_datetime_dict(raw)
    assert len(parsed) == 1


def test_parse_datetime_dict_empty():
    """Empty dict returns empty dict."""
    assert _parse_datetime_dict({}) == {}


# ---------------------------------------------------------------------------
# SolarPlaneConfig
# ---------------------------------------------------------------------------


def test_solar_plane_config():
    """SolarPlaneConfig stores values correctly."""
    plane = SolarPlaneConfig(declination=35, azimuth=-90, kwp=3.2)
    assert plane.declination == 35
    assert plane.azimuth == -90
    assert plane.kwp == 3.2
