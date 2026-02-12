"""Config flow for the Zeus integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
    TimeSelectorConfig,
)

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AVG_USAGE,
    CONF_CYCLE_DURATION,
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_DELAY_INTERVALS,
    CONF_DYNAMIC_CYCLE_DURATION,
    CONF_ENERGY_PROVIDER,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_API_KEY,
    CONF_FORECAST_ENTITY,
    CONF_MAX_POWER_OUTPUT,
    CONF_MIN_CYCLE_TIME,
    CONF_OUTPUT_CONTROL_ENTITY,
    CONF_PEAK_USAGE,
    CONF_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_PRODUCTION_ENTITY,
    CONF_SOLAR_AZIMUTH,
    CONF_SOLAR_DECLINATION,
    CONF_SOLAR_KWP,
    CONF_SWITCH_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_TOLERANCE,
    CONF_USE_ACTUAL_POWER,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
    ENERGY_PROVIDERS,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_MANUAL_DEVICE,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
    SUBENTRY_THERMOSTAT_DEVICE,
)
from .tibber_api import TibberApiClient, TibberAuthError

_LOGGER = logging.getLogger(__name__)

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

STEP_TIBBER_AUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ACCESS_TOKEN): str,
    }
)


class ZeusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Zeus."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._provider: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step — select energy provider."""
        if user_input is not None:
            self._provider = user_input[CONF_ENERGY_PROVIDER]

            if self._provider == ENERGY_PROVIDER_TIBBER:
                return await self.async_step_tibber_auth()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    async def async_step_tibber_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle Tibber API token authentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            access_token = user_input[CONF_ACCESS_TOKEN]

            # Validate the token by querying the Tibber API
            session = async_get_clientsession(self.hass)
            client = TibberApiClient(session, access_token)

            try:
                viewer_name = await client.async_validate_token()
            except TibberAuthError:
                errors["base"] = "invalid_token"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating Tibber token")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"Zeus ({viewer_name})",
                    data={
                        CONF_ENERGY_PROVIDER: self._provider,
                        CONF_ACCESS_TOKEN: access_token,
                    },
                )

        return self.async_show_form(
            step_id="tibber_auth",
            data_schema=STEP_TIBBER_AUTH_SCHEMA,
            errors=errors,
            description_placeholders={
                "tibber_token_url": "https://developer.tibber.com/settings/access-token"
            },
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
            SUBENTRY_THERMOSTAT_DEVICE: ThermostatDeviceSubentryFlow,
            SUBENTRY_MANUAL_DEVICE: ManualDeviceSubentryFlow,
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
                    vol.Required(CONF_SOLAR_DECLINATION, default=35): NumberSelector(
                        NumberSelectorConfig(
                            min=0,
                            max=90,
                            step=1,
                            unit_of_measurement="°",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_SOLAR_AZIMUTH, default=0): NumberSelector(
                        NumberSelectorConfig(
                            min=-180,
                            max=180,
                            step=1,
                            unit_of_measurement="°",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(CONF_SOLAR_KWP): NumberSelector(
                        NumberSelectorConfig(
                            min=0.1,
                            max=100.0,
                            step=0.01,
                            unit_of_measurement="kWp",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Optional(CONF_FORECAST_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
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
                        vol.Required(
                            CONF_SOLAR_DECLINATION, default=35
                        ): NumberSelector(
                            NumberSelectorConfig(
                                min=0,
                                max=90,
                                step=1,
                                unit_of_measurement="°",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(CONF_SOLAR_AZIMUTH, default=0): NumberSelector(
                            NumberSelectorConfig(
                                min=-180,
                                max=180,
                                step=1,
                                unit_of_measurement="°",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Required(CONF_SOLAR_KWP): NumberSelector(
                            NumberSelectorConfig(
                                min=0.1,
                                max=100.0,
                                step=0.01,
                                unit_of_measurement="kWp",
                                mode=NumberSelectorMode.BOX,
                            )
                        ),
                        vol.Optional(CONF_FORECAST_API_KEY): TextSelector(
                            TextSelectorConfig(type=TextSelectorType.PASSWORD)
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
            vol.Optional(CONF_USE_ACTUAL_POWER, default=False): BooleanSelector(),
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


def _thermostat_device_schema() -> vol.Schema:
    """Return the schema for a thermostat device subentry."""
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
            vol.Required(CONF_TEMPERATURE_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class="temperature",
                )
            ),
            vol.Required(CONF_PEAK_USAGE): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=10000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_TEMPERATURE_TOLERANCE, default=1.5): NumberSelector(
                NumberSelectorConfig(
                    min=0.5,
                    max=5.0,
                    step=0.5,
                    unit_of_measurement="\u00b0C",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_PRIORITY, default=5): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=10,
                    step=1,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
            vol.Optional(CONF_MIN_CYCLE_TIME, default=5): NumberSelector(
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


class ThermostatDeviceSubentryFlow(ConfigSubentryFlow):
    """Handle subentry flow for adding a thermostat device."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the thermostat device configuration step."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", "Thermostat Device"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_thermostat_device_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a thermostat device."""
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
                _thermostat_device_schema(),
                {"name": subentry.title, **subentry.data},
            ),
        )


def _manual_device_schema() -> vol.Schema:
    """Return the schema for a manual (dumb) device subentry."""
    return vol.Schema(
        {
            vol.Required("name"): str,
            vol.Required(CONF_PEAK_USAGE): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_AVG_USAGE): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=100000,
                    step=1,
                    unit_of_measurement="W",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_CYCLE_DURATION): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=1440,
                    step=1,
                    unit_of_measurement="min",
                    mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Optional(CONF_DYNAMIC_CYCLE_DURATION, default=False): BooleanSelector(),
            vol.Optional(CONF_POWER_SENSOR): EntitySelector(
                EntitySelectorConfig(
                    domain="sensor",
                    device_class="power",
                )
            ),
            vol.Optional(CONF_DELAY_INTERVALS): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT)
            ),
            vol.Required(CONF_PRIORITY, default=5): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=10,
                    step=1,
                    mode=NumberSelectorMode.SLIDER,
                )
            ),
        }
    )


class ManualDeviceSubentryFlow(ConfigSubentryFlow):
    """Handle subentry flow for adding a manual (dumb) device."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle the manual device configuration step."""
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get("name", "Manual Device"),
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_manual_device_schema(),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Handle reconfiguration of a manual device."""
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
                _manual_device_schema(),
                {"name": subentry.title, **subentry.data},
            ),
        )
