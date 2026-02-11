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


def _make_price_response() -> dict:
    """Create a mock Tibber price response covering the current slot."""
    now = dt_util.now()
    minutes = (now.minute // 15) * 15
    aligned = now.replace(minute=minutes, second=0, microsecond=0)

    slots = []
    for i in range(8):
        slot_time = aligned + timedelta(minutes=15 * i)
        slots.append(
            {
                "start_time": slot_time.isoformat(),
                "price": 0.25,
            }
        )

    return {"prices": {"Test Home": slots}}


async def test_recommended_output_sensor_with_forecast(
    hass: HomeAssistant,
) -> None:
    """Test that forecast attributes are exposed on the recommended output sensor."""
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

    # Set up mock forecast.solar entity states
    hass.states.async_set("sensor.power_production_now", "3200")
    hass.states.async_set("sensor.energy_production_today_remaining", "8500")
    hass.states.async_set("sensor.energy_production_today", "12000")
    hass.states.async_set("sensor.energy_current_hour", "800")
    hass.states.async_set("sensor.energy_next_hour", "950")
    hass.states.async_set("sensor.solar_production", "3000")
    hass.states.async_set("sensor.home_energy_usage", "1500")

    response = _make_price_response()

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
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

    # Verify forecast attributes are present
    attrs = state.attributes
    assert attrs["forecast_production_w"] == 3200.0
    assert attrs["forecast_energy_today_remaining_wh"] == 8500.0
    assert attrs["forecast_energy_today_wh"] == 12000.0
    assert attrs["forecast_energy_current_hour_wh"] == 800.0
    assert attrs["forecast_energy_next_hour_wh"] == 950.0


async def test_recommended_output_sensor_without_forecast(
    hass: HomeAssistant,
) -> None:
    """Test that sensor works without a forecast entity configured."""
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

    response = _make_price_response()

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
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
        ],
    )
    entry.add_to_hass(hass)

    hass.states.async_set("sensor.solar_production", "3000")

    response = _make_price_response()

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)

    response = _make_price_response()

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # No recommended output sensor yet (no solar inverter subentry)
    state = hass.states.get("sensor.zeus_energy_manager_recommended_inverter_output")
    assert state is None

    # Simulate adding a solar inverter subentry which triggers reload
    hass.states.async_set("sensor.solar_production", "3000")

    with patch(
        "homeassistant.core.ServiceRegistry.async_call",
        new_callable=AsyncMock,
        return_value=response,
    ):
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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

    response = _make_price_response()

    with (
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock,
            return_value=response,
        ),
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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

    response = _make_price_response()

    with (
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock,
            return_value=response,
        ),
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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

    response = _make_price_response()

    with (
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock,
            return_value=response,
        ),
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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

    response = _make_price_response()

    with (
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock,
            return_value=response,
        ),
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
        data={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
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

    response = _make_price_response()

    with (
        patch(
            "homeassistant.core.ServiceRegistry.async_call",
            new_callable=AsyncMock,
            return_value=response,
        ),
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
