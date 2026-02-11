"""Climate platform for Zeus thermostat devices."""

from __future__ import annotations

import contextlib
import logging
from typing import Any, ClassVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .binary_sensor import _get_device_info_for_entity
from .const import (
    CONF_POWER_SENSOR,
    CONF_SWITCH_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_TOLERANCE,
    DOMAIN,
    SUBENTRY_THERMOSTAT_DEVICE,
)
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)

# Default target temperature when no restored state is available
DEFAULT_TARGET_TEMP = 20.0

# Hard limits for the temperature
MIN_TEMP = 5.0
MAX_TEMP = 35.0
TEMP_STEP = 0.5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus climate entities from thermostat device subentries."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_THERMOSTAT_DEVICE:
            async_add_entities(
                [ZeusThermostatClimate(coordinator, entry, subentry.subentry_id, hass)],
                config_subentry_id=subentry.subentry_id,
            )


class ZeusThermostatClimate(
    CoordinatorEntity[PriceCoordinator], ClimateEntity, RestoreEntity
):
    """
    Climate entity for a Zeus-managed thermostat zone.

    Exposes a single target temperature that the user can adjust via the
    standard HA thermostat card.  The tolerance (configured in the subentry)
    defines the heating band: Zeus heats between ``target - tolerance`` and
    ``target + tolerance``, optimising for price and solar within that range.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [HVACMode.HEAT, HVACMode.OFF]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = TEMP_STEP
    _enable_turn_on_off_backwards_compatibility = False

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the Zeus thermostat climate entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_climate"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Thermostat"
        self._attr_translation_placeholders = {"device_name": device_name}

        # Device info -- link to the same device as the binary sensor
        switch_entity = subentry.data[CONF_SWITCH_ENTITY] if subentry else ""
        self._attr_device_info = _get_device_info_for_entity(
            hass, switch_entity, entry, subentry_id, device_name
        )

        # Defaults -- will be overridden by restore_state if available
        self._attr_target_temperature = DEFAULT_TARGET_TEMP
        self._attr_hvac_mode = HVACMode.HEAT

    async def async_added_to_hass(self) -> None:
        """Restore previous state when entity is added to HA."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            # Restore HVAC mode
            if last_state.state in (HVACMode.HEAT, HVACMode.OFF):
                self._attr_hvac_mode = HVACMode(last_state.state)

            # Restore target temperature
            attrs = last_state.attributes
            if "temperature" in attrs:
                with contextlib.suppress(ValueError, TypeError):
                    self._attr_target_temperature = float(attrs["temperature"])

    @property
    def _subentry_data(self) -> dict[str, Any]:
        """Get the subentry data for this thermostat device."""
        subentry = self._entry.subentries.get(self._subentry_id)
        if subentry is None:
            return {}
        return dict(subentry.data)

    @property
    def _tolerance(self) -> float:
        """Get the temperature tolerance from subentry config."""
        try:
            return float(self._subentry_data.get(CONF_TEMPERATURE_TOLERANCE, 1.5))
        except (ValueError, TypeError):
            return 1.5

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature from the linked sensor."""
        temp_sensor = self._subentry_data.get(CONF_TEMPERATURE_SENSOR)
        if not temp_sensor or self.hass is None:
            return None
        state = self.hass.states.get(temp_sensor)
        if state and state.state not in ("unknown", "unavailable"):
            with contextlib.suppress(ValueError, TypeError):
                return float(state.state)
        return None

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current heating action based on scheduler results."""
        if self._attr_hvac_mode == HVACMode.OFF:
            return HVACAction.OFF

        result = self.coordinator.schedule_results.get(self._subentry_id)
        if result and result.should_be_on:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes with scheduler info."""
        tolerance = self._tolerance
        target = self._attr_target_temperature or DEFAULT_TARGET_TEMP

        attrs: dict[str, Any] = {
            "temperature_tolerance": tolerance,
            "heating_lower_bound": round(target - tolerance, 1),
            "heating_upper_bound": round(target + tolerance, 1),
        }

        # Power info
        power_sensor = self._subentry_data.get(CONF_POWER_SENSOR)
        if power_sensor and self.hass is not None:
            state = self.hass.states.get(power_sensor)
            if state and state.state not in ("unknown", "unavailable"):
                with contextlib.suppress(ValueError, TypeError):
                    attrs["current_power_w"] = float(state.state)

        # Scheduler decision reason
        result = self.coordinator.schedule_results.get(self._subentry_id)
        if result:
            attrs["heating_reason"] = result.reason

        # Thermal learning data
        tracker = self.coordinator.get_thermal_tracker(self._subentry_id)
        if tracker is not None:
            if tracker.wh_per_degree is not None:
                attrs["wh_per_degree"] = round(tracker.wh_per_degree, 1)
            attrs["learning_sample_count"] = tracker.sample_count

            # Estimate time to target (minutes) if we have thermal model data
            if (
                tracker.wh_per_degree is not None
                and self.current_temperature is not None
                and self._attr_target_temperature is not None
            ):
                delta = self._attr_target_temperature - self.current_temperature
                if delta > 0 and power_sensor:
                    state = self.hass.states.get(power_sensor) if self.hass else None
                    power_w: float | None = None
                    if state and state.state not in ("unknown", "unavailable"):
                        with contextlib.suppress(ValueError, TypeError):
                            power_w = float(state.state)
                    if power_w and power_w > 0:
                        energy_needed_wh = delta * tracker.wh_per_degree
                        time_hours = energy_needed_wh / power_w
                        attrs["estimated_time_to_target_min"] = round(
                            time_hours * 60, 0
                        )

        # Learned average power
        # This is stored on the latest schedule request; read from coordinator
        # schedule_results don't carry it, but we can show the tracker info
        # which is more useful anyway.

        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (heat or off)."""
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

        # Trigger a scheduler rerun so the decision engine picks up the change
        await self.coordinator.async_run_scheduler()
        if self.coordinator.data is not None:
            self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temp = kwargs.get("temperature")
        if temp is not None:
            self._attr_target_temperature = float(temp)

        self.async_write_ha_state()

        # Trigger scheduler rerun
        await self.coordinator.async_run_scheduler()
        if self.coordinator.data is not None:
            self.coordinator.async_set_updated_data(self.coordinator.data)

    async def async_turn_on(self) -> None:
        """Turn on the thermostat (set to heat mode)."""
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        """Turn off the thermostat."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update -- refresh displayed state."""
        self.async_write_ha_state()
