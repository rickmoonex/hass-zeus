"""Switch platform for Zeus energy management."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PriceCoordinator


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
    """Set up Zeus switch entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ZeusMasterSwitch(coordinator, entry)])


class ZeusMasterSwitch(CoordinatorEntity[PriceCoordinator], SwitchEntity):
    """Master switch to enable or disable all Zeus management."""

    _attr_has_entity_name = True
    _attr_translation_key = "master_switch"

    def __init__(self, coordinator: PriceCoordinator, entry: ConfigEntry) -> None:
        """Initialize the master switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_master_switch"
        self._attr_device_info = _device_info(entry)
        self._attr_is_on = coordinator.enabled

    @property
    def icon(self) -> str:
        """Return the icon."""
        return "mdi:power" if self.is_on else "mdi:power-off"

    async def async_turn_on(self, **_kwargs: Any) -> None:
        """Turn on Zeus management."""
        self.coordinator.async_set_enabled(enabled=True)
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **_kwargs: Any) -> None:
        """Turn off Zeus management."""
        self.coordinator.async_set_enabled(enabled=False)
        self._attr_is_on = False
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Sync state with coordinator."""
        self._attr_is_on = self.coordinator.enabled
        self.async_write_ha_state()
