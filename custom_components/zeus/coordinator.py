"""DataUpdateCoordinator for Zeus energy price data."""

from __future__ import annotations

import contextlib
import importlib
import logging
import math
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
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_utc_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_CYCLE_DURATION,
    CONF_POWER_SENSOR,
    CONF_PRODUCTION_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    DOMAIN,
    ENERGY_PROVIDER_TIBBER,
    SLOT_DURATION_MIN,
    SUBENTRY_MANUAL_DEVICE,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
    SUBENTRY_THERMOSTAT_DEVICE,
)
from .thermal_model import ThermalTracker
from .tibber_api import TibberApiClient, TibberApiError

if TYPE_CHECKING:
    from .scheduler import ManualDeviceRanking, ScheduleResult

_LOGGER = logging.getLogger(__name__)

PRICE_UPDATE_INTERVAL = timedelta(hours=1)
FORECAST_CACHE_TTL = timedelta(hours=1)
THERMAL_STORAGE_KEY = "zeus_thermal_trackers"
THERMAL_STORAGE_VERSION = 1

RESERVATION_STORAGE_KEY = "zeus_manual_reservations"
RESERVATION_STORAGE_VERSION = 1


@dataclass(frozen=True)
class PriceSlot:
    """
    Represents a single energy price time slot.

    Attributes:
        start_time: Start of the 15-minute slot.
        price: Total price (energy + tax) — what you pay for consumption.
        energy_price: Energy-only price — what you receive/pay for grid export.

    """

    start_time: datetime
    price: float
    energy_price: float


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
        self._temp_unsub: CALLBACK_TYPE | None = None
        self._price_override: float | None = None
        self.schedule_results: dict[str, ScheduleResult] = {}
        self._scheduler_module: Any | None = None
        self._enabled: bool = True
        self._tibber_client: TibberApiClient | None = None
        self._thermal_trackers: dict[str, ThermalTracker] = {}
        self._thermal_store: Store[dict[str, Any]] = Store(
            hass, THERMAL_STORAGE_VERSION, THERMAL_STORAGE_KEY
        )
        self._thermal_unsub: CALLBACK_TYPE | None = None
        self._manual_reservations: dict[str, tuple[datetime, datetime]] = {}
        self._reservation_store: Store[dict[str, Any]] = Store(
            hass, RESERVATION_STORAGE_VERSION, RESERVATION_STORAGE_KEY
        )
        self.manual_device_results: dict[str, ManualDeviceRanking] = {}
        self.solar_forecast: dict[str, float] | None = None
        self._forecast_cache: dict[str, float] | None = None
        self._forecast_cache_time: datetime | None = None

    def _get_tibber_client(self) -> TibberApiClient:
        """Get or create the Tibber API client."""
        if self._tibber_client is None:
            if self.config_entry is None:
                msg = "No config entry available"
                raise UpdateFailed(msg)
            access_token = self.config_entry.data.get(CONF_ACCESS_TOKEN)
            if not access_token:
                msg = "No Tibber access token configured"
                raise UpdateFailed(msg)
            session = async_get_clientsession(self.hass)
            self._tibber_client = TibberApiClient(session, access_token)
        return self._tibber_client

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

        if not entity_ids or not self._has_managed_devices():
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

    @callback
    def _async_start_temperature_listener(self) -> None:
        """Listen for temperature changes to trigger thermostat re-evaluation."""
        if self._temp_unsub is not None or self.config_entry is None:
            return

        entity_ids: list[str] = []
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_type == SUBENTRY_THERMOSTAT_DEVICE:
                entity_id = subentry.data.get(CONF_TEMPERATURE_SENSOR)
                if entity_id:
                    entity_ids.append(entity_id)

        if not entity_ids:
            return

        @callback
        def _on_temp_change(
            _event: Event[EventStateChangedData],
        ) -> None:
            """Rerun scheduler when temperature changes."""
            self.hass.async_create_task(self._async_slot_update())

        self._temp_unsub = async_track_state_change_event(
            self.hass, entity_ids, _on_temp_change
        )

    @callback
    def _async_stop_temperature_listener(self) -> None:
        """Stop listening for temperature changes."""
        if self._temp_unsub is not None:
            self._temp_unsub()
            self._temp_unsub = None

    # ------------------------------------------------------------------
    # Thermal tracker management
    # ------------------------------------------------------------------

    async def async_restore_thermal_trackers(self) -> None:
        """Restore thermal tracker state from storage."""
        stored: dict[str, Any] | None = await self._thermal_store.async_load()
        if stored:
            for subentry_id, tracker_data in stored.items():
                self._thermal_trackers[subentry_id] = ThermalTracker.from_dict(
                    tracker_data
                )
            _LOGGER.debug(
                "Restored %d thermal trackers from storage",
                len(self._thermal_trackers),
            )

    async def async_save_thermal_trackers(self) -> None:
        """Persist thermal tracker state to storage."""
        data = {
            sid: tracker.to_dict() for sid, tracker in self._thermal_trackers.items()
        }
        await self._thermal_store.async_save(data)

    async def async_restore_reservations(self) -> None:
        """Restore manual device reservations from storage."""
        stored: dict[str, Any] | None = await self._reservation_store.async_load()
        if stored:
            now = dt_util.now()
            for sid, item in stored.items():
                try:
                    start = datetime.fromisoformat(item["start"])
                    end = datetime.fromisoformat(item["end"])
                    if end > now:  # Only restore non-expired reservations
                        self._manual_reservations[sid] = (start, end)
                except (KeyError, ValueError, TypeError):
                    continue
            _LOGGER.debug(
                "Restored %d manual reservations from storage",
                len(self._manual_reservations),
            )

    async def async_save_reservations(self) -> None:
        """Persist manual device reservations to storage."""
        data = {
            sid: {"start": start.isoformat(), "end": end.isoformat()}
            for sid, (start, end) in self._manual_reservations.items()
        }
        await self._reservation_store.async_save(data)

    def get_thermal_tracker(self, subentry_id: str) -> ThermalTracker | None:
        """Get the thermal tracker for a thermostat subentry."""
        return self._thermal_trackers.get(subentry_id)

    def _ensure_thermal_tracker(self, subentry_id: str) -> ThermalTracker:
        """Get or create a thermal tracker for a subentry."""
        if subentry_id not in self._thermal_trackers:
            self._thermal_trackers[subentry_id] = ThermalTracker()
        return self._thermal_trackers[subentry_id]

    @callback
    def _async_start_thermal_listener(self) -> None:
        """Listen for thermostat switch state changes to track heating sessions."""
        if self._thermal_unsub is not None or self.config_entry is None:
            return

        # Build a mapping: switch_entity_id -> (subentry_id, temp_sensor, power_sensor)
        switch_map: dict[str, tuple[str, str, str]] = {}
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_THERMOSTAT_DEVICE:
                continue
            sw = subentry.data.get(CONF_SWITCH_ENTITY)
            ts = subentry.data.get(CONF_TEMPERATURE_SENSOR)
            ps = subentry.data.get(CONF_POWER_SENSOR)
            if sw and ts and ps:
                switch_map[sw] = (subentry.subentry_id, ts, ps)

        if not switch_map:
            return

        @callback
        def _on_switch_change(
            event: Event[EventStateChangedData],
        ) -> None:
            """Track heater on/off transitions for thermal learning."""
            entity_id = event.data["entity_id"]
            old_state = event.data.get("old_state")
            new_state = event.data.get("new_state")

            if entity_id not in switch_map or old_state is None or new_state is None:
                return

            subentry_id, temp_sensor, power_sensor = switch_map[entity_id]
            tracker = self._ensure_thermal_tracker(subentry_id)
            now = dt_util.utcnow()

            # Read current temperature
            temp_state = self.hass.states.get(temp_sensor)
            current_temp: float | None = None
            if temp_state and temp_state.state not in ("unknown", "unavailable"):
                with contextlib.suppress(ValueError, TypeError):
                    current_temp = float(temp_state.state)

            was_on = old_state.state == "on"
            is_on = new_state.state == "on"

            if not was_on and is_on and current_temp is not None:
                # Heater turned ON
                tracker.on_heater_started(current_temp, now)
                _LOGGER.debug(
                    "Thermal session started for %s at %.1f°C",
                    entity_id,
                    current_temp,
                )

            elif was_on and not is_on and current_temp is not None:
                # Heater turned OFF — read average power
                avg_power: float = 0.0
                pw_state = self.hass.states.get(power_sensor)
                if pw_state and pw_state.state not in ("unknown", "unavailable"):
                    with contextlib.suppress(ValueError, TypeError):
                        avg_power = float(pw_state.state)

                tracker.on_heater_stopped(current_temp, avg_power, now)
                _LOGGER.debug(
                    "Thermal session ended for %s at %.1f°C (power=%.0fW)",
                    entity_id,
                    current_temp,
                    avg_power,
                )

                # Persist after each completed session
                self.hass.async_create_task(self.async_save_thermal_trackers())

        self._thermal_unsub = async_track_state_change_event(
            self.hass, list(switch_map.keys()), _on_switch_change
        )

    @callback
    def _async_stop_thermal_listener(self) -> None:
        """Stop listening for thermostat switch changes."""
        if self._thermal_unsub is not None:
            self._thermal_unsub()
            self._thermal_unsub = None

    # ------------------------------------------------------------------
    # Manual device reservation management
    # ------------------------------------------------------------------

    async def async_reserve_manual_device(
        self,
        subentry_id: str,
        start_time: datetime | None = None,
    ) -> None:
        """
        Reserve a time window for a manual device.

        If ``start_time`` is None, uses the #1 recommended window from the
        latest ranking results. Triggers a scheduler rerun so smart devices
        plan around the reservation.
        """
        if start_time is None:
            ranking = self.manual_device_results.get(subentry_id)
            if (
                not ranking
                or not ranking.recommended_start
                or not ranking.recommended_end
            ):
                _LOGGER.warning(
                    "Cannot reserve %s: no recommended window available", subentry_id
                )
                return
            start = ranking.recommended_start
            end = ranking.recommended_end
        else:
            # Find the matching request to compute end time from cycle_duration
            if self.config_entry is None:
                return
            subentry = self.config_entry.subentries.get(subentry_id)
            if subentry is None:
                return
            cycle_min = float(subentry.data.get(CONF_CYCLE_DURATION, 0))
            if cycle_min <= 0:
                return
            start = start_time
            slots_needed = math.ceil(cycle_min / SLOT_DURATION_MIN)
            end = start + timedelta(minutes=slots_needed * SLOT_DURATION_MIN)

        self._manual_reservations[subentry_id] = (start, end)
        _LOGGER.info(
            "Reserved manual device %s: %s -> %s",
            subentry_id,
            start.isoformat(),
            end.isoformat(),
        )

        await self.async_save_reservations()

        # Rerun scheduler so smart devices see the reservation
        await self.async_run_scheduler()
        if self.data is not None:
            self.async_set_updated_data(self.data)

    async def async_cancel_reservation(self, subentry_id: str) -> None:
        """Cancel a manual device reservation."""
        if subentry_id in self._manual_reservations:
            del self._manual_reservations[subentry_id]
            _LOGGER.info("Cancelled reservation for %s", subentry_id)
            await self.async_save_reservations()
            await self.async_run_scheduler()
            if self.data is not None:
                self.async_set_updated_data(self.data)

    def get_reservation(self, subentry_id: str) -> tuple[datetime, datetime] | None:
        """Get the active reservation for a manual device, or None."""
        return self._manual_reservations.get(subentry_id)

    def get_active_reservations(self) -> dict[str, tuple[datetime, datetime]]:
        """Return all non-expired reservations, pruning expired ones."""
        now = dt_util.now()
        expired = [
            sid for sid, (_, end) in self._manual_reservations.items() if end <= now
        ]
        for sid in expired:
            del self._manual_reservations[sid]
            _LOGGER.debug("Reservation expired for %s", sid)
        return dict(self._manual_reservations)

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

        # Start the slot timer and listeners after the first successful fetch
        self._async_start_slot_timer()
        self._async_start_solar_listener()
        self._async_start_temperature_listener()
        self._async_start_thermal_listener()
        return result

    async def _fetch_tibber_prices(self) -> dict[str, list[PriceSlot]]:
        """Fetch prices from the Tibber API using our own client."""
        client = self._get_tibber_client()

        try:
            homes = await client.async_get_prices()
        except TibberApiError as err:
            msg = f"Failed to fetch Tibber prices: {err}"
            raise UpdateFailed(msg) from err

        if not homes:
            msg = "No homes returned from Tibber API"
            raise UpdateFailed(msg)

        now = dt_util.now()

        for home_name, home in homes.items():
            if home_name not in self._cached_slots:
                self._cached_slots[home_name] = {}

            cache = self._cached_slots[home_name]

            for price_entry in home.prices:
                # Only add new slots; existing ones are immutable
                if price_entry.start_time not in cache:
                    cache[price_entry.start_time] = PriceSlot(
                        start_time=price_entry.start_time,
                        price=price_entry.total,
                        energy_price=price_entry.energy,
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
        """Get the current total price (energy + tax, for consumption)."""
        if self._price_override is not None:
            return self._price_override
        slot = self.get_current_slot()
        return slot.price if slot else None

    def get_current_energy_price(self) -> float | None:
        """Get the current energy-only price (for export/feed-in decisions)."""
        if self._price_override is not None:
            return self._price_override
        slot = self.get_current_slot()
        return slot.energy_price if slot else None

    def is_energy_price_negative(self) -> bool:
        """Check if the energy-only price is negative (you pay to export)."""
        price = self.get_current_energy_price()
        if price is None:
            return False
        return price < 0

    @property
    def enabled(self) -> bool:
        """Return whether Zeus management is enabled."""
        return self._enabled

    @callback
    def async_set_enabled(self, *, enabled: bool) -> None:
        """Enable or disable Zeus management."""
        self._enabled = enabled
        _LOGGER.info("Zeus management %s", "enabled" if enabled else "disabled")
        if self.data is not None:
            self.async_set_updated_data(self.data)

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
        """Get the total price for the next 15-minute slot."""
        slot = self.get_next_slot()
        return slot.price if slot else None

    def get_cached_forecast(self) -> dict[str, float] | None:
        """Return cached forecast if still valid, else None."""
        if (
            self._forecast_cache is not None
            and self._forecast_cache_time is not None
            and dt_util.utcnow() - self._forecast_cache_time < FORECAST_CACHE_TTL
        ):
            return self._forecast_cache
        return None

    def set_cached_forecast(self, forecast: dict[str, float]) -> None:
        """Store a fresh forecast in the cache."""
        self._forecast_cache = forecast
        self._forecast_cache_time = dt_util.utcnow()

    def _has_managed_devices(self) -> bool:
        """Check if any managed device subentries are configured."""
        if self.config_entry is None:
            return False
        return any(
            s.subentry_type
            in (
                SUBENTRY_SWITCH_DEVICE,
                SUBENTRY_THERMOSTAT_DEVICE,
                SUBENTRY_MANUAL_DEVICE,
            )
            for s in self.config_entry.subentries.values()
        )

    async def async_run_scheduler(self) -> None:
        """
        Run the scheduler and store results.

        Called automatically on each coordinator update and can also
        be triggered manually via the zeus.run_scheduler service.
        """
        if (
            not self._enabled
            or not self._has_managed_devices()
            or self.config_entry is None
        ):
            self.schedule_results = {}
            return

        # Lazy-import the scheduler module to avoid circular imports.
        # Cache it after the first load so importlib only runs once.
        if self._scheduler_module is None:
            self._scheduler_module = await self.hass.async_add_import_executor_job(
                importlib.import_module, ".scheduler", __package__
            )

        try:
            self.schedule_results = await self._scheduler_module.async_run_scheduler(
                self.hass, self.config_entry, self
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning("Scheduler run failed", exc_info=True)

    async def async_shutdown(self) -> None:
        """Shut down the coordinator and clean up timers."""
        self._async_stop_slot_timer()
        self._async_stop_solar_listener()
        self._async_stop_temperature_listener()
        self._async_stop_thermal_listener()
        await self.async_save_thermal_trackers()
        await self.async_save_reservations()
        await super().async_shutdown()
