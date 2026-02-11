"""The Zeus integration."""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback

from .const import CONF_ENERGY_PROVIDER, DOMAIN
from .coordinator import PriceCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

SERVICE_SET_PRICE_OVERRIDE = "set_price_override"
SERVICE_CLEAR_PRICE_OVERRIDE = "clear_price_override"
SERVICE_RUN_SCHEDULER = "run_scheduler"

SERVICE_SET_PRICE_OVERRIDE_SCHEMA = vol.Schema(
    {
        vol.Required("price"): vol.Coerce(float),
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Zeus from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    provider = entry.data.get(CONF_ENERGY_PROVIDER, "tibber")

    coordinator = PriceCoordinator(hass, entry, provider)
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_run_scheduler()

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _async_register_services(hass)

    # Reload the entry when subentries are added or removed so that
    # new entities are created and removed entities are cleaned up.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (e.g. subentry added/removed)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    # Remove services when no entries remain
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_SET_PRICE_OVERRIDE)
        hass.services.async_remove(DOMAIN, SERVICE_CLEAR_PRICE_OVERRIDE)
        hass.services.async_remove(DOMAIN, SERVICE_RUN_SCHEDULER)

    return unload_ok


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    """Register Zeus services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_PRICE_OVERRIDE):
        return

    def _get_coordinators() -> list[PriceCoordinator]:
        """Get all active Zeus coordinators."""
        return list(hass.data.get(DOMAIN, {}).values())

    async def async_handle_set_price_override(call: ServiceCall) -> None:
        """Handle the set_price_override service call."""
        price = call.data["price"]
        for coordinator in _get_coordinators():
            coordinator.async_set_price_override(price)

    async def async_handle_clear_price_override(call: ServiceCall) -> None:  # noqa: ARG001
        """Handle the clear_price_override service call."""
        for coordinator in _get_coordinators():
            coordinator.async_clear_price_override()

    async def async_handle_run_scheduler(call: ServiceCall) -> None:  # noqa: ARG001
        """Handle the run_scheduler service call."""
        for coordinator in _get_coordinators():
            await coordinator.async_run_scheduler()
            if coordinator.data is not None:
                coordinator.async_set_updated_data(coordinator.data)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PRICE_OVERRIDE,
        async_handle_set_price_override,
        schema=SERVICE_SET_PRICE_OVERRIDE_SCHEMA,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_PRICE_OVERRIDE,
        async_handle_clear_price_override,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_RUN_SCHEDULER,
        async_handle_run_scheduler,
    )
