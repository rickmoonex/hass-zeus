"""Number platform for Zeus manual device cycle duration."""

from __future__ import annotations

import contextlib
import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CYCLE_DURATION,
    CONF_DYNAMIC_CYCLE_DURATION,
    DOMAIN,
    SUBENTRY_MANUAL_DEVICE,
)
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus number entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_MANUAL_DEVICE and subentry.data.get(
            CONF_DYNAMIC_CYCLE_DURATION, False
        ):
            async_add_entities(
                [
                    ZeusManualDeviceCycleDuration(
                        coordinator, entry, subentry.subentry_id
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )


class ZeusManualDeviceCycleDuration(
    CoordinatorEntity[PriceCoordinator], NumberEntity, RestoreEntity
):
    """
    Number entity for adjustable cycle duration of a manual device.

    The configured value in the subentry serves as the default. The user
    can change this per-run (e.g. selecting a different dishwasher program).
    The value persists across HA restarts via RestoreEntity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "manual_device_cycle_duration"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_max_value = 1440
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
    ) -> None:
        """Initialize the cycle duration number entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_manual_cycle_duration"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Manual Device"
        self._attr_translation_placeholders = {"device_name": device_name}

        self._default_duration = (
            float(subentry.data.get(CONF_CYCLE_DURATION, 60)) if subentry else 60.0
        )
        self._attr_native_value = self._default_duration

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
            name=device_name,
            manufacturer="Zeus",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Restore previous value when entity is added to HA."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            "unknown",
            "unavailable",
        ):
            with contextlib.suppress(ValueError, TypeError):
                self._attr_native_value = float(last_state.state)

    async def async_set_native_value(self, value: float) -> None:
        """Set a new cycle duration and trigger scheduler rerun."""
        self._attr_native_value = value
        self.async_write_ha_state()
        # Trigger scheduler rerun so rankings update immediately
        await self.coordinator.async_run_scheduler()
        if self.coordinator.data is not None:
            self.coordinator.async_set_updated_data(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        """No-op — the number value is user-driven, not coordinator-driven."""
