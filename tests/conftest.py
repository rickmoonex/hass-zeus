"""Fixtures for Zeus tests."""

from __future__ import annotations

from collections.abc import Generator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import loader
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeus.const import (
    CONF_ACCESS_TOKEN,
    CONF_ENERGY_PROVIDER,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_MAX_POWER_OUTPUT,
    CONF_OUTPUT_CONTROL_ENTITY,
    CONF_PRODUCTION_ENTITY,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_SOLAR_INVERTER,
)
from custom_components.zeus.tibber_api import TibberHome, TibberPriceEntry


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations in all tests."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)


@pytest.fixture(autouse=True)
def _auto_recorder(recorder_mock) -> None:
    """Ensure recorder is available for all tests (required dependency)."""


FAKE_TOKEN = "test-token-123"  # noqa: S105


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data={
            CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER,
            CONF_ACCESS_TOKEN: FAKE_TOKEN,
        },
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_config_entry_with_subentries(hass: HomeAssistant) -> MockConfigEntry:
    """Create a mock config entry with solar inverter and home monitor subentries."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data={
            CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER,
            CONF_ACCESS_TOKEN: FAKE_TOKEN,
        },
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Solar Inverter",
                    CONF_PRODUCTION_ENTITY: "sensor.solar_production",
                    CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
                    CONF_MAX_POWER_OUTPUT: 5000,
                },
                "subentry_type": SUBENTRY_SOLAR_INVERTER,
                "title": "Solar Inverter",
                "unique_id": None,
            },
            {
                "data": {
                    "name": "Home Energy Monitor",
                    CONF_ENERGY_USAGE_ENTITY: "sensor.home_energy_usage",
                },
                "subentry_type": SUBENTRY_HOME_MONITOR,
                "title": "Home Energy Monitor",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_setup_entry() -> Generator[None]:
    """Override async_setup_entry."""
    with patch(
        "custom_components.zeus.async_setup_entry",
        return_value=True,
    ):
        yield


def make_tibber_api_response(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
    num_slots: int = 96,
    base_price: float = 0.25,
    base_energy_price: float | None = None,
) -> dict[str, Any]:
    """Create a mock TibberApiClient.async_get_prices() response.

    Returns a dict[str, TibberHome] matching the new API client format.
    """
    if base_time is None:
        base_time = datetime.now(tz=timezone(timedelta(hours=1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    if base_energy_price is None:
        base_energy_price = base_price * 0.8  # ~80% is energy, ~20% is tax

    prices = []
    for i in range(num_slots):
        slot_time = base_time + timedelta(minutes=15 * i)
        total = base_price + (i * 0.001)
        energy = base_energy_price + (i * 0.0008)
        tax = total - energy
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=round(energy, 4),
                tax=round(tax, 4),
                total=round(total, 4),
                level="NORMAL",
                currency="EUR",
            )
        )

    return {
        home_name: TibberHome(
            home_id="home-123",
            name=home_name,
            prices=prices,
        )
    }


def make_tibber_api_response_with_negative(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
) -> dict[str, Any]:
    """Create a mock Tibber API response with some negative energy prices."""
    if base_time is None:
        base_time = datetime.now(tz=timezone(timedelta(hours=1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    prices = []
    for i in range(96):
        slot_time = base_time + timedelta(minutes=15 * i)
        # Make slots 40-50 (10:00-12:30) have negative energy prices
        if 40 <= i <= 50:
            energy = -0.05
            tax = 0.03
            total = energy + tax  # -0.02
        else:
            energy = 0.20 + (i * 0.0008)
            tax = 0.05 + (i * 0.0002)
            total = energy + tax
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=round(energy, 4),
                tax=round(tax, 4),
                total=round(total, 4),
                level="NORMAL",
                currency="EUR",
            )
        )

    return {
        home_name: TibberHome(
            home_id="home-123",
            name=home_name,
            prices=prices,
        )
    }


@pytest.fixture
def mock_tibber_api() -> Generator[AsyncMock]:
    """Mock the TibberApiClient used by the coordinator."""
    response = make_tibber_api_response()

    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(return_value=response)

    with patch(
        "custom_components.zeus.coordinator.TibberApiClient",
        return_value=mock_client,
    ):
        yield mock_client
