"""Sensor platform for Zeus energy management."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_ENTITY,
    CONF_MAX_POWER_OUTPUT,
    CONF_PRODUCTION_ENTITY,
    DOMAIN,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_SOLAR_INVERTER,
)
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    """Return the shared Zeus device info."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Zeus Energy Manager",
        manufacturer="Zeus",
        entry_type=DeviceEntryType.SERVICE,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus sensor entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    # All entities share a single device. Don't pass config_subentry_id
    # so the device isn't incorrectly linked to a specific subentry.
    # Cleanup on subentry removal is handled by the update listener reload.
    entities: list[SensorEntity] = [
        ZeusCurrentPriceSensor(coordinator, entry),
        ZeusNextSlotPriceSensor(coordinator, entry),
    ]

    entities.extend(
        ZeusRecommendedOutputSensor(coordinator, entry, subentry.subentry_id)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_SOLAR_INVERTER
    )

    async_add_entities(entities)


class ZeusCurrentPriceSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Sensor showing the current energy price."""

    _attr_has_entity_name = True
    _attr_translation_key = "current_energy_price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the current price sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_energy_price"
        self._attr_device_info = _device_info(entry)
        self._update_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @callback
    def _update_from_coordinator(self) -> None:
        """Update sensor state from coordinator data."""
        self._attr_native_value = self.coordinator.get_current_price()

        slot = self.coordinator.get_current_slot()
        attrs: dict[str, Any] = {}
        if slot:
            attrs["slot_start"] = slot.start_time.isoformat()
        if self.coordinator.price_override is not None:
            attrs["price_override"] = self.coordinator.price_override
        self._attr_extra_state_attributes = attrs


class ZeusNextSlotPriceSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Sensor showing the price for the next 15-minute slot."""

    _attr_has_entity_name = True
    _attr_translation_key = "next_slot_price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the next slot price sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_slot_price"
        self._attr_device_info = _device_info(entry)
        self._update_from_coordinator()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_from_coordinator()
        self.async_write_ha_state()

    @callback
    def _update_from_coordinator(self) -> None:
        """Update sensor state from coordinator data."""
        self._attr_native_value = self.coordinator.get_next_slot_price()

        slot = self.coordinator.get_next_slot()
        self._attr_extra_state_attributes = (
            {"slot_start": slot.start_time.isoformat()} if slot else {}
        )


class ZeusRecommendedOutputSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """
    Sensor showing the recommended inverter output percentage.

    When the energy price is negative (you pay to export), this sensor
    calculates the optimal inverter output to match home consumption
    and avoid exporting to the grid.

    When the price is positive (you earn money for exporting), this
    sensor returns 100% (full production).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "recommended_inverter_output"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
    ) -> None:
        """Initialize the recommended output sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_recommended_output"
        self._attr_device_info = _device_info(entry)
        self._update_recommended_output()

    @property
    def _subentry_data(self) -> dict[str, Any]:
        """Get the subentry data for this inverter."""
        subentry = self._entry.subentries.get(self._subentry_id)
        if subentry is None:
            return {}
        return dict(subentry.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_recommended_output()
        self.async_write_ha_state()

    @callback
    def _update_recommended_output(self) -> None:
        """Calculate the recommended inverter output percentage."""
        # If price is not negative, produce at full capacity
        if not self.coordinator.is_price_negative():
            self._attr_native_value = 100.0
            self._update_extra_attributes()
            return

        subentry_data = self._subentry_data
        max_power = subentry_data.get(CONF_MAX_POWER_OUTPUT)
        if not max_power or max_power <= 0:
            self._attr_native_value = 0.0
            self._update_extra_attributes()
            return

        # Find the home energy monitor to get current consumption
        consumption = self._get_home_consumption()
        if consumption is None or consumption <= 0:
            # No consumption data or home is producing more than consuming
            self._attr_native_value = 0.0
            self._update_extra_attributes()
            return

        # Calculate output percentage to match consumption
        recommended_pct = min(100.0, (consumption / max_power) * 100.0)
        self._attr_native_value = round(recommended_pct, 1)
        self._update_extra_attributes()

    def _update_extra_attributes(self) -> None:
        """Update extra state attributes."""
        subentry_data = self._subentry_data
        attrs: dict[str, Any] = {
            "price_is_negative": self.coordinator.is_price_negative(),
        }
        if self.coordinator.price_override is not None:
            attrs["price_override"] = self.coordinator.price_override

        # self.hass is None during __init__ (before entity is added to HA)
        if self.hass is not None:
            production_entity = subentry_data.get(CONF_PRODUCTION_ENTITY)
            if production_entity:
                state = self.hass.states.get(production_entity)
                try:
                    attrs["current_production_w"] = (
                        float(state.state)
                        if state and state.state not in ("unknown", "unavailable")
                        else None
                    )
                except (ValueError, TypeError):
                    attrs["current_production_w"] = None

            consumption = self._get_home_consumption()
            attrs["home_consumption_w"] = consumption

            # Solar forecast data (from forecast.solar integration)
            forecast_entity = subentry_data.get(CONF_FORECAST_ENTITY)
            if forecast_entity:
                attrs.update(self._get_forecast_data(forecast_entity))

        attrs["max_power_output_w"] = subentry_data.get(CONF_MAX_POWER_OUTPUT)

        self._attr_extra_state_attributes = attrs

    def _get_home_consumption(self) -> float | None:
        """
        Get the current home energy consumption in watts.

        Looks for a home_monitor subentry to read the energy usage entity.
        Positive values mean consumption, negative means production.
        """
        if self.hass is None:
            return None

        for subentry in self._entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_HOME_MONITOR:
                entity_id = subentry.data.get(CONF_ENERGY_USAGE_ENTITY)
                if entity_id:
                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unknown", "unavailable"):
                        try:
                            return float(state.state)
                        except ValueError:
                            _LOGGER.warning(
                                "Could not parse energy usage from %s: %s",
                                entity_id,
                                state.state,
                            )
                            return None
        return None

    def _get_forecast_data(self, forecast_entity: str) -> dict[str, Any]:
        """
        Read solar forecast data from the configured forecast entity.

        Returns a dict of forecast-related attributes. The forecast entity
        is typically sensor.power_production_now from forecast.solar, but
        any power sensor will work for the forecast_production_w value.

        Additionally reads well-known forecast.solar entities for extra
        context (energy today remaining, energy current/next hour).
        """
        attrs: dict[str, Any] = {}

        # Read the configured forecast power entity
        _LOGGER.debug("Reading forecast entity: %s", forecast_entity)
        state = self.hass.states.get(forecast_entity)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                attrs["forecast_production_w"] = float(state.state)
                _LOGGER.debug("Forecast entity %s = %s W", forecast_entity, state.state)
            except (ValueError, TypeError):
                attrs["forecast_production_w"] = None
                _LOGGER.debug(
                    "Forecast entity %s has non-numeric state: %s",
                    forecast_entity,
                    state.state,
                )
        else:
            attrs["forecast_production_w"] = None
            _LOGGER.debug(
                "Forecast entity %s not found or unavailable (state=%s)",
                forecast_entity,
                state.state if state else "missing",
            )

        # Read well-known forecast.solar entities for additional context.
        # These are hardcoded entity IDs from the forecast_solar integration.
        forecast_entities = {
            "forecast_energy_today_remaining_wh": (
                "sensor.energy_production_today_remaining"
            ),
            "forecast_energy_today_wh": "sensor.energy_production_today",
            "forecast_energy_current_hour_wh": "sensor.energy_current_hour",
            "forecast_energy_next_hour_wh": "sensor.energy_next_hour",
        }
        for attr_key, entity_id in forecast_entities.items():
            entity_state = self.hass.states.get(entity_id)
            if entity_state and entity_state.state not in ("unknown", "unavailable"):
                try:
                    # forecast.solar stores native values in Wh but displays kWh
                    attrs[attr_key] = float(entity_state.state)
                except (ValueError, TypeError):
                    attrs[attr_key] = None
                    _LOGGER.debug(
                        "Forecast entity %s has non-numeric state: %s",
                        entity_id,
                        entity_state.state,
                    )
            else:
                attrs[attr_key] = None
                _LOGGER.debug(
                    "Forecast entity %s not found or unavailable (state=%s)",
                    entity_id,
                    entity_state.state if entity_state else "missing",
                )

        return attrs
