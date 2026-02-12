"""Sensor platform for Zeus energy management."""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CYCLE_DURATION,
    CONF_DELAY_INTERVALS,
    CONF_DYNAMIC_CYCLE_DURATION,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_ENTITY,
    CONF_MAX_POWER_OUTPUT,
    CONF_PEAK_USAGE,
    CONF_PRODUCTION_ENTITY,
    CONF_SWITCH_ENTITY,
    DOMAIN,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_MANUAL_DEVICE,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
    SUBENTRY_THERMOSTAT_DEVICE,
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


def _read_entity_float(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a numeric value from an entity state, returning None on failure."""
    if not entity_id or hass is None:
        return None
    state = hass.states.get(entity_id)
    if state and state.state not in ("unknown", "unavailable"):
        with contextlib.suppress(ValueError, TypeError):
            return float(state.state)
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus sensor entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Global sensors (shared Zeus Energy Manager device)
    entities: list[SensorEntity] = [
        ZeusCurrentPriceSensor(coordinator, entry),
        ZeusCurrentEnergyOnlyPriceSensor(coordinator, entry),
        ZeusNextSlotPriceSensor(coordinator, entry),
        ZeusSolarSurplusSensor(coordinator, entry),
        ZeusSolarSelfConsumptionRatioSensor(coordinator, entry),
        ZeusHomeConsumptionSensor(coordinator, entry),
        ZeusGridImportSensor(coordinator, entry),
        ZeusSolarFractionSensor(coordinator, entry),
        ZeusTodayAveragePriceSensor(coordinator, entry),
        ZeusTodayMinPriceSensor(coordinator, entry),
        ZeusTodayMaxPriceSensor(coordinator, entry),
        ZeusCheapestUpcomingPriceSensor(coordinator, entry),
    ]

    entities.extend(
        ZeusRecommendedOutputSensor(coordinator, entry, subentry.subentry_id)
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_SOLAR_INVERTER
    )

    async_add_entities(entities)

    # Per-switch-device sensors (linked to their subentry)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_SWITCH_DEVICE:
            async_add_entities(
                [
                    ZeusDeviceRuntimeTodaySensor(
                        coordinator, entry, subentry.subentry_id, hass
                    ),
                ],
                config_subentry_id=subentry.subentry_id,
            )

    # Per-thermostat-device sensors (linked to their subentry)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_THERMOSTAT_DEVICE:
            async_add_entities(
                [
                    ZeusThermostatRuntimeTodaySensor(
                        coordinator, entry, subentry.subentry_id, hass
                    ),
                ],
                config_subentry_id=subentry.subentry_id,
            )

    # Per-manual-device sensors (linked to their subentry)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_MANUAL_DEVICE:
            async_add_entities(
                [
                    ZeusManualDeviceRecommendationSensor(
                        coordinator, entry, subentry.subentry_id
                    ),
                ],
                config_subentry_id=subentry.subentry_id,
            )


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
            attrs["energy_price"] = slot.energy_price
        if self.coordinator.price_override is not None:
            attrs["price_override"] = self.coordinator.price_override
        self._attr_extra_state_attributes = attrs


class ZeusCurrentEnergyOnlyPriceSensor(
    CoordinatorEntity[PriceCoordinator], SensorEntity
):
    """
    Sensor showing the current energy-only price (without tax).

    This is the price relevant for grid export — what you receive or pay
    per kWh when feeding solar back to the grid. When this goes negative
    you are paying to export, which is when the inverter should throttle.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "current_energy_only_price"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the energy-only price sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_current_energy_only_price"
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
        self._attr_native_value = self.coordinator.get_current_energy_price()

        slot = self.coordinator.get_current_slot()
        attrs: dict[str, Any] = {}
        if slot:
            attrs["slot_start"] = slot.start_time.isoformat()
            attrs["total_price"] = slot.price
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
        # If Zeus is disabled or energy price is not negative, produce at full capacity
        if (
            not self.coordinator.enabled
            or not self.coordinator.is_energy_price_negative()
        ):
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
            "energy_price_is_negative": self.coordinator.is_energy_price_negative(),
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

            # Solar forecast data from configured power entity
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
        Read solar forecast data from the configured forecast power entity.

        Returns a dict with the current forecast production value in watts.
        """
        attrs: dict[str, Any] = {}

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

        return attrs


# ---------------------------------------------------------------------------
# Helper: read solar production and home consumption from subentries
# ---------------------------------------------------------------------------


def _get_solar_production(hass: HomeAssistant, entry: ConfigEntry) -> float | None:
    """Read current solar production in watts from inverter subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_SOLAR_INVERTER:
            return _read_entity_float(hass, subentry.data.get(CONF_PRODUCTION_ENTITY))
    return None


def _get_home_consumption(hass: HomeAssistant, entry: ConfigEntry) -> float | None:
    """Read current home consumption in watts from home monitor subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_HOME_MONITOR:
            return _read_entity_float(hass, subentry.data.get(CONF_ENERGY_USAGE_ENTITY))
    return None


# ---------------------------------------------------------------------------
# Global sensors: Solar & energy
# ---------------------------------------------------------------------------


class ZeusSolarSurplusSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Real-time solar surplus in watts (production - consumption)."""

    _attr_has_entity_name = True
    _attr_translation_key = "solar_surplus"
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the solar surplus sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_surplus"
        self._attr_device_info = _device_info(entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        production = _get_solar_production(self.hass, self._entry)
        consumption = _get_home_consumption(self.hass, self._entry)
        if production is not None and consumption is not None:
            self._attr_native_value = round(max(0.0, production - consumption), 1)
        else:
            self._attr_native_value = None


class ZeusSolarSelfConsumptionRatioSensor(
    CoordinatorEntity[PriceCoordinator], SensorEntity
):
    """Percentage of solar production consumed by the home (not exported)."""

    _attr_has_entity_name = True
    _attr_translation_key = "solar_self_consumption_ratio"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the solar self-consumption ratio sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_self_consumption_ratio"
        self._attr_device_info = _device_info(entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        production = _get_solar_production(self.hass, self._entry)
        consumption = _get_home_consumption(self.hass, self._entry)
        if production is not None and production > 0 and consumption is not None:
            # Self-consumption = min(consumption, production) / production
            self_consumed = min(consumption, production)
            self._attr_native_value = round((self_consumed / production) * 100.0, 1)
        else:
            self._attr_native_value = None


class ZeusHomeConsumptionSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Home power consumption in watts."""

    _attr_has_entity_name = True
    _attr_translation_key = "home_consumption"
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the home consumption sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_home_consumption"
        self._attr_device_info = _device_info(entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        consumption = _get_home_consumption(self.hass, self._entry)
        self._attr_native_value = (
            round(consumption, 1) if consumption is not None else None
        )


class ZeusGridImportSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Grid import in watts (consumption minus production, 0 if producing surplus)."""

    _attr_has_entity_name = True
    _attr_translation_key = "grid_import"
    _attr_native_unit_of_measurement = "W"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the grid import sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_grid_import"
        self._attr_device_info = _device_info(entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        production = _get_solar_production(self.hass, self._entry)
        consumption = _get_home_consumption(self.hass, self._entry)
        if consumption is not None:
            prod = production if production is not None else 0.0
            self._attr_native_value = round(max(0.0, consumption - prod), 1)
        else:
            self._attr_native_value = None


class ZeusSolarFractionSensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Percentage of home consumption covered by solar production."""

    _attr_has_entity_name = True
    _attr_translation_key = "solar_fraction"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the solar fraction sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_solar_fraction"
        self._attr_device_info = _device_info(entry)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        production = _get_solar_production(self.hass, self._entry)
        consumption = _get_home_consumption(self.hass, self._entry)
        if production is not None and consumption is not None and consumption > 0:
            fraction = min(100.0, (production / consumption) * 100.0)
            self._attr_native_value = round(fraction, 1)
        elif consumption is not None and consumption == 0:
            # No consumption — solar covers 100% (trivially)
            self._attr_native_value = 100.0 if production and production > 0 else 0.0
        else:
            self._attr_native_value = None


# ---------------------------------------------------------------------------
# Global sensors: Price analytics
# ---------------------------------------------------------------------------


class _ZeusPriceSensorBase(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Base class for price-derived sensors."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 4

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the price sensor base."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = _device_info(entry)

    def _get_today_slots(self) -> list[Any]:
        """Return all price slots for today."""
        if not self.coordinator.data:
            return []
        home = self.coordinator.get_first_home_name()
        if not home:
            return []
        now = dt_util.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow_start = today_start + timedelta(days=1)
        return [
            s
            for s in self.coordinator.data.get(home, [])
            if today_start <= s.start_time < tomorrow_start
        ]

    def _get_future_slots(self) -> list[Any]:
        """Return all price slots from now onwards."""
        if not self.coordinator.data:
            return []
        home = self.coordinator.get_first_home_name()
        if not home:
            return []
        now = dt_util.now()
        return [
            s
            for s in self.coordinator.data.get(home, [])
            if s.start_time + timedelta(minutes=15) > now
        ]


class ZeusTodayAveragePriceSensor(_ZeusPriceSensorBase):
    """Average energy price across all of today's slots."""

    _attr_translation_key = "today_average_price"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the today average price sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_today_average_price"
        self._update_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        slots = self._get_today_slots()
        if slots:
            self._attr_native_value = sum(s.price for s in slots) / len(slots)
            self._attr_extra_state_attributes = {"slot_count": len(slots)}
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class ZeusTodayMinPriceSensor(_ZeusPriceSensorBase):
    """Lowest energy price today (with time attribute)."""

    _attr_translation_key = "today_min_price"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the today min price sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_today_min_price"
        self._update_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        slots = self._get_today_slots()
        if slots:
            cheapest = min(slots, key=lambda s: s.price)
            self._attr_native_value = cheapest.price
            self._attr_extra_state_attributes = {
                "slot_start": cheapest.start_time.isoformat(),
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class ZeusTodayMaxPriceSensor(_ZeusPriceSensorBase):
    """Highest energy price today (with time attribute)."""

    _attr_translation_key = "today_max_price"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the today max price sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_today_max_price"
        self._update_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        slots = self._get_today_slots()
        if slots:
            most_expensive = max(slots, key=lambda s: s.price)
            self._attr_native_value = most_expensive.price
            self._attr_extra_state_attributes = {
                "slot_start": most_expensive.start_time.isoformat(),
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


class ZeusCheapestUpcomingPriceSensor(_ZeusPriceSensorBase):
    """Cheapest upcoming slot price (with time attribute)."""

    _attr_translation_key = "cheapest_upcoming_price"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the cheapest upcoming price sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_cheapest_upcoming_price"
        self._update_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        slots = self._get_future_slots()
        if slots:
            cheapest = min(slots, key=lambda s: s.price)
            self._attr_native_value = cheapest.price
            self._attr_extra_state_attributes = {
                "slot_start": cheapest.start_time.isoformat(),
            }
        else:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}


# ---------------------------------------------------------------------------
# Per-device sensors
# ---------------------------------------------------------------------------


class ZeusDeviceRuntimeTodaySensor(CoordinatorEntity[PriceCoordinator], SensorEntity):
    """Minutes a managed device has run today (from scheduler results)."""

    _attr_has_entity_name = True
    _attr_translation_key = "device_runtime_today"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the device runtime today sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_runtime_today"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Switch Device"
        self._attr_translation_placeholders = {"device_name": device_name}

        switch_entity = subentry.data[CONF_SWITCH_ENTITY] if subentry else ""
        self._attr_device_info = _get_device_info_for_switch(
            hass, switch_entity, entry, subentry_id, device_name
        )

        # Initial value from schedule results
        result = coordinator.schedule_results.get(subentry_id)
        if result:
            daily = float(subentry.data.get("daily_runtime", 0) if subentry else 0)
            self._attr_native_value = round(daily - result.remaining_runtime_min, 1)
        else:
            self._attr_native_value = None

    @callback
    def _handle_coordinator_update(self) -> None:
        result = self.coordinator.schedule_results.get(self._subentry_id)
        if result:
            subentry = self._entry.subentries.get(self._subentry_id)
            daily = float(subentry.data.get("daily_runtime", 0) if subentry else 0)
            self._attr_native_value = round(
                max(0.0, daily - result.remaining_runtime_min), 1
            )
        else:
            self._attr_native_value = None
        self.async_write_ha_state()


def _get_device_info_for_switch(
    hass: HomeAssistant,
    entity_id: str,
    entry: ConfigEntry,
    subentry_id: str,
    device_name: str,
) -> DeviceInfo:
    """
    Get device info for the target switch entity.

    Mirrors the logic in binary_sensor.py — if the entity belongs to an
    existing device, link to that device. Otherwise use a Zeus-managed device.
    """
    ent_reg = er.async_get(hass)

    ent_entry = ent_reg.async_get(entity_id)
    if ent_entry and ent_entry.device_id:
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get(ent_entry.device_id)
        if device and device.identifiers:
            return DeviceInfo(identifiers=device.identifiers)

    # Fallback: Zeus-managed device
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
        name=device_name,
        manufacturer="Zeus",
        entry_type=DeviceEntryType.SERVICE,
    )


class ZeusThermostatRuntimeTodaySensor(
    CoordinatorEntity[PriceCoordinator], SensorEntity
):
    """Minutes a thermostat device has been heating today."""

    _attr_has_entity_name = True
    _attr_translation_key = "thermostat_runtime_today"
    _attr_native_unit_of_measurement = "min"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the thermostat runtime today sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = (
            f"{entry.entry_id}_{subentry_id}_thermostat_runtime_today"
        )

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Thermostat Device"
        self._attr_translation_placeholders = {
            "device_name": device_name,
        }

        switch_entity = subentry.data[CONF_SWITCH_ENTITY] if subentry else ""
        self._attr_device_info = _get_device_info_for_switch(
            hass, switch_entity, entry, subentry_id, device_name
        )

        self._attr_native_value = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update runtime from scheduler (thermostat tracks switch on-time)."""
        # Thermostat results don't have remaining_runtime_min in the same
        # way switch devices do, but we can query the switch's on-time
        # through the scheduler. For now, this sensor updates when the
        # coordinator provides new schedule results. The actual runtime
        # tracking happens via the recorder in the scheduler module.
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Manual device sensors
# ---------------------------------------------------------------------------

_MAX_RANKED_WINDOWS = 10


class ZeusManualDeviceRecommendationSensor(
    CoordinatorEntity[PriceCoordinator], SensorEntity
):
    """Sensor showing the recommended start time for a manual device cycle."""

    _attr_has_entity_name = True
    _attr_translation_key = "manual_device_recommendation"

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
    ) -> None:
        """Initialize the manual device recommendation sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_manual_recommendation"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Manual Device"
        self._attr_translation_placeholders = {"device_name": device_name}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
            name=device_name,
            manufacturer="Zeus",
            entry_type=DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_state()
        self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        """Update sensor state from coordinator ranking results."""
        ranking = self.coordinator.manual_device_results.get(self._subentry_id)
        if ranking and ranking.recommended_start:
            self._attr_native_value = ranking.recommended_start.strftime("%H:%M")
        else:
            self._attr_native_value = None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes with ranking details."""
        ranking = self.coordinator.manual_device_results.get(self._subentry_id)
        reservation = self.coordinator.get_reservation(self._subentry_id)

        subentry = self._entry.subentries.get(self._subentry_id)
        cycle_duration_min = (
            float(subentry.data.get(CONF_CYCLE_DURATION, 0)) if subentry else 0.0
        )
        peak_usage_w = float(subentry.data.get(CONF_PEAK_USAGE, 0)) if subentry else 0.0

        has_delay_intervals = bool(subentry and subentry.data.get(CONF_DELAY_INTERVALS))
        dynamic_cycle = bool(
            subentry and subentry.data.get(CONF_DYNAMIC_CYCLE_DURATION, False)
        )

        # Resolve the number entity_id for cycle duration (if dynamic)
        number_entity_id: str | None = None
        if dynamic_cycle and self.hass is not None:
            ent_reg = er.async_get(self.hass)
            number_uid = (
                f"{self._entry.entry_id}_{self._subentry_id}_manual_cycle_duration"
            )
            number_entry = ent_reg.async_get_entity_id("number", DOMAIN, number_uid)
            if number_entry:
                number_entity_id = number_entry

        attrs: dict[str, Any] = {
            "subentry_id": self._subentry_id,
            "cycle_duration_min": cycle_duration_min,
            "peak_usage_w": peak_usage_w,
            "dynamic_cycle_duration": dynamic_cycle,
            "number_entity_id": number_entity_id,
            "has_delay_intervals": has_delay_intervals,
            "reserved": reservation is not None,
            "reservation_start": reservation[0].isoformat() if reservation else None,
            "reservation_end": reservation[1].isoformat() if reservation else None,
        }

        if ranking:
            attrs["recommended_start"] = (
                ranking.recommended_start.isoformat()
                if ranking.recommended_start
                else None
            )
            attrs["recommended_end"] = (
                ranking.recommended_end.isoformat() if ranking.recommended_end else None
            )

            if ranking.windows:
                best = ranking.windows[0]
                attrs["estimated_cost"] = round(best.total_cost, 4)
                attrs["delay_hours"] = best.delay_hours

                # Compute cost_if_now: what it would cost to start right now
                cost_now = self._compute_cost_if_now(ranking)
                attrs["cost_if_now"] = (
                    round(cost_now, 4) if cost_now is not None else None
                )

                if cost_now is not None and cost_now > 0:
                    savings = ((cost_now - best.total_cost) / cost_now) * 100.0
                    attrs["savings_pct"] = round(max(0.0, savings), 1)
                else:
                    attrs["savings_pct"] = None

                # Top ranked windows
                attrs["ranked_windows"] = [
                    {
                        "start": w.start_time.isoformat(),
                        "end": w.end_time.isoformat(),
                        "cost": round(w.total_cost, 4),
                        "solar_pct": round(w.solar_fraction * 100.0, 1),
                        **(
                            {"delay_hours": w.delay_hours}
                            if w.delay_hours is not None
                            else {}
                        ),
                    }
                    for w in ranking.windows[:_MAX_RANKED_WINDOWS]
                ]
            else:
                attrs["estimated_cost"] = None
                attrs["delay_hours"] = None
                attrs["cost_if_now"] = None
                attrs["savings_pct"] = None
                attrs["ranked_windows"] = []
        else:
            attrs["recommended_start"] = None
            attrs["recommended_end"] = None
            attrs["estimated_cost"] = None
            attrs["delay_hours"] = None
            attrs["cost_if_now"] = None
            attrs["savings_pct"] = None
            attrs["ranked_windows"] = []

        return attrs

    def _compute_cost_if_now(self, ranking: Any) -> float | None:
        """Find the window starting closest to now (first window chronologically)."""
        if not ranking.windows:
            return None
        now = dt_util.now()
        # Find the first window whose start is >= now (or the very first one)
        for window in ranking.windows:
            if window.start_time >= now:
                # The earliest possible window — but this may not start *now*
                pass
        # The cost_if_now is the cost of the window starting at the current slot
        # Find the window with the earliest start_time
        earliest = min(ranking.windows, key=lambda w: w.start_time)
        if earliest.start_time <= now + timedelta(minutes=15):
            return earliest.total_cost
        return None
