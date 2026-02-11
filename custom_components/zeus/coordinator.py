"""DataUpdateCoordinator for Zeus energy price data."""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_PRODUCTION_ENTITY,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
)

if TYPE_CHECKING:
    from .scheduler import ScheduleResult

_LOGGER = logging.getLogger(__name__)

PRICE_UPDATE_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True)
class PriceSlot:
    """Represents a single energy price time slot."""

    start_time: datetime
    price: float


@dataclass
class PriceData:
    """Container for cached energy price data."""

    home_name: str
    slots: list[PriceSlot]


class PriceCoordinator(DataUpdateCoordinator[dict[str, list[PriceSlot]]]):
    """
    Coordinator to fetch and cache energy prices.

    Fetches new price data from the energy provider every hour and
    re-evaluates the current slot at every 15-minute boundary so
    sensors always reflect the active price window.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, provider: str) -> None:
        """Initialize the price coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_price_coordinator",
            update_interval=PRICE_UPDATE_INTERVAL,
            config_entry=entry,
        )
        self.provider = provider
        self._cached_slots: dict[str, dict[datetime, PriceSlot]] = {}
        self._slot_unsub: CALLBACK_TYPE | None = None
        self._solar_unsub: CALLBACK_TYPE | None = None
        self._price_override: float | None = None
        self.schedule_results: dict[str, ScheduleResult] = {}

    @callback
    def _async_start_slot_timer(self) -> None:
        """Start the 15-minute slot boundary timer."""
        if self._slot_unsub is not None:
            return

        @callback
        def _on_slot_boundary(_now: datetime) -> None:
            """Re-notify all listeners at each 15-minute boundary."""
            if self.data is not None:
                self.hass.async_create_task(self._async_slot_update())

        # Fire at second 0 of minutes 0, 15, 30, 45
        self._slot_unsub = async_track_utc_time_change(
            self.hass, _on_slot_boundary, minute=(0, 15, 30, 45), second=0
        )

    @callback
    def _async_stop_slot_timer(self) -> None:
        """Stop the 15-minute slot boundary timer."""
        if self._slot_unsub is not None:
            self._slot_unsub()
            self._slot_unsub = None

    @callback
    def _async_start_solar_listener(self) -> None:
        """Listen for solar production changes to trigger scheduler reruns."""
        if self._solar_unsub is not None or self.config_entry is None:
            return

        # Collect production entity IDs from solar inverter subentries
        entity_ids: list[str] = []
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_SOLAR_INVERTER:
                entity_id = subentry.data.get(CONF_PRODUCTION_ENTITY)
                if entity_id:
                    entity_ids.append(entity_id)

        if not entity_ids or not self._has_switch_devices():
            return

        @callback
        def _on_solar_change(
            _event: Event[EventStateChangedData],
        ) -> None:
            """Rerun scheduler when solar production changes."""
            self.hass.async_create_task(self._async_slot_update())

        self._solar_unsub = async_track_state_change_event(
            self.hass, entity_ids, _on_solar_change
        )

    @callback
    def _async_stop_solar_listener(self) -> None:
        """Stop listening for solar production changes."""
        if self._solar_unsub is not None:
            self._solar_unsub()
            self._solar_unsub = None

    async def _async_slot_update(self) -> None:
        """Run scheduler and re-notify listeners on 15-min boundary."""
        await self.async_run_scheduler()
        if self.data is not None:
            self.async_set_updated_data(self.data)

    async def _async_update_data(self) -> dict[str, list[PriceSlot]]:
        """Fetch price data from the energy provider."""
        if self.provider == ENERGY_PROVIDER_TIBBER:
            result = await self._fetch_tibber_prices()
        else:
            msg = f"Unsupported energy provider: {self.provider}"
            raise UpdateFailed(msg)

        # Start the slot timer and solar listener after the first successful fetch
        self._async_start_slot_timer()
        self._async_start_solar_listener()
        return result

    async def _fetch_tibber_prices(self) -> dict[str, list[PriceSlot]]:
        """Fetch prices from the Tibber integration."""
        try:
            response: dict[str, Any] | None = await self.hass.services.async_call(
                "tibber",
                "get_prices",
                blocking=True,
                return_response=True,
            )
        except Exception as err:
            msg = f"Failed to fetch Tibber prices: {err}"
            raise UpdateFailed(msg) from err

        if not response or "prices" not in response:
            msg = "No price data received from Tibber"
            raise UpdateFailed(msg)

        prices_data: dict[str, list[dict[str, Any]]] = response["prices"]
        now = dt_util.now()

        for home_name, slots in prices_data.items():
            if home_name not in self._cached_slots:
                self._cached_slots[home_name] = {}

            cache = self._cached_slots[home_name]

            for slot_data in slots:
                start_time = dt_util.parse_datetime(slot_data["start_time"])
                if start_time is None:
                    continue

                # Only add new slots; existing ones are immutable
                if start_time not in cache:
                    cache[start_time] = PriceSlot(
                        start_time=start_time,
                        price=float(slot_data["price"]),
                    )

            # Prune slots older than 1 hour ago
            cutoff = now - timedelta(hours=1)
            expired = [ts for ts in cache if ts < cutoff]
            for ts in expired:
                del cache[ts]

        # Build result from cache, sorted by start time
        result: dict[str, list[PriceSlot]] = {}
        for home_name, cache in self._cached_slots.items():
            result[home_name] = sorted(cache.values(), key=lambda s: s.start_time)

        return result

    def get_first_home_name(self) -> str | None:
        """Get the name of the first home in the price data."""
        if not self.data:
            return None
        return next(iter(self.data), None)

    def get_current_slot(self) -> PriceSlot | None:
        """Get the price slot for the current 15-minute window."""
        home = self.get_first_home_name()
        if not home or not self.data:
            return None

        now = dt_util.now()
        slots = self.data.get(home, [])

        for slot in slots:
            slot_end = slot.start_time + timedelta(minutes=15)
            if slot.start_time <= now < slot_end:
                return slot

        return None

    def get_current_price(self) -> float | None:
        """Get the current energy price (respects override)."""
        if self._price_override is not None:
            return self._price_override
        slot = self.get_current_slot()
        return slot.price if slot else None

    def is_price_negative(self) -> bool:
        """Check if the current price is negative (you pay to export)."""
        price = self.get_current_price()
        if price is None:
            return False
        return price < 0

    @property
    def price_override(self) -> float | None:
        """Return the current price override, or None if not set."""
        return self._price_override

    @callback
    def async_set_price_override(self, price: float) -> None:
        """Override the current energy price for testing."""
        self._price_override = price
        _LOGGER.info("Price override set to %s EUR/kWh", price)
        if self.data is not None:
            self.async_set_updated_data(self.data)

    @callback
    def async_clear_price_override(self) -> None:
        """Clear the price override, reverting to real prices."""
        self._price_override = None
        _LOGGER.info("Price override cleared")
        if self.data is not None:
            self.async_set_updated_data(self.data)

    def get_next_slot(self) -> PriceSlot | None:
        """Get the next 15-minute price slot."""
        home = self.get_first_home_name()
        if not home or not self.data:
            return None

        current_slot = self.get_current_slot()
        if current_slot is None:
            return None

        current_end = current_slot.start_time + timedelta(minutes=15)
        slots = self.data.get(home, [])

        for slot in slots:
            if slot.start_time >= current_end:
                return slot

        return None

    def get_next_slot_price(self) -> float | None:
        """Get the price for the next 15-minute slot."""
        slot = self.get_next_slot()
        return slot.price if slot else None

    def _has_switch_devices(self) -> bool:
        """Check if any switch device subentries are configured."""
        if self.config_entry is None:
            return False
        return any(
            s.subentry_type == SUBENTRY_SWITCH_DEVICE
            for s in self.config_entry.subentries.values()
        )

    async def async_run_scheduler(self) -> None:
        """
        Run the scheduler and store results.

        Called automatically on each coordinator update and can also
        be triggered manually via the zeus.run_scheduler service.
        """
        if not self._has_switch_devices() or self.config_entry is None:
            self.schedule_results = {}
            return

        scheduler = importlib.import_module(".scheduler", __package__)

        try:
            self.schedule_results = await scheduler.async_run_scheduler(
                self.hass, self.config_entry, self
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Scheduler run failed", exc_info=True)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and clean up timers."""
        self._async_stop_slot_timer()
        self._async_stop_solar_listener()
        await super().async_shutdown()
