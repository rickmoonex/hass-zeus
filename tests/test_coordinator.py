"""Tests for the Zeus price coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeus.const import (
    CONF_ENERGY_PROVIDER,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
)
from custom_components.zeus.coordinator import PriceCoordinator


def _make_response(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
    num_slots: int = 96,
    base_price: float = 0.25,
) -> dict[str, Any]:
    """Create a mock Tibber price response."""
    if base_time is None:
        base_time = datetime(2026, 2, 9, 0, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    slots = []
    for i in range(num_slots):
        slot_time = base_time + timedelta(minutes=15 * i)
        slots.append(
            {
                "start_time": slot_time.isoformat(),
                "price": round(base_price + (i * 0.001), 4),
            }
        )

    return {"prices": {home_name: slots}}


@pytest.fixture
def mock_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a mock config entry for coordinator tests."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    return entry


async def test_coordinator_fetches_tibber_prices(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator fetches and parses Tibber prices."""
    now = dt_util.now()
    # Use current time as base so slots aren't pruned
    base_time = now.replace(minute=0, second=0, microsecond=0)
    response = _make_response(base_time=base_time, num_slots=96)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.data is not None
    assert "Test Home" in coordinator.data
    # Some slots from the past hour may be pruned, but most should remain
    assert len(coordinator.data["Test Home"]) >= 92


async def test_coordinator_caches_existing_slots(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that existing price slots are not overwritten on refresh."""
    now = dt_util.now()
    # Use current time as base so slots aren't pruned
    base_time = now.replace(minute=0, second=0, microsecond=0)
    response1 = _make_response(base_time=base_time, num_slots=8, base_price=0.25)
    response2 = _make_response(base_time=base_time, num_slots=8, base_price=0.50)

    mock_call = AsyncMock(side_effect=[response1, response2])

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        mock_call,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

        # First slot should have original price
        first_slot = coordinator.data["Test Home"][0]
        assert first_slot.price == 0.25

        # Refresh again with different prices
        await coordinator.async_refresh()

        # First slot should still have the original price (cached, immutable)
        first_slot_after = coordinator.data["Test Home"][0]
        assert first_slot_after.price == 0.25


async def test_coordinator_get_current_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test getting the current price slot."""
    now = dt_util.now()
    # Align to 15-min boundary
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = _make_response(base_time=aligned - timedelta(hours=1), num_slots=16)

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    current_price = coordinator.get_current_price()
    assert current_price is not None


async def test_coordinator_negative_price_detection(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test negative price detection."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    # Create a response where the current slot has a negative price
    response = {
        "prices": {
            "Test Home": [
                {
                    "start_time": aligned.isoformat(),
                    "price": -0.05,
                }
            ]
        }
    }

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.is_price_negative() is True


async def test_coordinator_positive_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that positive prices are correctly identified."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "prices": {
            "Test Home": [
                {
                    "start_time": aligned.isoformat(),
                    "price": 0.25,
                }
            ]
        }
    }

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.is_price_negative() is False


async def test_coordinator_next_slot_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test next slot price retrieval."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    # Create 3 slots: current + 2 future
    slots = []
    for i in range(3):
        slot_time = aligned + timedelta(minutes=15 * i)
        slots.append(
            {
                "start_time": slot_time.isoformat(),
                "price": 0.10 * (i + 1),  # 0.10, 0.20, 0.30
            }
        )

    response = {"prices": {"Test Home": slots}}

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Next slot after current (0.10) should be the second slot (0.20)
    next_price = coordinator.get_next_slot_price()
    assert next_price is not None
    assert abs(next_price - 0.20) < 0.001


async def test_coordinator_handles_service_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator handles service call errors gracefully."""
    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        side_effect=Exception("Service unavailable"),
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # After a failed refresh, last_update_success should be False
    assert coordinator.last_update_success is False


async def test_coordinator_handles_empty_response(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator handles empty responses gracefully."""
    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value={},
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_coordinator_unsupported_provider(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that unsupported providers cause update failure."""
    coordinator = PriceCoordinator(hass, mock_entry, "unsupported")
    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_coordinator_price_override(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that price override replaces the real price."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "prices": {
            "Test Home": [
                {
                    "start_time": aligned.isoformat(),
                    "price": 0.25,
                }
            ]
        }
    }

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Real price should be 0.25
    assert coordinator.get_current_price() == 0.25
    assert coordinator.is_price_negative() is False
    assert coordinator.price_override is None

    # Set override to negative price
    coordinator.async_set_price_override(-0.05)
    assert coordinator.get_current_price() == -0.05
    assert coordinator.is_price_negative() is True
    assert coordinator.price_override == -0.05

    # Clear override, back to real price
    coordinator.async_clear_price_override()
    assert coordinator.get_current_price() == 0.25
    assert coordinator.is_price_negative() is False
    assert coordinator.price_override is None


async def test_coordinator_price_override_zero(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that a price override of 0 works correctly (not treated as None)."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "prices": {
            "Test Home": [
                {
                    "start_time": aligned.isoformat(),
                    "price": 0.25,
                }
            ]
        }
    }

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Override to exactly 0
    coordinator.async_set_price_override(0.0)
    assert coordinator.get_current_price() == 0.0
    assert coordinator.is_price_negative() is False
