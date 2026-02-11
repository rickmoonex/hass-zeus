"""Tests for the Zeus config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.zeus.const import (
    CONF_ACCESS_TOKEN,
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_ENERGY_PROVIDER,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_ENTITY,
    CONF_MAX_POWER_OUTPUT,
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
from custom_components.zeus.tibber_api import TibberAuthError

FAKE_TOKEN = "test-token-123"  # noqa: S105


async def test_user_flow_tibber(hass: HomeAssistant, mock_setup_entry) -> None:
    """Test the full user config flow with Tibber authentication."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    # Select Tibber as provider — should advance to tibber_auth step
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tibber_auth"

    # Enter a valid token
    with patch(
        "custom_components.zeus.config_flow.TibberApiClient",
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.async_validate_token.return_value = "Test User"
        mock_client_cls.return_value = mock_client

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_ACCESS_TOKEN: FAKE_TOKEN},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Zeus (Test User)"
    assert result["data"] == {
        CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER,
        CONF_ACCESS_TOKEN: FAKE_TOKEN,
    }


async def test_user_flow_tibber_invalid_token(
    hass: HomeAssistant, mock_setup_entry
) -> None:
    """Test Tibber auth step with an invalid token shows error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_ENERGY_PROVIDER: ENERGY_PROVIDER_TIBBER},
    )
    assert result["step_id"] == "tibber_auth"

    with patch(
        "custom_components.zeus.config_flow.TibberApiClient",
    ) as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.async_validate_token.side_effect = TibberAuthError("bad token")
        mock_client_cls.return_value = mock_client

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_ACCESS_TOKEN: "bad-token"},
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_token"}


async def test_user_flow_already_configured(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test the user config flow when already configured.

    With single_config_entry: true in manifest.json, HA aborts immediately
    at flow init when an entry already exists.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "user"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_solar_inverter_subentry_flow(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test adding a solar inverter subentry."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_SOLAR_INVERTER),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "My Inverter",
            CONF_PRODUCTION_ENTITY: "sensor.solar_production",
            CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
            CONF_MAX_POWER_OUTPUT: 5000,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Inverter"


async def test_home_monitor_subentry_flow(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test adding a home energy monitor subentry."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_HOME_MONITOR),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "My Monitor",
            CONF_ENERGY_USAGE_ENTITY: "sensor.home_energy",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Monitor"


async def test_solar_inverter_subentry_already_configured(
    hass: HomeAssistant,
    mock_setup_entry,
    mock_config_entry_with_subentries: MockConfigEntry,
) -> None:
    """Test that adding a second solar inverter subentry is aborted."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry_with_subentries.entry_id, SUBENTRY_SOLAR_INVERTER),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_home_monitor_subentry_already_configured(
    hass: HomeAssistant,
    mock_setup_entry,
    mock_config_entry_with_subentries: MockConfigEntry,
) -> None:
    """Test that adding a second home monitor subentry is aborted."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry_with_subentries.entry_id, SUBENTRY_HOME_MONITOR),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_solar_inverter_subentry_with_forecast(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test adding a solar inverter subentry with an optional forecast entity."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_SOLAR_INVERTER),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "My Inverter",
            CONF_PRODUCTION_ENTITY: "sensor.solar_production",
            CONF_OUTPUT_CONTROL_ENTITY: "number.inverter_output",
            CONF_MAX_POWER_OUTPUT: 5000,
            CONF_FORECAST_ENTITY: "sensor.power_production_now",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "My Inverter"


async def test_switch_device_subentry_flow(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test adding a switch device subentry."""
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_SWITCH_DEVICE),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "Washing Machine",
            CONF_SWITCH_ENTITY: "switch.washing_machine",
            CONF_POWER_SENSOR: "sensor.washing_machine_power",
            CONF_PEAK_USAGE: 2000,
            CONF_DAILY_RUNTIME: 120,
            CONF_DEADLINE: "22:00:00",
            CONF_PRIORITY: 3,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Washing Machine"


async def test_multiple_switch_device_subentries(
    hass: HomeAssistant, mock_setup_entry, mock_config_entry
) -> None:
    """Test that multiple switch device subentries can be added."""
    # Add first switch device
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_SWITCH_DEVICE),
        context={"source": "user"},
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "Washing Machine",
            CONF_SWITCH_ENTITY: "switch.washing_machine",
            CONF_POWER_SENSOR: "sensor.washing_machine_power",
            CONF_PEAK_USAGE: 2000,
            CONF_DAILY_RUNTIME: 120,
            CONF_DEADLINE: "22:00:00",
            CONF_PRIORITY: 3,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Add second switch device (should NOT be aborted)
    result = await hass.config_entries.subentries.async_init(
        (mock_config_entry.entry_id, SUBENTRY_SWITCH_DEVICE),
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM  # Not aborted

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "name": "Dishwasher",
            CONF_SWITCH_ENTITY: "switch.dishwasher",
            CONF_POWER_SENSOR: "sensor.dishwasher_power",
            CONF_PEAK_USAGE: 1800,
            CONF_DAILY_RUNTIME: 90,
            CONF_DEADLINE: "23:00:00",
            CONF_PRIORITY: 5,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Dishwasher"
