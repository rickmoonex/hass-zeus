"""Button platform for Zeus manual device reservation."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SUBENTRY_MANUAL_DEVICE
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zeus button entities."""
    coordinator: PriceCoordinator = hass.data[DOMAIN][entry.entry_id]

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_MANUAL_DEVICE:
            async_add_entities(
                [
                    ZeusManualDeviceReserveButton(
                        coordinator, entry, subentry.subentry_id
                    )
                ],
                config_subentry_id=subentry.subentry_id,
            )


class ZeusManualDeviceReserveButton(CoordinatorEntity[PriceCoordinator], ButtonEntity):
    """Button to reserve the recommended time slot for a manual device."""

    _attr_has_entity_name = True
    _attr_translation_key = "manual_device_reserve"

    def __init__(
        self,
        coordinator: PriceCoordinator,
        entry: ConfigEntry,
        subentry_id: str,
    ) -> None:
        """Initialize the manual device reserve button."""
        super().__init__(coordinator)
        self._entry = entry
        self._subentry_id = subentry_id
        self._attr_unique_id = f"{entry.entry_id}_{subentry_id}_manual_reserve"

        subentry = entry.subentries.get(subentry_id)
        device_name = subentry.title if subentry else "Manual Device"
        self._attr_translation_placeholders = {"device_name": device_name}

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_{subentry_id}")},
            name=device_name,
            manufacturer="Zeus",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_press(self) -> None:
        """Reserve the recommended time slot."""
        await self.coordinator.async_reserve_manual_device(self._subentry_id)
