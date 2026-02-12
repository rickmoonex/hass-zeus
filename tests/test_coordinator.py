"""Tests for the Zeus price coordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeus.const import (
    CONF_ACCESS_TOKEN,
    CONF_ENERGY_PROVIDER,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
)
from custom_components.zeus.coordinator import PRICE_UPDATE_INTERVAL, PriceCoordinator
from custom_components.zeus.tibber_api import (
    TibberApiError,
    TibberAuthError,
    TibberHome,
    TibberPriceEntry,
)

from .conftest import FAKE_TOKEN


def _make_api_response(
    home_name: str = "Test Home",
    base_time: datetime | None = None,
    num_slots: int = 96,
    base_total: float = 0.25,
    base_energy: float = 0.20,
) -> dict[str, TibberHome]:
    """Create a mock TibberApiClient.async_get_prices() response."""
    if base_time is None:
        base_time = datetime(2026, 2, 9, 0, 0, 0, tzinfo=timezone(timedelta(hours=1)))

    prices = []
    for i in range(num_slots):
        slot_time = base_time + timedelta(minutes=15 * i)
        total = round(base_total + (i * 0.001), 4)
        energy = round(base_energy + (i * 0.0008), 4)
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=energy,
                tax=round(total - energy, 4),
                total=total,
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
def mock_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a mock config entry for coordinator tests."""
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


def _patch_tibber_client(mock_client: AsyncMock):
    """Patch TibberApiClient constructor to return our mock."""
    return patch(
        "custom_components.zeus.coordinator.TibberApiClient",
        return_value=mock_client,
    )


def _make_mock_client(response: dict[str, TibberHome]) -> AsyncMock:
    """Create a mock TibberApiClient with the given response."""
    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(return_value=response)
    return mock_client


async def test_coordinator_fetches_tibber_prices(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator fetches and parses Tibber prices."""
    now = dt_util.now()
    base_time = now.replace(minute=0, second=0, microsecond=0)
    response = _make_api_response(base_time=base_time, num_slots=96)

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
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
    base_time = now.replace(minute=0, second=0, microsecond=0)
    response1 = _make_api_response(
        base_time=base_time, num_slots=8, base_total=0.25, base_energy=0.20
    )
    response2 = _make_api_response(
        base_time=base_time, num_slots=8, base_total=0.50, base_energy=0.40
    )

    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(side_effect=[response1, response2])

    with _patch_tibber_client(mock_client):
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
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = _make_api_response(base_time=aligned - timedelta(hours=1), num_slots=16)

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    current_price = coordinator.get_current_price()
    assert current_price is not None


async def test_coordinator_get_current_energy_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test getting the current energy-only price."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = _make_api_response(base_time=aligned - timedelta(hours=1), num_slots=16)

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    energy_price = coordinator.get_current_energy_price()
    assert energy_price is not None
    # Energy price should be less than total price (no tax)
    total_price = coordinator.get_current_price()
    assert total_price is not None
    assert energy_price < total_price


async def test_coordinator_negative_energy_price_detection(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test negative energy price detection."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    # Create a response where the current slot has a negative energy price
    # but a positive total price (tax keeps total above zero)
    response = {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=[
                TibberPriceEntry(
                    start_time=aligned,
                    energy=-0.05,
                    tax=0.08,
                    total=0.03,  # positive total, negative energy
                    level="VERY_CHEAP",
                    currency="EUR",
                ),
            ],
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Energy price is negative → should throttle inverter
    assert coordinator.is_energy_price_negative() is True
    # But total price is positive
    current_price = coordinator.get_current_price()
    assert current_price is not None
    assert current_price > 0


async def test_coordinator_positive_energy_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that positive energy prices are correctly identified."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=[
                TibberPriceEntry(
                    start_time=aligned,
                    energy=0.20,
                    tax=0.05,
                    total=0.25,
                    level="NORMAL",
                    currency="EUR",
                ),
            ],
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.is_energy_price_negative() is False


async def test_coordinator_next_slot_price(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test next slot price retrieval."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    prices = []
    for i in range(3):
        slot_time = aligned + timedelta(minutes=15 * i)
        total = 0.10 * (i + 1)  # 0.10, 0.20, 0.30
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=total * 0.8,
                tax=total * 0.2,
                total=total,
                level="NORMAL",
                currency="EUR",
            )
        )

    response = {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=prices,
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Next slot after current (0.10) should be the second slot (0.20)
    next_price = coordinator.get_next_slot_price()
    assert next_price is not None
    assert abs(next_price - 0.20) < 0.001


async def test_coordinator_handles_api_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator handles API errors gracefully."""
    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(
        side_effect=TibberApiError("Connection failed")
    )

    with (
        _patch_tibber_client(mock_client),
        patch(
            "custom_components.zeus.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # After a failed refresh, last_update_success should be False
    assert coordinator.last_update_success is False


async def test_coordinator_handles_empty_response(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the coordinator handles empty responses gracefully."""
    mock_client = _make_mock_client({})  # No homes returned

    with _patch_tibber_client(mock_client):
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
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=[
                TibberPriceEntry(
                    start_time=aligned,
                    energy=0.20,
                    tax=0.05,
                    total=0.25,
                    level="NORMAL",
                    currency="EUR",
                ),
            ],
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Real prices
    assert coordinator.get_current_price() == 0.25
    assert coordinator.get_current_energy_price() == 0.20
    assert coordinator.is_energy_price_negative() is False
    assert coordinator.price_override is None

    # Set override to negative price — affects both getters
    coordinator.async_set_price_override(-0.05)
    assert coordinator.get_current_price() == -0.05
    assert coordinator.get_current_energy_price() == -0.05
    assert coordinator.is_energy_price_negative() is True
    assert coordinator.price_override == -0.05

    # Clear override, back to real prices
    coordinator.async_clear_price_override()
    assert coordinator.get_current_price() == 0.25
    assert coordinator.get_current_energy_price() == 0.20
    assert coordinator.is_energy_price_negative() is False
    assert coordinator.price_override is None


async def test_coordinator_price_override_zero(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that a price override of 0 works correctly (not treated as None)."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=[
                TibberPriceEntry(
                    start_time=aligned,
                    energy=0.20,
                    tax=0.05,
                    total=0.25,
                    level="NORMAL",
                    currency="EUR",
                ),
            ],
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    # Override to exactly 0
    coordinator.async_set_price_override(0.0)
    assert coordinator.get_current_price() == 0.0
    assert coordinator.get_current_energy_price() == 0.0
    assert coordinator.is_energy_price_negative() is False


async def test_coordinator_energy_price_stored_in_slots(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that both price and energy_price are correctly stored in PriceSlot."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    response = {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=[
                TibberPriceEntry(
                    start_time=aligned,
                    energy=0.18,
                    tax=0.07,
                    total=0.25,
                    level="NORMAL",
                    currency="EUR",
                ),
            ],
        )
    }

    mock_client = _make_mock_client(response)
    with _patch_tibber_client(mock_client):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    slot = coordinator.get_current_slot()
    assert slot is not None
    assert slot.price == 0.25  # total
    assert slot.energy_price == 0.18  # energy only


async def test_coordinator_update_interval_is_15_minutes(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that the price update interval is 15 minutes."""
    assert timedelta(minutes=15) == PRICE_UPDATE_INTERVAL

    coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
    assert coordinator.update_interval == timedelta(minutes=15)


async def test_coordinator_retries_on_transient_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that transient API errors are retried and succeed on later attempt."""
    now = dt_util.now()
    base_time = now.replace(minute=0, second=0, microsecond=0)
    response = _make_api_response(base_time=base_time, num_slots=8)

    mock_client = AsyncMock()
    # Fail twice, then succeed
    mock_client.async_get_prices = AsyncMock(
        side_effect=[
            TibberApiError("timeout"),
            TibberApiError("server error"),
            response,
        ]
    )

    with (
        _patch_tibber_client(mock_client),
        patch(
            "custom_components.zeus.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data is not None
    assert "Test Home" in coordinator.data
    assert mock_client.async_get_prices.call_count == 3


async def test_coordinator_retries_exhausted(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that UpdateFailed is raised after all retries are exhausted."""
    mock_client = AsyncMock()
    # Fail on all 4 attempts (1 initial + 3 retries)
    mock_client.async_get_prices = AsyncMock(
        side_effect=TibberApiError("persistent failure")
    )

    with (
        _patch_tibber_client(mock_client),
        patch(
            "custom_components.zeus.coordinator.asyncio.sleep", new_callable=AsyncMock
        ),
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    # 1 initial + 3 retries = 4 total attempts
    assert mock_client.async_get_prices.call_count == 4


async def test_coordinator_no_retry_on_auth_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that authentication errors are NOT retried."""
    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(
        side_effect=TibberAuthError("invalid token")
    )

    with (
        _patch_tibber_client(mock_client),
        patch(
            "custom_components.zeus.coordinator.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    # Auth error should NOT trigger retries — only 1 call
    assert mock_client.async_get_prices.call_count == 1
    mock_sleep.assert_not_called()


async def test_coordinator_retry_uses_exponential_backoff(
    hass: HomeAssistant, mock_entry: MockConfigEntry
) -> None:
    """Test that retry delays follow exponential backoff (30s, 60s, 120s)."""
    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(side_effect=TibberApiError("failure"))

    with (
        _patch_tibber_client(mock_client),
        patch(
            "custom_components.zeus.coordinator.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
    ):
        coordinator = PriceCoordinator(hass, mock_entry, ENERGY_PROVIDER_TIBBER)
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    # 3 retries → 3 sleep calls with increasing delays
    assert mock_sleep.call_count == 3
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [30, 60, 120]
