"""Config flow for the Zeus integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TimeSelector,
    TimeSelectorConfig,
)

from .const import (
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_ENERGY_PROVIDER,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FEED_IN_RATE,
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
    ENERGY_PROVIDERS,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ENERGY_PROVIDER): SelectSelector(
            SelectSelectorConfig(
                options=ENERGY_PROVIDERS,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key=CONF_ENERGY_PROVIDER,
            )
        ),
    }
)


class ZeusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zeus."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            provider = user_input[CONF_ENERGY_PROVIDER]

            # Validate that the selected provider integration is set up
            if provider == ENERGY_PROVIDER_TIBBER:
                entries = self.hass.config_entries.async_entries("tibber")
                if not entries:
                    errors["base"] = "provider_not_found"

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title="Zeus",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,  # noqa: ARG003
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            SUBENTRY_SOLAR_INVERTER: SolarInverterSubentryFlow,
            SUBENTRY_HOME_MONITOR: HomeMonitorSubentryFlow,
            SUBENTRY_SWITCH_DEVICE: SwitchDeviceSubentryFlow,
        }


class SolarInverterSubentryFlow(ConfigSubentryFlow):
    """Handle subentry flow for adding a solar inverter."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the solar inverter configuration step."""
        # Enforce max 1 solar inverter subentry
        entry = self._get_entry()
        if any(
            s.subentry_type == SUBENTRY_SOLAR_INVERTER
            for s in entry.subentries.values()
        ):
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", "Solar Inverter"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Solar Inverter"): str,
                    vol.Required(CONF_PRODUCTION_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(CONF_OUTPUT_CONTROL_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain=["number", "input_number"])
                    ),
                    vol.Required(CONF_MAX_POWER_OUTPUT): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=100000,
                            step=1,
                            unit_of_measurement="W",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_FORECAST_ENTITY): EntitySelector(
                        EntitySelectorConfig(
                            domain="sensor",
                            device_class="power",
                        )
                    ),
                    vol.Optional(CONF_FEED_IN_RATE): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=1,
                            step=0.001,
                            unit_of_measurement="EUR/kWh",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a solar inverter."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                title=user_input.get("name", subentry.title),
                data=user_input,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("name"): str,
                        vol.Required(CONF_PRODUCTION_ENTITY): EntitySelector(
                            EntitySelectorConfig(domain="sensor")
                        ),
                        vol.Required(CONF_OUTPUT_CONTROL_ENTITY): EntitySelector(
                            EntitySelectorConfig(domain=["number", "input_number"])
                        ),
                        vol.Required(CONF_MAX_POWER_OUTPUT): NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                max=100000,
                                step=1,
                                unit_of_measurement="W",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional(CONF_FORECAST_ENTITY): EntitySelector(
                            EntitySelectorConfig(
                                domain="sensor",
                                device_class="power",
                            )
                        ),
                        vol.Optional(CONF_FEED_IN_RATE): NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                max=1,
                                step=0.001,
                                unit_of_measurement="EUR/kWh",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                    }
                ),
                {"name": subentry.title, **subentry.data},
            ),
        )


class HomeMonitorSubentryFlow(ConfigSubentryFlow):
    """Handle subentry flow for adding a home energy monitor."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the home energy monitor configuration step."""
        # Enforce max 1 home monitor subentry
        entry = self._get_entry()
        if any(
            s.subentry_type == SUBENTRY_HOME_MONITOR for s in entry.subentries.values()
        ):
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", "Home Energy Monitor"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("name", default="Home Energy Monitor"): str,
                    vol.Required(CONF_ENERGY_USAGE_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a home energy monitor."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                title=user_input.get("name", subentry.title),
                data=user_input,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("name"): str,
                        vol.Required(CONF_ENERGY_USAGE_ENTITY): EntitySelector(
                            EntitySelectorConfig(domain="sensor")
                        ),
                    }
                ),
                {"name": subentry.title, **subentry.data},
            ),
        )


def _switch_device_schema() -> vol.Schema:
    """Return the schema for a switch device subentry."""
    return vol.Schema(
        {
            vol.Required("name"): str,
            vol.Required(CONF_SWITCH_ENTITY): EntitySelector(
                EntitySelectorConfig(domain=["switch", "input_boolean"])
            ),
            vol.Required(CONF_POWER_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class="power",
                )
            ),
            vol.Required(CONF_PEAK_USAGE): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_DAILY_RUNTIME): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=1440,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_DEADLINE, default="23:00:00"): TimeSelector(
                TimeSelectorConfig()
            ),
            vol.Required(CONF_PRIORITY, default=5): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=10,
                    step=1,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(CONF_MIN_CYCLE_TIME, default=0): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=60,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
        }
    )


class SwitchDeviceSubentryFlow(ConfigSubentryFlow):
    """Handle subentry flow for adding a switch device."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the switch device configuration step."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", "Switch Device"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_switch_device_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a switch device."""
        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_reload_and_abort(
                self._get_entry(),
                subentry,
                title=user_input.get("name", subentry.title),
                data=user_input,
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _switch_device_schema(),
                {"name": subentry.title, **subentry.data},
            ),
        )
