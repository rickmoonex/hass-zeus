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


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(hass: HomeAssistant) -> None:
    """Enable custom integrations in all tests."""
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)


@pytest.fixture(autouse=True)
def _auto_recorder(recorder_mock) -> None:
    """Ensure recorder is available for all tests (required dependency)."""


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a mock config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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


def _make_tibber_price_response(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
    num_slots: int = 96,
    base_price: float = 0.25,
) -> dict[str, Any]:
    """Create a mock Tibber get_prices response."""
    if base_time is None:
        base_time = datetime.now(tz=timezone(timedelta(hours=1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    slots = []
    for i in range(num_slots):
        slot_time = base_time + timedelta(minutes=15 * i)
        slots.append(
            {
                "start_time": slot_time.isoformat(),
                "price": base_price + (i * 0.001),
            }
        )

    return {"prices": {home_name: slots}}


def make_tibber_price_response_with_negative(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
) -> dict[str, Any]:
    """Create a mock Tibber response with some negative prices."""
    if base_time is None:
        base_time = datetime.now(tz=timezone(timedelta(hours=1))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    slots = []
    for i in range(96):
        slot_time = base_time + timedelta(minutes=15 * i)
        # Make slots 40-50 (10:00-12:30) negative
        price = -0.05 if 40 <= i <= 50 else 0.25 + (i * 0.001)
        slots.append(
            {
                "start_time": slot_time.isoformat(),
                "price": price,
            }
        )

    return {"prices": {home_name: slots}}


@pytest.fixture
def mock_tibber_service() -> Generator[AsyncMock]:
    """Mock the tibber.get_prices service call."""
    response = _make_tibber_price_response()

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ) as mock_call:
        yield mock_call
