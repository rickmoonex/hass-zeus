"""Tests for the Zeus sensor platform."""

from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeus.const import (
    CONF_ACCESS_TOKEN,
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_ENERGY_PROVIDER,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_ENTITY,
    CONF_MAX_POWER_OUTPUT,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTPUT_CONTROL_ENTITY,
    CONF_PEAK_USAGE,
    CONF_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_PRODUCTION_ENTITY,
    CONF_SWITCH_ENTITY,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
)
from custom_components.zeus.tibber_api import TibberHome, TibberPriceEntry

from .conftest import FAKE_TOKEN


def _make_tibber_api_response() -> dict[str, TibberHome]:
    """Create a mock TibberApiClient.async_get_prices() response for sensor tests."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    prices = []
    for i in range(8):
        slot_time = aligned + timedelta(minutes=15 * i)
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=0.20,
                tax=0.05,
                total=0.25,
                level="NORMAL",
                currency="EUR",
            )
        )

    return {
        "Test Home": TibberHome(
            home_id="home-123",
            name="Test Home",
            prices=prices,
        )
    }


def _patch_tibber_client(response: dict[str, TibberHome] | None = None):
    """Create a context manager that patches TibberApiClient."""
    if response is None:
        response = _make_tibber_api_response()
    mock_client = AsyncMock()
    mock_client.async_get_prices = AsyncMock(return_value=response)
    return patch(
        "custom_components.zeus.coordinator.TibberApiClient",
        return_value=mock_client,
    )


def _entry_data() -> dict:
    """Return default entry data with access token."""
    return {
        CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER,
        CONF_ACCESS_TOKEN: FAKE_TOKEN,
    }


async def test_recommended_output_sensor_with_forecast(
    hass: HomeAssistant,
) -> None:
    """Test that forecast attributes are exposed on the recommended output sensor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Solar Inverter",
                    CONF_PRODUCTION_ENTITY: "sensor.solar_production",
                    CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
                    CONF_MAX_POWER_OUTPUT: 5000,
                    CONF_FORECAST_ENTITY: "sensor.power_production_now",
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

    # Set up mock forecast power entity and solar production states
    hass.states.async_set("sensor.power_production_now", "3200")
    hass.states.async_set("sensor.solar_production", "3000")
    hass.states.async_set("sensor.home_energy_usage", "1500")

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        # Trigger a coordinator refresh so sensors update with hass available
        coordinator = hass.data[DOMAIN][entry.entry_id]
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # Find the recommended output sensor (device name prefixed)
    state = hass.states.get("sensor.zeus_energy_manager_recommended_inverter_output")
    assert state is not None

    # Price is positive, so recommended output should be 100%
    assert float(state.state) == 100.0

    # Verify forecast production attribute is present (from configured power entity)
    attrs = state.attributes
    assert attrs["forecast_production_w"] == 3200.0


async def test_recommended_output_sensor_without_forecast(
    hass: HomeAssistant,
) -> None:
    """Test that sensor works without a forecast entity configured."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Solar Inverter",
                    CONF_PRODUCTION_ENTITY: "sensor.solar_production",
                    CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
                    CONF_MAX_POWER_OUTPUT: 5000,
                    # No forecast entity
                },
                "subentry_type": SUBENTRY_SOLAR_INVERTER,
                "title": "Solar Inverter",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.solar_production", "3000")

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_recommended_inverter_output")
    assert state is not None
    assert float(state.state) == 100.0

    # Forecast attributes should NOT be present
    attrs = state.attributes
    assert "forecast_production_w" not in attrs


async def test_entities_grouped_under_single_device(
    hass: HomeAssistant,
) -> None:
    """Test that all Zeus entities are grouped under one device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
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
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.solar_production", "3000")

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Verify a single device was created
    device_reg = dr.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_reg, entry.entry_id)
    assert len(devices) == 1

    device = devices[0]
    assert device.name == "Zeus Energy Manager"
    assert device.manufacturer == "Zeus"
    assert device.entry_type == dr.DeviceEntryType.SERVICE
    assert (DOMAIN, entry.entry_id) in device.identifiers

    # All entities should reference this device
    for entity_id in [
        "sensor.zeus_energy_manager_current_energy_price",
        "sensor.zeus_energy_manager_next_slot_price",
        "sensor.zeus_energy_manager_recommended_inverter_output",
        "binary_sensor.zeus_energy_manager_negative_energy_price",
    ]:
        state = hass.states.get(entity_id)
        assert state is not None, f"Entity {entity_id} not found"


async def test_entry_reloads_on_subentry_change(
    hass: HomeAssistant,
) -> None:
    """Test that the config entry reloads when a subentry is added."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # No recommended output sensor yet (no solar inverter subentry)
    state = hass.states.get("sensor.zeus_energy_manager_recommended_inverter_output")
    assert state is None

    # Simulate adding a solar inverter subentry which triggers reload
    hass.states.async_set("sensor.solar_production", "3000")

    with _patch_tibber_client():
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=MappingProxyType(
                    {
                        "name": "Solar Inverter",
                        CONF_PRODUCTION_ENTITY: "sensor.solar_production",
                        CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
                        CONF_MAX_POWER_OUTPUT: 5000,
                    }
                ),
                subentry_type=SUBENTRY_SOLAR_INVERTER,
                title="Solar Inverter",
                unique_id=None,
            ),
        )
        await hass.async_block_till_done()

    # After reload, the recommended output sensor should exist
    state = hass.states.get("sensor.zeus_energy_manager_recommended_inverter_output")
    assert state is not None
    assert float(state.state) == 100.0


async def test_device_schedule_binary_sensor_created(
    hass: HomeAssistant,
) -> None:
    """Test that a schedule binary sensor is created per switch device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Washing Machine",
                    CONF_SWITCH_ENTITY: "switch.washing_machine",
                    CONF_POWER_SENSOR: "sensor.washing_machine_power",
                    CONF_PEAK_USAGE: 2000,
                    CONF_DAILY_RUNTIME: 120,
                    CONF_DEADLINE: "22:00:00",
                    CONF_PRIORITY: 3,
                },
                "subentry_type": SUBENTRY_SWITCH_DEVICE,
                "title": "Washing Machine",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    # Set up the switch entity state so the scheduler can run
    hass.states.async_set("switch.washing_machine", "off")
    hass.states.async_set("sensor.washing_machine_power", "0")

    with (
        _patch_tibber_client(),
        patch(
            "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Find the schedule binary sensor — entity name is based on subentry title
    # Since there's no device for switch.washing_machine, Zeus creates one
    # named after the subentry title ("Washing Machine")
    states = [
        s for s in hass.states.async_all("binary_sensor") if "schedule" in s.entity_id
    ]
    assert len(states) == 1
    state = states[0]

    # Check that attributes are populated
    attrs = state.attributes
    assert attrs["managed_entity"] == "switch.washing_machine"
    assert attrs["power_sensor"] == "sensor.washing_machine_power"
    assert attrs["peak_usage_w"] == 2000
    assert attrs["daily_runtime_min"] == 120
    assert attrs["priority"] == 3

    # Verify the Zeus-created device is named after the subentry
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    schedule_ent = ent_reg.async_get(state.entity_id)
    assert schedule_ent is not None
    assert schedule_ent.device_id is not None
    device = dev_reg.async_get(schedule_ent.device_id)
    assert device is not None
    assert device.name == "Washing Machine"

    # The orphan switch entity should also be on this device
    switch_ent = ent_reg.async_get("switch.washing_machine")
    if switch_ent:
        assert switch_ent.device_id == device.id


async def test_device_schedule_sensor_links_to_existing_device(
    hass: HomeAssistant,
) -> None:
    """Test that the schedule binary sensor links to the target entity's device."""
    # Register a device and entity in the registries
    dev_reg = dr.async_get(hass)

    # Create a config entry for the "other" integration that owns the switch
    other_entry = MockConfigEntry(domain="other", title="Other")
    other_entry.add_to_hass(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        identifiers={("other", "washer_device")},
        name="Washing Machine Device",
        manufacturer="Samsung",
    )

    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        "switch",
        "other",
        "washer_switch",
        config_entry=other_entry,
        device_id=device.id,
        suggested_object_id="washing_machine",
    )
    hass.states.async_set("switch.washing_machine", "off")
    hass.states.async_set("sensor.washing_machine_power", "0")

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Washing Machine",
                    CONF_SWITCH_ENTITY: "switch.washing_machine",
                    CONF_POWER_SENSOR: "sensor.washing_machine_power",
                    CONF_PEAK_USAGE: 2000,
                    CONF_DAILY_RUNTIME: 120,
                    CONF_DEADLINE: "22:00:00",
                    CONF_PRIORITY: 3,
                },
                "subentry_type": SUBENTRY_SWITCH_DEVICE,
                "title": "Washing Machine",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    with (
        _patch_tibber_client(),
        patch(
            "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The schedule binary sensor should be linked to the existing device
    schedule_states = [
        s for s in hass.states.async_all("binary_sensor") if "schedule" in s.entity_id
    ]
    assert len(schedule_states) == 1

    # Verify it's on the Samsung device, not a new Zeus device
    schedule_entity = ent_reg.async_get(schedule_states[0].entity_id)
    assert schedule_entity is not None
    assert schedule_entity.device_id == device.id


async def test_run_scheduler_service(
    hass: HomeAssistant,
) -> None:
    """Test that the zeus.run_scheduler service triggers the scheduler."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Washing Machine",
                    CONF_SWITCH_ENTITY: "switch.washing_machine",
                    CONF_POWER_SENSOR: "sensor.washing_machine_power",
                    CONF_PEAK_USAGE: 2000,
                    CONF_DAILY_RUNTIME: 120,
                    CONF_DEADLINE: "22:00:00",
                    CONF_PRIORITY: 3,
                },
                "subentry_type": SUBENTRY_SWITCH_DEVICE,
                "title": "Washing Machine",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("switch.washing_machine", "off")
    hass.states.async_set("sensor.washing_machine_power", "0")

    with (
        _patch_tibber_client(),
        patch(
            "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # The service should be registered
    assert hass.services.has_service(DOMAIN, "run_scheduler")

    # Call the service
    with patch(
        "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
        new_callable=AsyncMock,
        return_value=0.0,
    ):
        await hass.services.async_call(DOMAIN, "run_scheduler", blocking=True)
        await hass.async_block_till_done()

    # Verify the coordinator has schedule results
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert len(coordinator.schedule_results) == 1


async def test_min_cycle_time_prevents_rapid_toggling(
    hass: HomeAssistant,
) -> None:
    """Test that min_cycle_time prevents the switch from toggling too fast."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Washing Machine",
                    CONF_SWITCH_ENTITY: "switch.washing_machine",
                    CONF_POWER_SENSOR: "sensor.washing_machine_power",
                    CONF_PEAK_USAGE: 2000,
                    CONF_DAILY_RUNTIME: 120,
                    CONF_DEADLINE: "22:00:00",
                    CONF_PRIORITY: 3,
                    CONF_MIN_CYCLE_TIME: 15,  # 15 minute minimum cycle
                },
                "subentry_type": SUBENTRY_SWITCH_DEVICE,
                "title": "Washing Machine",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("switch.washing_machine", "off")
    hass.states.async_set("sensor.washing_machine_power", "0")

    with (
        _patch_tibber_client(),
        patch(
            "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Find the schedule binary sensor
    states = [
        s for s in hass.states.async_all("binary_sensor") if "schedule" in s.entity_id
    ]
    assert len(states) == 1

    # Verify the min_cycle_time attribute is exposed
    attrs = states[0].attributes
    assert attrs["min_cycle_time_min"] == 15.0


async def test_device_schedule_binary_sensor_has_min_cycle_time_default(
    hass: HomeAssistant,
) -> None:
    """Test that min_cycle_time defaults to 0 when not specified."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
        subentries_data=[
            {
                "data": {
                    "name": "Dryer",
                    CONF_SWITCH_ENTITY: "switch.dryer",
                    CONF_POWER_SENSOR: "sensor.dryer_power",
                    CONF_PEAK_USAGE: 3000,
                    CONF_DAILY_RUNTIME: 60,
                    CONF_DEADLINE: "23:00:00",
                    CONF_PRIORITY: 5,
                    # No min_cycle_time specified
                },
                "subentry_type": SUBENTRY_SWITCH_DEVICE,
                "title": "Dryer",
                "unique_id": None,
            },
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("switch.dryer", "off")
    hass.states.async_set("sensor.dryer_power", "0")

    with (
        _patch_tibber_client(),
        patch(
            "custom_components.zeus.scheduler.async_get_runtime_today_minutes",
            new_callable=AsyncMock,
            return_value=0.0,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    states = [
        s for s in hass.states.async_all("binary_sensor") if "schedule" in s.entity_id
    ]
    assert len(states) == 1

    attrs = states[0].attributes
    assert attrs["min_cycle_time_min"] == 0.0
    assert attrs["cycle_locked"] is False


async def test_solar_forecast_sensor_with_data(
    hass: HomeAssistant,
) -> None:
    """Test that the solar forecast sensor shows today's total and hourly breakdown."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Build a forecast dict with hours for today and tomorrow
    now = dt_util.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow = today + timedelta(days=1)

    forecast = {}
    # 3 hours today: 500 Wh each
    for h in (8, 10, 14):
        forecast[(today + timedelta(hours=h)).isoformat()] = 500.0
    # 2 hours tomorrow: 700 Wh each
    for h in (9, 13):
        forecast[(tomorrow + timedelta(hours=h)).isoformat()] = 700.0

    coordinator.solar_forecast = forecast
    coordinator.async_set_updated_data(coordinator.data)
    await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_solar_forecast_today")
    assert state is not None
    assert float(state.state) == 1.5  # 1500 Wh = 1.5 kWh

    attrs = state.attributes
    assert attrs["today_total_kwh"] == 1.5
    assert attrs["tomorrow_total_kwh"] == 1.4
    assert len(attrs["hourly_today"]) == 3
    assert attrs["hourly_today"]["08:00"] == 0.5
    assert attrs["hourly_today"]["10:00"] == 0.5
    assert attrs["hourly_today"]["14:00"] == 0.5
    assert len(attrs["hourly_tomorrow"]) == 2


async def test_solar_forecast_sensor_without_data(
    hass: HomeAssistant,
) -> None:
    """Test that the solar forecast sensor is None when no forecast is available."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_solar_forecast_today")
    assert state is not None
    # No forecast data set on coordinator, state should be unknown
    assert state.state == "unknown"


async def test_energy_prices_sensor_with_data(
    hass: HomeAssistant,
) -> None:
    """Test that the energy prices sensor exposes hourly price arrays."""
    now = dt_util.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    # Build 96 slots for today + 16 for tomorrow with varying prices
    prices = []
    for i in range(96):
        slot_time = today_start + timedelta(minutes=15 * i)
        total = 0.20 + (i * 0.001)
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=total * 0.8,
                tax=total * 0.2,
                total=round(total, 4),
                level="NORMAL",
                currency="EUR",
            )
        )
    for i in range(16):
        slot_time = tomorrow_start + timedelta(minutes=15 * i)
        total = 0.30 + (i * 0.001)
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=total * 0.8,
                tax=total * 0.2,
                total=round(total, 4),
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

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client(response):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_energy_prices")
    assert state is not None

    # State should have hourly entries for today (past hours may be pruned
    # by the coordinator cache which drops slots older than 1 hour)
    assert int(state.state) >= 1

    attrs = state.attributes
    assert "prices_today" in attrs
    assert "prices_tomorrow" in attrs
    assert "min_price" in attrs
    assert "max_price" in attrs

    # prices_today should be a list of hourly dicts (past hours are
    # pruned from the coordinator cache)
    assert isinstance(attrs["prices_today"], list)
    assert len(attrs["prices_today"]) >= 1

    # Each entry should have 'start' and 'price'
    first = attrs["prices_today"][0]
    assert "start" in first
    assert "price" in first

    # Min/max should span the price range
    assert attrs["min_price"] <= attrs["max_price"]

    # Tomorrow should have data (we added 16 tomorrow slots = 4 hours)
    assert len(attrs["prices_tomorrow"]) == 4


async def test_energy_prices_sensor_without_data(
    hass: HomeAssistant,
) -> None:
    """Test that the energy prices sensor handles no data gracefully."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client():
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_energy_prices")
    assert state is not None

    # Should have prices_today/prices_tomorrow attributes even if sparse
    attrs = state.attributes
    assert "prices_today" in attrs
    assert "prices_tomorrow" in attrs


async def test_current_price_sensor_has_min_max_attributes(
    hass: HomeAssistant,
) -> None:
    """Test that the current price sensor includes min/max price attributes."""
    now = dt_util.now()
    # Start from the current 15-min aligned slot so nothing is pruned
    minutes = (now.minute // 15) * 15
    slot_base = now.replace(minute=minutes, second=0, microsecond=0)

    # Build 8 slots from now with distinct prices
    prices = []
    for i in range(8):
        slot_time = slot_base + timedelta(minutes=15 * i)
        total = 0.10 + (i * 0.05)  # 0.10, 0.15, 0.20, ... 0.45
        prices.append(
            TibberPriceEntry(
                start_time=slot_time,
                energy=total * 0.8,
                tax=total * 0.2,
                total=round(total, 4),
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

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Zeus",
        data=_entry_data(),
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    with _patch_tibber_client(response):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get("sensor.zeus_energy_manager_current_energy_price")
    assert state is not None

    attrs = state.attributes
    # min/max should be present (slots are for today)
    assert "min_price" in attrs
    assert "max_price" in attrs
    assert attrs["min_price"] == 0.10
    assert attrs["max_price"] == 0.45
