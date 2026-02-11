"""Binary sensor platform for Zeus energy management."""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_MIN_CYCLE_TIME,
    CONF_PEAK_USAGE,
    CONF_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_SWITCH_ENTITY,
    DOMAIN,
    SUBENTRY_SWITCH_DEVICE,
)
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus binary sensor entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Global binary sensors (not linked to a subentry)
    async_add_entities(
        [ZeusNegativePriceSensor(coordinator, entry)],
    )

    # Per-switch-device binary sensors (linked to their subentry)
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_SWITCH_DEVICE:
            async_add_entities(
                [
                    ZeusDeviceScheduleSensor(
                        coordinator, entry, subentry.subentry_id, hass
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )


class ZeusNegativePriceSensor(CoordinatorEntity[PriceCoordinator], BinarySensorEntity):
    """
    Binary sensor that is on when the energy price is negative.

    When this sensor is ON, it means you would PAY for electricity
    you deliver to the grid. This is the trigger for reducing
    inverter output to avoid grid export.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "negative_energy_price"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the negative price binary sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_negative_energy_price"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Zeus Energy Manager",
            manufacturer="Zeus",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._attr_is_on = self.coordinator.is_energy_price_negative()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._attr_is_on = self.coordinator.is_energy_price_negative()
        self.async_write_ha_state()


def _get_device_info_for_entity(
    hass: HomeAssistant,
    entity_id: str,
    entry: ConfigEntry,
    subentry_id: str,
    device_name: str,
) -> DeviceInfo:
    """
    Get device info for the target switch entity.

    If the entity belongs to a device, return DeviceInfo linking to that
    device. Otherwise create a Zeus-managed device named after the subentry
    and move the orphan switch entity onto it.
    """
    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    ent_entry = ent_reg.async_get(entity_id)
    if ent_entry and ent_entry.device_id:
        device = dev_reg.async_get(ent_entry.device_id)
        if device and device.identifiers:
            return DeviceInfo(identifiers=device.identifiers)

    # No device found — create a Zeus-managed device for this entity
    identifiers = {(DOMAIN, f"{entry.entry_id}_{subentry_id}")}
    device_info = DeviceInfo(
        identifiers=identifiers,
        name=device_name,
        manufacturer="Zeus",
        entry_type=DeviceEntryType.SERVICE,
    )

    # Pre-create the device so we can move the orphan switch onto it
    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers=identifiers,
        name=device_name,
        manufacturer="Zeus",
        entry_type=DeviceEntryType.SERVICE,
    )

    # Move the orphan switch entity onto this device
    if ent_entry and not ent_entry.device_id:
        ent_reg.async_update_entity(entity_id, device_id=device.id)

    return device_info


class ZeusDeviceScheduleSensor(CoordinatorEntity[PriceCoordinator], BinarySensorEntity):
    """
    Binary sensor reporting whether Zeus wants a managed device on.

    ON means the scheduler has determined this is an optimal time slot
    for the device to run. The sensor also directly controls the
    underlying switch entity to match the schedule decision.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "device_schedule"

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the device schedule binary sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_device_schedule"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Switch Device"
        self._attr_translation_placeholders = {"device_name": device_name}

        switch_entity = subentry.data[CONF_SWITCH_ENTITY] if subentry else ""
        self._attr_device_info = _get_device_info_for_entity(
            hass, switch_entity, entry, subentry_id, device_name
        )

        # Read initial state from coordinator results (populated during setup)
        result = coordinator.schedule_results.get(subentry_id)
        self._attr_is_on = result.should_be_on if result else False

        # Track when the switch last changed state for min_cycle_time enforcement
        self._last_switch_change: datetime | None = None
        self._current_switch_state: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Apply initial switch control when entity is added to HA."""
        await super().async_added_to_hass()
        # Sync the underlying switch to match the initial schedule decision
        if self._attr_is_on is not None:
            switch_entity = self._subentry_data.get(CONF_SWITCH_ENTITY)
            if switch_entity:
                await self._async_control_switch(
                    switch_entity, turn_on=self._attr_is_on
                )

    @property
    def _subentry_data(self) -> dict[str, Any]:
        """Get the subentry data for this device."""
        subentry = self._entry.subentries.get(self._subentry_id)
        if subentry is None:
            return {}
        return dict(subentry.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        data = self._subentry_data
        power_sensor = data.get(CONF_POWER_SENSOR)
        current_usage_w: float | None = None
        if power_sensor and self.hass is not None:
            state = self.hass.states.get(power_sensor)
            if state and state.state not in ("unknown", "unavailable"):
                with contextlib.suppress(ValueError, TypeError):
                    current_usage_w = float(state.state)

        min_cycle = self._get_min_cycle_time_min()
        attrs: dict[str, Any] = {
            "managed_entity": data.get(CONF_SWITCH_ENTITY),
            "power_sensor": power_sensor,
            "current_usage_w": current_usage_w,
            "peak_usage_w": data.get(CONF_PEAK_USAGE),
            "daily_runtime_min": data.get(CONF_DAILY_RUNTIME),
            "deadline": data.get(CONF_DEADLINE),
            "priority": data.get(CONF_PRIORITY),
            "min_cycle_time_min": min_cycle,
            "cycle_locked": self._is_cycle_locked(desired_on=not self._attr_is_on)
            if self._attr_is_on is not None
            else False,
        }

        result = self.coordinator.schedule_results.get(self._subentry_id)
        if result:
            attrs["remaining_runtime_min"] = round(result.remaining_runtime_min, 1)
            attrs["schedule_reason"] = result.reason
            attrs["scheduled_slots"] = [s.isoformat() for s in result.scheduled_slots]

        return attrs

    def _get_min_cycle_time_min(self) -> float:
        """Get the minimum cycle time in minutes from config."""
        return float(self._subentry_data.get(CONF_MIN_CYCLE_TIME, 0))

    def _is_cycle_locked(self, *, desired_on: bool) -> bool:
        """
        Check if switching is blocked by minimum cycle time.

        Returns True if the device must hold its current state because
        insufficient time has passed since the last state change.
        """
        min_cycle = self._get_min_cycle_time_min()
        if min_cycle <= 0:
            return False

        if self._last_switch_change is None or self._current_switch_state is None:
            return False

        # Only relevant when we want to CHANGE state
        if desired_on == self._current_switch_state:
            return False

        elapsed_min = (
            dt_util.utcnow() - self._last_switch_change
        ).total_seconds() / 60.0
        if elapsed_min < min_cycle:
            _LOGGER.debug(
                "Cycle lock: %s must stay %s for %.1f more min (min_cycle=%.0f)",
                self._subentry_data.get(CONF_SWITCH_ENTITY),
                "on" if self._current_switch_state else "off",
                min_cycle - elapsed_min,
                min_cycle,
            )
            return True

        return False

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update — apply schedule result and sync switch."""
        # When Zeus is disabled, turn off all managed devices
        if not self.coordinator.enabled:
            if self._attr_is_on:
                self._attr_is_on = False
                switch_entity = self._subentry_data.get(CONF_SWITCH_ENTITY)
                if switch_entity:
                    self.hass.async_create_task(
                        self._async_control_switch(switch_entity, turn_on=False)
                    )
            self.async_write_ha_state()
            return

        result = self.coordinator.schedule_results.get(self._subentry_id)
        if result is None:
            return

        desired_on = result.should_be_on

        # Enforce minimum cycle time — hold current state if locked
        if self._is_cycle_locked(desired_on=desired_on):
            # Keep the binary sensor showing the actual (held) state
            self.async_write_ha_state()
            return

        self._attr_is_on = desired_on

        # Always sync the underlying switch to match the schedule decision.
        # This ensures the switch is corrected even if it was manually changed.
        switch_entity = self._subentry_data.get(CONF_SWITCH_ENTITY)
        if switch_entity:
            self.hass.async_create_task(
                self._async_control_switch(switch_entity, turn_on=desired_on)
            )

        self.async_write_ha_state()

    async def _async_control_switch(self, entity_id: str, *, turn_on: bool) -> None:
        """Turn on or off the underlying switch entity."""
        # Track the state change for min_cycle_time enforcement
        if self._current_switch_state != turn_on:
            self._last_switch_change = dt_util.utcnow()
            self._current_switch_state = turn_on

        domain = entity_id.split(".", maxsplit=1)[0]
        service = "turn_on" if turn_on else "turn_off"
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entity_id}, blocking=True
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Failed to %s %s", service, entity_id, exc_info=True)
