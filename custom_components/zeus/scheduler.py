"""Scheduler for Zeus switch device management."""

from __future__ import annotations

import contextlib
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import EntityRegistry

from homeassistant.components.recorder import history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AVG_USAGE,
    CONF_CYCLE_DURATION,
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_DELAY_INTERVALS,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_FORECAST_API_KEY,
    CONF_MIN_CYCLE_TIME,
    CONF_PEAK_USAGE,
    CONF_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_PRODUCTION_ENTITY,
    CONF_SOLAR_AZIMUTH,
    CONF_SOLAR_DECLINATION,
    CONF_SOLAR_KWP,
    CONF_SWITCH_ENTITY,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_TOLERANCE,
    CONF_USE_ACTUAL_POWER,
    SLOT_DURATION_MIN,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_MANUAL_DEVICE,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
    SUBENTRY_THERMOSTAT_DEVICE,
)
from .coordinator import PriceCoordinator, PriceSlot
from .forecast_solar_api import (
    ForecastSolarApiError,
    ForecastSolarClient,
    SolarPlaneConfig,
)
from .thermal_model import async_get_learned_avg_power_w, blend_with_peak

_LOGGER = logging.getLogger(__name__)

# Minimum number of time parts when parsing a deadline string (HH:MM:SS)
_TIME_PARTS_WITH_SECONDS = 3


@dataclass
class DeviceScheduleRequest:
    """A device requesting scheduled runtime."""

    subentry_id: str
    name: str
    switch_entity: str
    power_sensor: str
    peak_usage_w: float
    daily_runtime_min: float
    deadline: time
    priority: int
    min_cycle_time_min: float = 0.0
    runtime_today_min: float = 0.0
    is_on: bool = False
    actual_usage_w: float | None = None
    use_actual_power: bool = False

    @property
    def effective_usage_w(self) -> float:
        """
        Power draw used for solar consumption calculations.

        When ``use_actual_power`` is enabled **and** the device is
        currently ON with a live reading, use the actual draw.  This is
        intended for devices whose real consumption may be zero while
        technically "on" (e.g. a boiler whose water is already hot).

        When ``use_actual_power`` is disabled (the default), peak is
        always returned.  This prevents other devices from reacting to
        temporary power dips in variable-load devices like washing
        machines.
        """
        if (
            self.use_actual_power
            and self.is_on
            and self.actual_usage_w is not None
            and self.actual_usage_w >= 0
        ):
            return self.actual_usage_w
        return self.peak_usage_w

    @property
    def remaining_runtime_min(self) -> float:
        """Minutes of runtime still needed today."""
        return max(0.0, self.daily_runtime_min - self.runtime_today_min)

    @property
    def remaining_slots_needed(self) -> int:
        """Number of 15-minute slots needed to meet remaining runtime."""
        return math.ceil(self.remaining_runtime_min / SLOT_DURATION_MIN)


@dataclass
class ScheduleResult:
    """The result of scheduling for one device."""

    subentry_id: str
    should_be_on: bool
    remaining_runtime_min: float
    scheduled_slots: list[datetime] = field(default_factory=list)
    reason: str = ""


@dataclass
class ThermostatScheduleRequest:
    """A thermostat device requesting temperature-managed scheduling."""

    subentry_id: str
    name: str
    switch_entity: str
    power_sensor: str
    temperature_sensor: str
    peak_usage_w: float
    target_temp_low: float
    target_temp_high: float
    priority: int
    min_cycle_time_min: float = 5.0
    hvac_mode: str = "heat"
    # Learned data (populated from thermal model)
    learned_avg_power_w: float | None = None
    wh_per_degree: float | None = None
    # Live state (populated at runtime)
    current_temperature: float | None = None
    is_on: bool = False
    actual_usage_w: float | None = None

    @property
    def effective_power_w(self) -> float:
        """Best estimate of power draw, using learned data when available."""
        if self.learned_avg_power_w is not None:
            return self.learned_avg_power_w
        return self.peak_usage_w

    @property
    def lower_bound(self) -> float:
        """Temperature at which heating is forced on."""
        return self.target_temp_low

    @property
    def upper_bound(self) -> float:
        """Temperature at which heating is forced off."""
        return self.target_temp_high

    @property
    def temp_urgency(self) -> float:
        """
        How urgently heating is needed (0.0 = at upper bound, 1.0 = at lower bound).

        Returns 0.5 if no temperature reading is available.
        """
        if self.current_temperature is None:
            return 0.5
        margin_range = self.upper_bound - self.lower_bound
        if margin_range <= 0:
            return 0.5
        return max(
            0.0, min(1.0, (self.upper_bound - self.current_temperature) / margin_range)
        )


def _get_state_changes(
    hass: HomeAssistant,
    entity_id: str,
    start: datetime,
    end: datetime,
) -> list[State]:
    """Fetch state changes from recorder DB. BLOCKING -- run in executor."""
    return history.state_changes_during_period(
        hass,
        start,
        end,
        entity_id,
        include_start_time_state=True,
        no_attributes=True,
    ).get(entity_id, [])


def _compute_on_seconds(
    states: list[State],
    start_ts: float,
    end_ts: float,
    now_ts: float,
) -> float:
    """Compute total seconds the entity spent in 'on' state."""
    previous_matches = False
    last_change_ts = 0.0
    elapsed = 0.0

    for state in states:
        matches = state.state == "on"
        change_ts = state.last_changed_timestamp

        if math.floor(change_ts) > end_ts:
            break
        if math.floor(change_ts) > now_ts:
            break

        if previous_matches:
            elapsed += change_ts - last_change_ts

        previous_matches = matches
        last_change_ts = max(start_ts, change_ts)

    if previous_matches:
        elapsed += min(end_ts, now_ts) - last_change_ts

    return elapsed


async def async_get_runtime_today_minutes(
    hass: HomeAssistant,
    entity_id: str,
) -> float:
    """Get how many minutes a switch entity has been 'on' today."""
    now = dt_util.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Convert to UTC for recorder queries
    start_utc = dt_util.as_utc(today_start)
    end_utc = dt_util.as_utc(now)

    try:
        instance = get_instance(hass)
    except KeyError:
        _LOGGER.debug("Recorder not available, assuming 0 runtime")
        return 0.0

    states = await instance.async_add_executor_job(
        _get_state_changes, hass, entity_id, start_utc, end_utc
    )

    now_ts = dt_util.utcnow().timestamp()
    start_ts = math.floor(start_utc.timestamp())
    end_ts = math.floor(end_utc.timestamp())

    seconds = _compute_on_seconds(states, start_ts, end_ts, now_ts)
    return seconds / 60.0


async def async_get_solar_forecast(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PriceCoordinator | None = None,
) -> dict[str, float] | None:
    """
    Get hourly solar forecast from Forecast.Solar API.

    Results are cached on the coordinator for 1 hour to stay within
    the Forecast.Solar free-tier rate limit (12 requests/hour).
    """
    # Return cached forecast if available and fresh
    if coordinator is not None:
        cached = coordinator.get_cached_forecast()
        if cached is not None:
            return cached

    # Collect solar plane configs from all solar_inverter subentries
    planes: list[SolarPlaneConfig] = []
    api_key: str | None = None

    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SOLAR_INVERTER:
            continue
        data = subentry.data
        dec = data.get(CONF_SOLAR_DECLINATION)
        az = data.get(CONF_SOLAR_AZIMUTH)
        kwp = data.get(CONF_SOLAR_KWP)
        if dec is None or az is None or kwp is None:
            continue
        planes.append(
            SolarPlaneConfig(
                declination=int(dec),
                azimuth=int(az),
                kwp=float(kwp),
            )
        )
        # Use the first API key found across subentries
        if api_key is None:
            api_key = data.get(CONF_FORECAST_API_KEY)

    if not planes:
        _LOGGER.debug("No solar planes configured, skipping forecast")
        return None

    # Use HA's configured latitude/longitude
    latitude = hass.config.latitude
    longitude = hass.config.longitude

    session = async_get_clientsession(hass)
    client = ForecastSolarClient(
        session=session,
        latitude=latitude,
        longitude=longitude,
        planes=planes,
        api_key=api_key,
    )

    try:
        result = await client.async_get_estimate()
    except ForecastSolarApiError:
        _LOGGER.exception("Failed to fetch solar forecast")
        return None

    if not result.watts:
        _LOGGER.warning("Solar forecast returned empty watts data")
        return None

    # Convert watts dict to wh_hours format for backward compatibility
    # with the scheduler's _build_slot_info. The old format was
    # {iso_string: wh_per_hour}. The new API gives us watts (avg power
    # per period). We group by hour and average the watts, which equals
    # the Wh for that hour.
    wh_hours: dict[str, float] = {}
    hourly_watts: dict[str, list[float]] = {}

    for dt_key, watts in result.watts.items():
        # Group by the start of each hour
        hour_start = dt_key.replace(minute=0, second=0, microsecond=0)
        iso_key = hour_start.isoformat()
        hourly_watts.setdefault(iso_key, []).append(watts)

    for iso_key, watt_list in hourly_watts.items():
        # Average watts over the hour = Wh for that hour
        wh_hours[iso_key] = sum(watt_list) / len(watt_list)

    # Cache the result on the coordinator
    if coordinator is not None:
        coordinator.set_cached_forecast(wh_hours)

    return wh_hours


def _build_device_requests(
    entry: ConfigEntry,
) -> list[DeviceScheduleRequest]:
    """Build schedule requests from switch device subentries."""
    requests = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SWITCH_DEVICE:
            continue
        data = subentry.data
        deadline_str = data.get(CONF_DEADLINE, "23:00:00")
        # Parse "HH:MM:SS" string to time object
        parts = str(deadline_str).split(":")
        deadline_time = time(
            hour=int(parts[0]),
            minute=int(parts[1]) if len(parts) > 1 else 0,
            second=int(parts[2]) if len(parts) >= _TIME_PARTS_WITH_SECONDS else 0,
        )

        requests.append(
            DeviceScheduleRequest(
                subentry_id=subentry.subentry_id,
                name=subentry.title,
                switch_entity=data[CONF_SWITCH_ENTITY],
                power_sensor=data[CONF_POWER_SENSOR],
                peak_usage_w=float(data[CONF_PEAK_USAGE]),
                daily_runtime_min=float(data[CONF_DAILY_RUNTIME]),
                deadline=deadline_time,
                priority=int(data.get(CONF_PRIORITY, 5)),
                min_cycle_time_min=float(data.get(CONF_MIN_CYCLE_TIME, 0)),
                use_actual_power=bool(data.get(CONF_USE_ACTUAL_POWER, False)),
            )
        )
    return requests


async def _async_build_thermostat_requests(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PriceCoordinator,
) -> list[ThermostatScheduleRequest]:
    """
    Build thermostat schedule requests from thermostat device subentries.

    Reads the target temperature from the associated climate entity and
    the tolerance from the subentry config to compute the heating bounds.
    Populates learned power and thermal data from the coordinator's
    thermal trackers and the recorder.
    """
    requests = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_THERMOSTAT_DEVICE:
            continue
        data = subentry.data

        tolerance = float(data.get(CONF_TEMPERATURE_TOLERANCE, 1.5))

        # Look up the climate entity for this subentry to get the target temp
        climate_entity_id = _find_climate_entity(hass, entry, subentry.subentry_id)
        target_temp = 20.0  # default
        hvac_mode = "heat"

        if climate_entity_id:
            climate_state = hass.states.get(climate_entity_id)
            if climate_state:
                hvac_mode = climate_state.state
                attrs = climate_state.attributes
                if "temperature" in attrs:
                    with contextlib.suppress(ValueError, TypeError):
                        target_temp = float(attrs["temperature"])

        # Query learned average power from the recorder
        peak_w = float(data[CONF_PEAK_USAGE])
        switch_entity = data[CONF_SWITCH_ENTITY]
        power_sensor = data[CONF_POWER_SENSOR]

        learned_avg_power_w: float | None = None
        wh_per_degree: float | None = None

        try:
            raw_learned, _on_hours = await async_get_learned_avg_power_w(
                hass, power_sensor, switch_entity
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to get learned power for %s", subentry.title, exc_info=True
            )
            raw_learned = None

        # Get thermal tracker data from coordinator
        tracker = coordinator.get_thermal_tracker(subentry.subentry_id)
        sample_count = tracker.sample_count if tracker else 0

        if raw_learned is not None:
            learned_avg_power_w = blend_with_peak(raw_learned, peak_w, sample_count)

        if tracker and tracker.wh_per_degree is not None:
            wh_per_degree = tracker.wh_per_degree

        requests.append(
            ThermostatScheduleRequest(
                subentry_id=subentry.subentry_id,
                name=subentry.title,
                switch_entity=switch_entity,
                power_sensor=power_sensor,
                temperature_sensor=data[CONF_TEMPERATURE_SENSOR],
                peak_usage_w=peak_w,
                target_temp_low=target_temp - tolerance,
                target_temp_high=target_temp + tolerance,
                priority=int(data.get(CONF_PRIORITY, 5)),
                min_cycle_time_min=float(data.get(CONF_MIN_CYCLE_TIME, 5)),
                hvac_mode=hvac_mode,
                learned_avg_power_w=learned_avg_power_w,
                wh_per_degree=wh_per_degree,
            )
        )
    return requests


def _find_climate_entity(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry_id: str,
) -> str | None:
    """Find the Zeus climate entity ID for a thermostat subentry."""
    ent_reg = er.async_get(hass)
    expected_unique_id = f"{entry.entry_id}_{subentry_id}_climate"

    for ent_entry in ent_reg.entities.get_entries_for_config_entry_id(entry.entry_id):
        if ent_entry.unique_id == expected_unique_id:
            return ent_entry.entity_id
    return None


@dataclass
class _SlotInfo:
    """Pre-computed information for a single time slot."""

    start_time: datetime
    price: float  # Total price (energy + tax) — consumption cost
    energy_price: float  # Energy-only price — feed-in compensation / export value
    solar_production_w: float  # Raw forecast production for this hour
    solar_surplus_w: float  # Production minus home consumption (shared pool)
    remaining_solar_w: float  # Surplus still available after device assignments


def _build_slot_info(
    price_slots: list[PriceSlot],
    solar_forecast: dict[str, Any] | None,
    home_consumption_w: float,
    now: datetime,
    live_solar_surplus_w: float | None = None,
) -> dict[datetime, _SlotInfo]:
    """
    Build pre-computed slot info for all future slots.

    This is computed once and shared across all devices. The
    ``remaining_solar_w`` field starts equal to ``solar_surplus_w`` and is
    decremented as devices are assigned to slots, so that multiple devices
    sharing a slot correctly split the available solar.

    For the **current** slot, if ``live_solar_surplus_w`` is provided it
    replaces the forecast surplus when it is higher, so that devices can
    be opportunistically activated when real production exceeds the
    forecast.
    """
    # Pre-parse the solar forecast into a {hour_dt: wh} lookup for O(1) access
    solar_by_hour: dict[tuple[int, int, int, int], float] = {}
    if solar_forecast:
        for iso_str, wh_value in solar_forecast.items():
            try:
                forecast_dt = dt_util.parse_datetime(iso_str)
                if forecast_dt is None:
                    continue
                key = (
                    forecast_dt.date().year,
                    forecast_dt.date().month,
                    forecast_dt.date().day,
                    forecast_dt.hour,
                )
                solar_by_hour[key] = float(wh_value)
            except (ValueError, TypeError):
                continue

    info: dict[datetime, _SlotInfo] = {}
    for slot in price_slots:
        slot_end = slot.start_time + timedelta(minutes=SLOT_DURATION_MIN)
        if slot_end <= now:
            continue

        price = slot.price if slot.price is not None else 0.0
        energy_price = slot.energy_price if slot.energy_price is not None else 0.0

        # Look up solar production for this slot's hour
        solar_production_w = 0.0
        hour_key = (
            slot.start_time.year,
            slot.start_time.month,
            slot.start_time.day,
            slot.start_time.hour,
        )
        if hour_key in solar_by_hour:
            # Wh per hour ≈ average W for that hour
            solar_production_w = solar_by_hour[hour_key]

        solar_surplus_w = max(0.0, solar_production_w - home_consumption_w)

        info[slot.start_time] = _SlotInfo(
            start_time=slot.start_time,
            price=price,
            energy_price=energy_price,
            solar_production_w=solar_production_w,
            solar_surplus_w=solar_surplus_w,
            remaining_solar_w=solar_surplus_w,
        )

    # Override current slot with live solar surplus when it exceeds forecast.
    _apply_live_solar_override(info, live_solar_surplus_w, now)

    return info


def _apply_live_solar_override(
    info: dict[datetime, _SlotInfo],
    live_solar_surplus_w: float | None,
    now: datetime,
) -> None:
    """
    Apply live solar surplus to the current slot and correct future forecasts.

    When real-time solar production exceeds the forecast, this replaces the
    current slot's surplus with the live value and applies a bias correction
    factor to all future slots to compensate for systematic under-prediction.
    """
    if live_solar_surplus_w is None:
        return

    current_slot_start = _get_current_slot_start(now)
    if current_slot_start not in info:
        return

    current = info[current_slot_start]
    if live_solar_surplus_w <= current.solar_surplus_w:
        return

    _LOGGER.debug(
        "Live solar surplus %.0fW exceeds forecast %.0fW for slot %s",
        live_solar_surplus_w,
        current.solar_surplus_w,
        current_slot_start,
    )

    # Compute bias factor: how much live exceeds forecast.
    # Apply to future slots to correct systematic under-prediction.
    forecast_surplus = current.solar_surplus_w
    if forecast_surplus > 0:
        bias = live_solar_surplus_w / forecast_surplus
        if bias > 1.0:
            _LOGGER.debug(
                "Applying forecast bias correction: %.2fx to future slots",
                bias,
            )
            for st, s in info.items():
                if st > current_slot_start and s.solar_surplus_w > 0:
                    adjusted = s.solar_surplus_w * bias
                    s.solar_surplus_w = adjusted
                    s.remaining_solar_w = adjusted

    current.solar_surplus_w = live_solar_surplus_w
    current.remaining_solar_w = live_solar_surplus_w


def _cost_for_device_in_slot(
    slot: _SlotInfo,
    device_peak_w: float,
) -> float:
    """
    Compute the marginal cost of running a device in a slot.

    Uses the slot's *remaining* solar (after previously-assigned devices have
    consumed their share) so that concurrent devices correctly split the
    available surplus.

    The slot's ``energy_price`` (spot price) is used as the opportunity cost
    of consuming solar instead of exporting it: without saldering, feed-in
    compensation equals the spot price.  If a future grid slot is cheaper
    than the current spot price, the device should run later and export now.

    Returns:
        A cost score where lower is better.  Full solar coverage yields a
        negative score (opportunity cost of the spot price), partial solar
        yields a proportionally reduced price, and no solar yields the raw
        grid price (total incl. tax).

    """
    feed_in = slot.energy_price  # spot price = what you earn for export

    if slot.remaining_solar_w >= device_peak_w:
        # Solar fully covers this device.
        # Opportunity cost: we lose the feed-in revenue for this power.
        if feed_in > 0:
            return -feed_in
        return -1.0

    if slot.remaining_solar_w > 0:
        solar_fraction = slot.remaining_solar_w / device_peak_w
        grid_cost = slot.price * (1.0 - solar_fraction)
        # Subtract opportunity cost of the solar portion we consume
        if feed_in > 0:
            opportunity_cost = feed_in * solar_fraction
            return grid_cost - opportunity_cost
        return grid_cost

    return slot.price


def _get_eligible_slots(
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
    deadline: time,
) -> list[datetime]:
    """Return slot start times between *now* and *deadline*, chronologically."""
    deadline_dt = now.replace(
        hour=deadline.hour,
        minute=deadline.minute,
        second=deadline.second,
        microsecond=0,
    )
    if deadline_dt <= now:
        return []

    return sorted(st for st, s in slot_info.items() if s.start_time < deadline_dt)


@dataclass
class _DeviceState:
    """Mutable bookkeeping for a device during scheduling."""

    remaining_needed: int
    assigned_slots: list[datetime] = field(default_factory=list)
    forced_on: bool = False


def _apply_deadline_forced(
    active_devices: list[DeviceScheduleRequest],
    states: dict[str, _DeviceState],
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
    current_slot_start: datetime,
) -> None:
    """Phase 1: Force-assign all eligible slots for deadline-pressured devices."""
    for device in active_devices:
        eligible = _get_eligible_slots(slot_info, now, device.deadline)
        if not eligible:
            continue

        state = states[device.subentry_id]
        if state.remaining_needed < len(eligible):
            continue

        # Must use ALL eligible slots — no room to skip any
        state.forced_on = True
        for st in eligible:
            if st not in state.assigned_slots:
                state.assigned_slots.append(st)
                state.remaining_needed = max(0, state.remaining_needed - 1)
                # Consume solar — use actual draw for current slot
                consumption = _solar_consumption_for_device(
                    device, st, current_slot_start
                )
                info = slot_info[st]
                info.remaining_solar_w = max(0.0, info.remaining_solar_w - consumption)


def _solar_consumption_for_device(
    device: DeviceScheduleRequest,
    slot_start: datetime,
    current_slot_start: datetime,
) -> float:
    """
    Determine how much solar a device will consume in a slot.

    For the **current** slot, if the device is already ON, use its actual
    live power draw (which may be lower than peak).  For future slots or
    devices that are off, use peak as a safe upper bound.
    """
    if slot_start == current_slot_start:
        return device.effective_usage_w
    return device.peak_usage_w


def _find_cheapest_assignment(
    active_devices: list[DeviceScheduleRequest],
    states: dict[str, _DeviceState],
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
) -> tuple[DeviceScheduleRequest | None, datetime | None]:
    """Find the single globally cheapest (device, slot) pair to assign next."""
    best_cost = float("inf")
    best_device: DeviceScheduleRequest | None = None
    best_slot_time: datetime | None = None

    for device in active_devices:
        state = states[device.subentry_id]
        if state.remaining_needed <= 0:
            continue

        eligible = _get_eligible_slots(slot_info, now, device.deadline)
        already = set(state.assigned_slots)

        for st in eligible:
            if st in already:
                continue
            cost = _cost_for_device_in_slot(slot_info[st], device.peak_usage_w)

            # Pick lowest cost; break ties by priority (lower = better)
            if cost < best_cost or (
                cost == best_cost
                and best_device is not None
                and device.priority < best_device.priority
            ):
                best_cost = cost
                best_device = device
                best_slot_time = st

    return best_device, best_slot_time


def _apply_cost_optimal(
    active_devices: list[DeviceScheduleRequest],
    states: dict[str, _DeviceState],
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
    current_slot_start: datetime,
) -> None:
    """Phase 2: Iteratively assign the globally cheapest (device, slot) pair."""
    while True:
        best_device, best_slot_time = _find_cheapest_assignment(
            active_devices, states, slot_info, now
        )
        if best_device is None or best_slot_time is None:
            break

        state = states[best_device.subentry_id]
        state.assigned_slots.append(best_slot_time)
        state.remaining_needed -= 1

        # Consume solar surplus — use actual draw for current slot
        consumption = _solar_consumption_for_device(
            best_device, best_slot_time, current_slot_start
        )
        info = slot_info[best_slot_time]
        info.remaining_solar_w = max(0.0, info.remaining_solar_w - consumption)


def _build_result(
    device: DeviceScheduleRequest,
    state: _DeviceState,
    slot_info: dict[datetime, _SlotInfo],
    current_slot_start: datetime,
) -> ScheduleResult:
    """Build a ScheduleResult for a single device from its assignment state."""
    slots = sorted(state.assigned_slots)
    should_be_on = current_slot_start in slots

    # Determine if current slot is solar-powered (check original surplus)
    solar_powered = False
    if should_be_on and current_slot_start in slot_info:
        solar_powered = (
            slot_info[current_slot_start].solar_surplus_w >= device.peak_usage_w
        )

    if state.forced_on and should_be_on:
        reason = "Forced on: deadline pressure"
    else:
        reason = _determine_reason(
            should_be_on=should_be_on,
            deadline_pressure=False,
            has_remaining_runtime=device.remaining_runtime_min > 0,
            solar_powered=solar_powered,
        )

    return ScheduleResult(
        subentry_id=device.subentry_id,
        should_be_on=should_be_on,
        remaining_runtime_min=device.remaining_runtime_min,
        scheduled_slots=slots,
        reason=reason,
    )


def compute_schedules(  # noqa: PLR0913
    devices: list[DeviceScheduleRequest],
    price_slots: list[PriceSlot],
    solar_forecast: dict[str, Any] | None,
    home_consumption_w: float,
    now: datetime,
    live_solar_surplus_w: float | None = None,
) -> tuple[dict[str, ScheduleResult], dict[datetime, _SlotInfo]]:
    """
    Compute the globally optimal schedule for all devices.

    The algorithm minimises total energy cost across all devices while
    respecting deadlines and priorities.  Multiple devices **can** run in
    the same slot, with solar surplus shared fairly between them.

    Phase 1 forces on devices whose deadline leaves no room to skip slots.
    Phase 2 iteratively picks the globally cheapest (device, slot) pair,
    deducting solar surplus after each pick so costs stay accurate.
    Priority is used as a tiebreaker when costs are equal.

    When ``live_solar_surplus_w`` is provided, the current slot's solar
    surplus is upgraded to the live value if it exceeds the forecast.

    Each slot's ``energy_price`` (the spot price without tax) is used as the
    feed-in opportunity cost — what you'd earn by exporting solar instead of
    consuming it.  If a future grid slot is cheaper, the device runs later.

    Devices that are currently ON report their ``actual_usage_w`` via
    the power sensor.  For the current slot, this actual draw is used
    for solar consumption calculations instead of peak, freeing surplus
    for other devices.

    Returns:
        A tuple of (schedule results dict, slot_info dict).  The slot_info
        has its ``remaining_solar_w`` depleted by the assigned devices and
        can be passed to ``compute_thermostat_decisions`` so both device
        types share a single solar pool.

    """
    slot_info = _build_slot_info(
        price_slots, solar_forecast, home_consumption_w, now, live_solar_surplus_w
    )
    current_slot_start = _get_current_slot_start(now)

    results: dict[str, ScheduleResult] = {}
    states: dict[str, _DeviceState] = {}
    active_devices: list[DeviceScheduleRequest] = []

    for device in devices:
        if device.remaining_runtime_min <= 0:
            results[device.subentry_id] = ScheduleResult(
                subentry_id=device.subentry_id,
                should_be_on=False,
                remaining_runtime_min=0.0,
                reason="Daily runtime already met",
            )
            continue
        active_devices.append(device)
        states[device.subentry_id] = _DeviceState(
            remaining_needed=device.remaining_slots_needed,
        )

    if not active_devices:
        return results, slot_info

    # Sort by priority for deterministic processing (1 = highest)
    active_devices.sort(key=lambda d: d.priority)

    _apply_deadline_forced(active_devices, states, slot_info, now, current_slot_start)
    _apply_cost_optimal(active_devices, states, slot_info, now, current_slot_start)

    for device in active_devices:
        results[device.subentry_id] = _build_result(
            device, states[device.subentry_id], slot_info, current_slot_start
        )

    return results, slot_info


def _determine_reason(
    *,
    should_be_on: bool,
    deadline_pressure: bool,
    has_remaining_runtime: bool,
    solar_powered: bool,
) -> str:
    """Determine the human-readable scheduling reason."""
    if should_be_on and deadline_pressure:
        return "Forced on: deadline pressure"
    if should_be_on and solar_powered:
        return "Scheduled: solar surplus available"
    if should_be_on:
        return "Scheduled: optimal price slot"
    if has_remaining_runtime:
        return "Waiting for cheaper slot"
    return "Daily runtime met"


def _get_current_slot_start(now: datetime) -> datetime:
    """Get the start time of the current 15-minute slot."""
    minute = (now.minute // SLOT_DURATION_MIN) * SLOT_DURATION_MIN
    return now.replace(minute=minute, second=0, microsecond=0)


async def _async_populate_switch_devices(
    hass: HomeAssistant,
    devices: list[DeviceScheduleRequest],
) -> None:
    """Populate live state for switch device requests."""
    for device in devices:
        device.runtime_today_min = await async_get_runtime_today_minutes(
            hass, device.switch_entity
        )
        switch_state = hass.states.get(device.switch_entity)
        device.is_on = switch_state is not None and switch_state.state == "on"
        power_state = hass.states.get(device.power_sensor)
        if power_state and power_state.state not in ("unknown", "unavailable"):
            try:
                device.actual_usage_w = float(power_state.state)
            except (ValueError, TypeError):
                device.actual_usage_w = None


def _populate_thermostat_live_state(
    hass: HomeAssistant,
    thermostats: list[ThermostatScheduleRequest],
) -> None:
    """Populate live sensor readings for thermostat requests."""
    for therm in thermostats:
        temp_state = hass.states.get(therm.temperature_sensor)
        if temp_state and temp_state.state not in ("unknown", "unavailable"):
            try:
                therm.current_temperature = float(temp_state.state)
            except (ValueError, TypeError):
                therm.current_temperature = None

        switch_state = hass.states.get(therm.switch_entity)
        therm.is_on = switch_state is not None and switch_state.state == "on"
        power_state = hass.states.get(therm.power_sensor)
        if power_state and power_state.state not in ("unknown", "unavailable"):
            try:
                therm.actual_usage_w = float(power_state.state)
            except (ValueError, TypeError):
                therm.actual_usage_w = None


def _ensure_slot_info(  # noqa: PLR0913
    shared_slot_info: dict[datetime, _SlotInfo] | None,
    price_slots: list[PriceSlot],
    solar_forecast: dict[str, Any] | None,
    home_consumption_w: float,
    now: datetime,
    live_solar_surplus_w: float | None,
) -> dict[datetime, _SlotInfo]:
    """Return existing slot info or build it fresh."""
    if shared_slot_info is not None:
        return shared_slot_info
    return _build_slot_info(
        price_slots, solar_forecast, home_consumption_w, now, live_solar_surplus_w
    )


async def async_run_scheduler(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PriceCoordinator,
) -> dict[str, ScheduleResult]:
    """Run the full scheduling cycle for switch, thermostat, and manual devices."""
    results: dict[str, ScheduleResult] = {}

    price_slots = _get_all_future_slots(coordinator)
    solar_forecast = await async_get_solar_forecast(hass, entry, coordinator)
    coordinator.solar_forecast = solar_forecast
    raw_home_consumption_w = _get_home_consumption(hass, entry)
    now = dt_util.now()

    # --- Switch devices ---
    devices = _build_device_requests(entry)
    if devices:
        await _async_populate_switch_devices(hass, devices)

    # Subtract power draw of Zeus-managed devices that are currently ON from
    # home consumption.  The home monitor reports total household load which
    # includes devices controlled by Zeus.  If we don't subtract them, a
    # device turning on inflates home consumption → reduces the apparent
    # solar surplus → scheduler reschedules the device to a "cheaper" slot
    # → device turns off → surplus returns → feedback loop.
    managed_draw_w = _get_managed_device_draw(hass, entry, devices)
    home_consumption_w = max(0.0, raw_home_consumption_w - managed_draw_w)
    if managed_draw_w > 0:
        _LOGGER.debug(
            "Home consumption: %.0fW raw - %.0fW managed = %.0fW net",
            raw_home_consumption_w,
            managed_draw_w,
            home_consumption_w,
        )
    live_solar_surplus_w = _get_live_solar_surplus(hass, entry, home_consumption_w)

    # Build shared slot info once — all device types deplete the same solar pool.
    shared_slot_info: dict[datetime, _SlotInfo] | None = None

    # --- Manual device reservations (apply BEFORE smart device scheduling) ---
    manual_requests = _build_manual_device_requests(hass, entry)
    active_reservations = coordinator.get_active_reservations()
    if active_reservations and manual_requests:
        shared_slot_info = _ensure_slot_info(
            shared_slot_info,
            price_slots,
            solar_forecast,
            home_consumption_w,
            now,
            live_solar_surplus_w,
        )
        apply_reservations_to_slot_info(
            shared_slot_info, active_reservations, manual_requests
        )

    # --- Switch devices ---
    if devices:
        if shared_slot_info is not None:
            switch_results, shared_slot_info = _compute_schedules_with_slot_info(
                devices, shared_slot_info, now
            )
        else:
            switch_results, shared_slot_info = compute_schedules(
                devices,
                price_slots,
                solar_forecast,
                home_consumption_w,
                now,
                live_solar_surplus_w=live_solar_surplus_w,
            )
        results.update(switch_results)

    # --- Thermostat devices ---
    thermostats = await _async_build_thermostat_requests(hass, entry, coordinator)
    if thermostats:
        _populate_thermostat_live_state(hass, thermostats)
        results.update(
            compute_thermostat_decisions(
                thermostats,
                price_slots,
                solar_forecast,
                home_consumption_w,
                now,
                live_solar_surplus_w=live_solar_surplus_w,
                slot_info=shared_slot_info,
            )
        )

    # --- Manual device rankings (compute AFTER smart device scheduling) ---
    if manual_requests:
        shared_slot_info = _ensure_slot_info(
            shared_slot_info,
            price_slots,
            solar_forecast,
            home_consumption_w,
            now,
            live_solar_surplus_w,
        )
        manual_results: dict[str, ManualDeviceRanking] = {}
        for req in manual_requests:
            manual_results[req.subentry_id] = compute_manual_device_rankings(
                req, shared_slot_info, now
            )
        coordinator.manual_device_results = manual_results

    return results


def _compute_schedules_with_slot_info(
    devices: list[DeviceScheduleRequest],
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
) -> tuple[dict[str, ScheduleResult], dict[datetime, _SlotInfo]]:
    """
    Run the switch scheduling algorithm on pre-built slot info.

    Used when slot_info was already created (e.g. to apply manual device
    reservations) so we don't rebuild and lose the reservation deductions.
    """
    current_slot_start = _get_current_slot_start(now)

    results: dict[str, ScheduleResult] = {}
    states: dict[str, _DeviceState] = {}
    active_devices: list[DeviceScheduleRequest] = []

    for device in devices:
        if device.remaining_runtime_min <= 0:
            results[device.subentry_id] = ScheduleResult(
                subentry_id=device.subentry_id,
                should_be_on=False,
                remaining_runtime_min=0.0,
                reason="Daily runtime already met",
            )
            continue
        active_devices.append(device)
        states[device.subentry_id] = _DeviceState(
            remaining_needed=device.remaining_slots_needed,
        )

    if not active_devices:
        return results, slot_info

    active_devices.sort(key=lambda d: d.priority)

    _apply_deadline_forced(active_devices, states, slot_info, now, current_slot_start)
    _apply_cost_optimal(active_devices, states, slot_info, now, current_slot_start)

    for device in active_devices:
        results[device.subentry_id] = _build_result(
            device, states[device.subentry_id], slot_info, current_slot_start
        )

    return results, slot_info


def _get_all_future_slots(coordinator: PriceCoordinator) -> list[PriceSlot]:
    """Get all available price slots from the coordinator."""
    if not coordinator.data:
        return []

    home = coordinator.get_first_home_name()
    if not home:
        return []

    return coordinator.data.get(home, [])


def _get_managed_device_draw(
    hass: HomeAssistant,
    entry: ConfigEntry,
    switch_devices: list[DeviceScheduleRequest] | None = None,
) -> float:
    """
    Sum the live power draw of all Zeus-managed devices that are currently ON.

    This includes switch devices and thermostat devices.  The value is
    subtracted from the home monitor reading so that Zeus's own managed
    load doesn't inflate the "background" home consumption used for
    solar surplus calculations.
    """
    total = 0.0

    # Switch devices (already populated with live state if provided)
    if switch_devices:
        for device in switch_devices:
            if device.is_on and device.actual_usage_w is not None:
                total += device.actual_usage_w

    # Thermostat devices — read directly from power sensors
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_THERMOSTAT_DEVICE:
            continue
        switch_entity = subentry.data.get(CONF_SWITCH_ENTITY)
        power_sensor = subentry.data.get(CONF_POWER_SENSOR)
        if not switch_entity or not power_sensor:
            continue
        switch_state = hass.states.get(switch_entity)
        if switch_state is None or switch_state.state != "on":
            continue
        power_state = hass.states.get(power_sensor)
        if power_state and power_state.state not in ("unknown", "unavailable"):
            with contextlib.suppress(ValueError, TypeError):
                total += float(power_state.state)

    return total


def _get_home_consumption(hass: HomeAssistant, entry: ConfigEntry) -> float:
    """Get current home consumption from the home monitor subentry."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_HOME_MONITOR:
            entity_id = subentry.data.get(CONF_ENERGY_USAGE_ENTITY)
            if entity_id:
                state = hass.states.get(entity_id)
                if state and state.state not in ("unknown", "unavailable"):
                    try:
                        return float(state.state)
                    except (ValueError, TypeError):
                        pass
    return 0.0


def _get_live_solar_surplus(
    hass: HomeAssistant,
    entry: ConfigEntry,
    home_consumption_w: float,
) -> float | None:
    """
    Get the real-time solar surplus (production minus consumption).

    Reads the current solar production from the inverter's production entity.
    Returns None if no solar inverter is configured or the sensor is
    unavailable, so the scheduler can fall back to forecast-only mode.
    """
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_SOLAR_INVERTER:
            continue
        entity_id = subentry.data.get(CONF_PRODUCTION_ENTITY)
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                production_w = float(state.state)
                return max(0.0, production_w - home_consumption_w)
            except (ValueError, TypeError):
                _LOGGER.debug(
                    "Could not parse solar production from %s: %s",
                    entity_id,
                    state.state,
                )
    return None


# ---------------------------------------------------------------------------
# Thermostat decision engine
# ---------------------------------------------------------------------------

# Number of upcoming slots to consider for price comparison
_THERMOSTAT_LOOKAHEAD_SLOTS = 8

# Urgency threshold below which we consider waiting for solar
_SOLAR_WAIT_URGENCY_THRESHOLD = 0.6

# Urgency threshold for fallback (no price data) decisions
_URGENCY_FALLBACK_THRESHOLD = 0.5


def _percentile_rank(value: float, values: list[float]) -> float:
    """
    Compute the percentile rank of *value* within *values* (0.0 to 1.0).

    A rank of 0.0 means *value* is the cheapest; 1.0 means the most expensive.
    Returns 0.5 if *values* is empty.
    """
    if not values:
        return 0.5
    count_below = sum(1 for v in values if v < value)
    return count_below / len(values)


def compute_thermostat_decisions(  # noqa: PLR0913
    thermostats: list[ThermostatScheduleRequest],
    price_slots: list[PriceSlot],
    solar_forecast: dict[str, Any] | None,
    home_consumption_w: float,
    now: datetime,
    live_solar_surplus_w: float | None = None,
    slot_info: dict[datetime, _SlotInfo] | None = None,
) -> dict[str, ScheduleResult]:
    """
    Compute heating decisions for all thermostat devices.

    Unlike switch devices (which use slot-based runtime scheduling), thermostat
    devices use a real-time decision engine based on temperature state, price
    context, and solar availability.

    The algorithm uses three tiers:
    1. FORCE ON  -- temperature at or below lower bound (target - margin)
    2. FORCE OFF -- temperature at or above upper bound (target + margin)
    3. OPTIMIZE  -- within margin, decide based on price, solar, and urgency

    Devices are processed by priority (1 = highest) so that higher-priority
    zones consume solar surplus first, leaving less for lower-priority zones.

    When ``slot_info`` is provided (e.g. pre-depleted by switch device
    scheduling), it is reused so that thermostats see the remaining solar
    after switch devices have consumed their share.
    """
    if not thermostats:
        return {}

    # Reuse shared slot_info if provided, otherwise build fresh
    if slot_info is None:
        slot_info = _build_slot_info(
            price_slots, solar_forecast, home_consumption_w, now, live_solar_surplus_w
        )
    current_slot_start = _get_current_slot_start(now)

    # Get current and upcoming price slots for comparison
    current_slot = slot_info.get(current_slot_start)
    upcoming_slots = sorted(
        (s for st, s in slot_info.items() if st > current_slot_start),
        key=lambda s: s.start_time,
    )[:_THERMOSTAT_LOOKAHEAD_SLOTS]

    upcoming_prices = [s.price for s in upcoming_slots]

    # Sort by priority (1 = highest) for deterministic solar allocation
    sorted_thermostats = sorted(thermostats, key=lambda t: t.priority)

    results: dict[str, ScheduleResult] = {}

    for thermostat in sorted_thermostats:
        result = _decide_thermostat(
            thermostat,
            current_slot,
            upcoming_prices,
            upcoming_slots,
            slot_info,
            current_slot_start,
        )
        results[thermostat.subentry_id] = result

        # Consume solar surplus if this thermostat will heat
        if result.should_be_on and current_slot_start in slot_info:
            info = slot_info[current_slot_start]
            consumption = thermostat.actual_usage_w or thermostat.effective_power_w
            info.remaining_solar_w = max(0.0, info.remaining_solar_w - consumption)

    return results


def _decide_thermostat(  # noqa: PLR0913
    thermostat: ThermostatScheduleRequest,
    current_slot: _SlotInfo | None,
    upcoming_prices: list[float],
    upcoming_slots: list[_SlotInfo],
    slot_info: dict[datetime, _SlotInfo],
    current_slot_start: datetime,
) -> ScheduleResult:
    """
    Decide whether a single thermostat device should heat right now.

    Returns a ScheduleResult with should_be_on and a descriptive reason.
    """
    temp = thermostat.current_temperature
    lower = thermostat.lower_bound
    upper = thermostat.upper_bound

    # HVAC mode OFF — do not heat
    if thermostat.hvac_mode == "off":
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=False,
            remaining_runtime_min=0.0,
            scheduled_slots=[],
            reason="Thermostat off",
        )

    # No temperature reading — safe fallback based on current state
    if temp is None:
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=thermostat.is_on,
            remaining_runtime_min=0.0,
            scheduled_slots=[current_slot_start] if thermostat.is_on else [],
            reason="No temperature reading \u2014 holding current state",
        )

    # Tier 1: FORCE ON — below minimum temperature
    if temp <= lower:
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=True,
            remaining_runtime_min=0.0,
            scheduled_slots=[current_slot_start],
            reason=(
                f"Forced on: temperature {temp:.1f}\u00b0C"
                f" at or below minimum {lower:.1f}\u00b0C"
            ),
        )

    # Tier 2: FORCE OFF — above maximum temperature
    if temp >= upper:
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=False,
            remaining_runtime_min=0.0,
            scheduled_slots=[],
            reason=(
                f"Forced off: temperature {temp:.1f}\u00b0C"
                f" at or above maximum {upper:.1f}\u00b0C"
            ),
        )

    # Tier 3: OPTIMIZE — within margin, decide based on price/solar/urgency
    return _decide_thermostat_optimized(
        thermostat,
        current_slot,
        upcoming_prices,
        upcoming_slots,
        slot_info,
        current_slot_start,
    )


def _decide_thermostat_optimized(  # noqa: PLR0913
    thermostat: ThermostatScheduleRequest,
    current_slot: _SlotInfo | None,
    upcoming_prices: list[float],
    upcoming_slots: list[_SlotInfo],
    slot_info: dict[datetime, _SlotInfo],
    current_slot_start: datetime,
) -> ScheduleResult:
    """
    Optimization logic when temperature is within the margin range.

    Uses urgency-weighted price threshold: the closer to the lower margin,
    the more willing Zeus is to accept higher-priced slots for heating.
    Solar surplus is always used (free energy). Solar forecast is considered
    for look-ahead: coast if free solar is expected soon.

    When thermal model data is available (``wh_per_degree``), thermal
    headroom is estimated to decide whether the zone can safely coast
    until a cheaper slot arrives.
    """
    urgency = thermostat.temp_urgency
    power_w = thermostat.effective_power_w

    # Check solar surplus availability in current slot
    has_solar = False
    if current_slot is not None:
        has_solar = current_slot.remaining_solar_w >= power_w

    # Solar surplus available — always heat (free energy)
    if has_solar:
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=True,
            remaining_runtime_min=0.0,
            scheduled_slots=[current_slot_start],
            reason="Heating: solar surplus available",
        )

    # Solar forecast look-ahead: if urgency is low and solar is coming soon, coast
    if urgency < _SOLAR_WAIT_URGENCY_THRESHOLD and _solar_coming_soon(
        upcoming_slots, power_w
    ):
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=False,
            remaining_runtime_min=0.0,
            scheduled_slots=[],
            reason="Coasting: solar surplus expected soon",
        )

    # Thermal headroom: if we know Wh/°C, estimate how long until lower bound
    headroom_result = _check_thermal_headroom(
        thermostat, upcoming_prices, upcoming_slots, slot_info, current_slot_start
    )
    if headroom_result is not None:
        return headroom_result

    # Price-based decision: urgency-weighted threshold
    if current_slot is not None and upcoming_prices:
        price_rank = _percentile_rank(current_slot.price, upcoming_prices)

        # Urgency-weighted threshold:
        # urgency 0.3 (near upper) → heat only if price in bottom 30%
        # urgency 0.7 (near lower) → heat if price in bottom 70%
        # urgency 1.0 → always heat (but this is caught by FORCE ON above)
        if price_rank <= urgency:
            return ScheduleResult(
                subentry_id=thermostat.subentry_id,
                should_be_on=True,
                remaining_runtime_min=0.0,
                scheduled_slots=[current_slot_start],
                reason=(
                    f"Heating: cheap price"
                    f" (rank {price_rank:.0%}, urgency {urgency:.0%})"
                ),
            )

        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=False,
            remaining_runtime_min=0.0,
            scheduled_slots=[],
            reason=(
                f"Coasting: waiting for cheaper slot"
                f" (rank {price_rank:.0%}, urgency {urgency:.0%})"
            ),
        )

    # No price data — fall back to heating if urgency is above threshold
    should_heat = urgency > _URGENCY_FALLBACK_THRESHOLD
    return ScheduleResult(
        subentry_id=thermostat.subentry_id,
        should_be_on=should_heat,
        remaining_runtime_min=0.0,
        scheduled_slots=[current_slot_start] if should_heat else [],
        reason="Heating: no price data, urgency-based fallback"
        if should_heat
        else "Coasting: no price data, urgency-based fallback",
    )


# Thermal headroom thresholds
_HEADROOM_COAST_HOURS = 2.0  # Coast if we can go this long before lower bound
_HEADROOM_URGENT_HOURS = 0.5  # Boost urgency if time to lower bound is this short


def _check_thermal_headroom(
    thermostat: ThermostatScheduleRequest,
    upcoming_prices: list[float],
    upcoming_slots: list[_SlotInfo],
    slot_info: dict[datetime, _SlotInfo],
    current_slot_start: datetime,
) -> ScheduleResult | None:
    """
    Use thermal model to decide whether to coast or heat urgently.

    Returns a ScheduleResult if the thermal model provides a clear signal,
    or None to fall through to the normal price-based decision.
    """
    if thermostat.wh_per_degree is None or thermostat.current_temperature is None:
        return None

    power_w = thermostat.effective_power_w
    if power_w <= 0:
        return None

    degrees_above_lower = thermostat.current_temperature - thermostat.lower_bound
    if degrees_above_lower <= 0:
        return None  # At or below lower — force on handles this

    # Estimate coast time: how many hours until we hit the lower bound
    # assuming the zone loses heat at the same rate it took to gain it
    coast_time_hours = degrees_above_lower * thermostat.wh_per_degree / power_w

    # Plenty of headroom and a cheaper slot exists within the coast window
    if coast_time_hours > _HEADROOM_COAST_HOURS and upcoming_prices:
        coast_slots = int(coast_time_hours * 60 / SLOT_DURATION_MIN)
        reachable = upcoming_slots[:coast_slots]
        current_price = (
            slot_info[current_slot_start].price
            if current_slot_start in slot_info
            else None
        )
        if current_price is not None and any(
            s.price < current_price for s in reachable
        ):
            return ScheduleResult(
                subentry_id=thermostat.subentry_id,
                should_be_on=False,
                remaining_runtime_min=0.0,
                scheduled_slots=[],
                reason=(
                    f"Coasting: thermal headroom {coast_time_hours:.1f}h,"
                    f" cheaper slot available"
                ),
            )

    # Very little headroom — boost effective urgency to accept current slot
    if coast_time_hours < _HEADROOM_URGENT_HOURS:
        current_slot = slot_info.get(current_slot_start)
        if current_slot is not None:
            return ScheduleResult(
                subentry_id=thermostat.subentry_id,
                should_be_on=True,
                remaining_runtime_min=0.0,
                scheduled_slots=[current_slot_start],
                reason=(
                    f"Heating: low thermal headroom"
                    f" ({coast_time_hours:.1f}h to lower bound)"
                ),
            )

    return None


def _solar_coming_soon(
    upcoming_slots: list[_SlotInfo],
    device_peak_w: float,
    max_slots_ahead: int = 3,
) -> bool:
    """Check if any upcoming slot (within max_slots_ahead) has enough solar surplus."""
    for slot in upcoming_slots[:max_slots_ahead]:
        if slot.remaining_solar_w >= device_peak_w:
            return True
    return False


# ---------------------------------------------------------------------------
# Manual (dumb) device ranking
# ---------------------------------------------------------------------------


@dataclass
class ManualDeviceScheduleRequest:
    """A non-smart device requesting schedule advice."""

    subentry_id: str
    name: str
    peak_usage_w: float
    cycle_duration_min: float
    priority: int
    power_sensor: str | None = None
    delay_intervals_h: list[float] | None = None  # e.g. [3, 6, 9]
    avg_usage_w: float | None = None


@dataclass
class ManualDeviceWindow:
    """A single candidate time window for a manual device cycle."""

    start_time: datetime
    end_time: datetime
    total_cost: float
    solar_fraction: float  # 0.0-1.0, fraction of slots with solar coverage
    delay_hours: float | None = None  # set when device uses delay intervals


@dataclass
class ManualDeviceRanking:
    """Ranked windows for a manual device, cheapest first."""

    subentry_id: str
    windows: list[ManualDeviceWindow]  # sorted cheapest first
    recommended_start: datetime | None
    recommended_end: datetime | None


def compute_manual_device_rankings(
    request: ManualDeviceScheduleRequest,
    slot_info: dict[datetime, _SlotInfo],
    now: datetime,
) -> ManualDeviceRanking:
    """
    Rank all contiguous time windows for a manual device cycle.

    Only slots until the next 06:00 local time are considered, so
    recommendations stay within an actionable overnight horizon.

    For devices with delay intervals, only windows starting at valid delay
    offsets from ``now`` are considered. Otherwise, every contiguous block
    of ``ceil(cycle_duration / 15)`` future slots is evaluated.
    """
    slots_needed = math.ceil(request.cycle_duration_min / SLOT_DURATION_MIN)
    if slots_needed <= 0:
        return _empty_ranking(request.subentry_id)

    # Only consider slots until the next day at 06:00 local time.
    tomorrow_6am = (now + timedelta(days=1)).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    # If it's already past midnight but before 06:00, use today's 06:00
    today_6am = now.replace(hour=6, minute=0, second=0, microsecond=0)
    cutoff = today_6am if now < today_6am else tomorrow_6am

    eligible = sorted(
        st
        for st in slot_info
        if st + timedelta(minutes=SLOT_DURATION_MIN) > now and st < cutoff
    )
    if not eligible:
        return _empty_ranking(request.subentry_id)

    if request.delay_intervals_h:
        windows = _rank_delay_interval_windows(
            request, slot_info, eligible, slots_needed, now
        )
    else:
        windows = _rank_all_contiguous_windows(
            request, slot_info, eligible, slots_needed
        )

    # Sort by total cost (cheapest first), then by solar fraction (higher first)
    windows.sort(key=lambda w: (w.total_cost, -w.solar_fraction))

    recommended_start = windows[0].start_time if windows else None
    recommended_end = windows[0].end_time if windows else None

    return ManualDeviceRanking(
        subentry_id=request.subentry_id,
        windows=windows,
        recommended_start=recommended_start,
        recommended_end=recommended_end,
    )


def _empty_ranking(subentry_id: str) -> ManualDeviceRanking:
    """Return an empty ranking result."""
    return ManualDeviceRanking(
        subentry_id=subentry_id,
        windows=[],
        recommended_start=None,
        recommended_end=None,
    )


def _rank_all_contiguous_windows(
    request: ManualDeviceScheduleRequest,
    slot_info: dict[datetime, _SlotInfo],
    eligible: list[datetime],
    slots_needed: int,
) -> list[ManualDeviceWindow]:
    """Find and score all contiguous windows of the required length."""
    windows: list[ManualDeviceWindow] = []

    for i in range(len(eligible) - slots_needed + 1):
        window_slots = eligible[i : i + slots_needed]

        if not _is_contiguous(window_slots):
            continue

        total_cost, solar_fraction = _score_window(
            window_slots, slot_info, request.peak_usage_w, request.avg_usage_w
        )
        end_time = window_slots[-1] + timedelta(minutes=SLOT_DURATION_MIN)

        windows.append(
            ManualDeviceWindow(
                start_time=window_slots[0],
                end_time=end_time,
                total_cost=total_cost,
                solar_fraction=solar_fraction,
            )
        )

    return windows


def _rank_delay_interval_windows(
    request: ManualDeviceScheduleRequest,
    slot_info: dict[datetime, _SlotInfo],
    eligible: list[datetime],
    slots_needed: int,
    now: datetime,
) -> list[ManualDeviceWindow]:
    """
    Evaluate only windows starting at valid delay offsets from now.

    For a device with delay_intervals_h=[3, 6, 9], we compute the slot
    boundary for each delay and score that window.
    """
    windows: list[ManualDeviceWindow] = []
    eligible_set = set(eligible)
    last_eligible = eligible[-1] if eligible else now

    for delay_h in sorted(request.delay_intervals_h or []):
        target_start = now + timedelta(hours=delay_h)
        # Snap to slot boundary
        snapped = target_start.replace(
            minute=(target_start.minute // SLOT_DURATION_MIN) * SLOT_DURATION_MIN,
            second=0,
            microsecond=0,
        )
        # Verify all required slots exist in the price data
        window_slots = [
            snapped + timedelta(minutes=SLOT_DURATION_MIN * k)
            for k in range(slots_needed)
        ]
        if window_slots[-1] > last_eligible:
            continue  # Not enough price data for this delay
        if not all(st in eligible_set for st in window_slots):
            continue

        total_cost, solar_fraction = _score_window(
            window_slots, slot_info, request.peak_usage_w, request.avg_usage_w
        )
        end_time = window_slots[-1] + timedelta(minutes=SLOT_DURATION_MIN)

        windows.append(
            ManualDeviceWindow(
                start_time=snapped,
                end_time=end_time,
                total_cost=total_cost,
                solar_fraction=solar_fraction,
                delay_hours=delay_h,
            )
        )

    return windows


def _is_contiguous(slots: list[datetime]) -> bool:
    """Check that slot start times form a contiguous sequence."""
    for j in range(1, len(slots)):
        if slots[j] - slots[j - 1] != timedelta(minutes=SLOT_DURATION_MIN):
            return False
    return True


def _score_window(
    window_slots: list[datetime],
    slot_info: dict[datetime, _SlotInfo],
    peak_usage_w: float,
    avg_usage_w: float | None = None,
) -> tuple[float, float]:
    """Score a window — returns (total_cost, solar_fraction)."""
    total_cost = 0.0
    solar_count = 0
    for st in window_slots:
        slot = slot_info[st]
        total_cost += _cost_for_device_in_slot(slot, avg_usage_w or peak_usage_w)
        if slot.remaining_solar_w >= peak_usage_w:
            solar_count += 1
    solar_fraction = solar_count / len(window_slots) if window_slots else 0.0
    return total_cost, solar_fraction


def _build_manual_device_requests(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> list[ManualDeviceScheduleRequest]:
    """Build manual device schedule requests from subentries."""
    ent_reg = er.async_get(hass)
    requests = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_MANUAL_DEVICE:
            continue
        data = subentry.data

        # Read cycle duration from the number entity if available,
        # otherwise fall back to the config default.
        config_duration = float(data[CONF_CYCLE_DURATION])
        cycle_duration = _read_number_entity_value(
            hass,
            ent_reg,
            entry.entry_id,
            subentry.subentry_id,
            config_duration,
        )

        # Parse delay intervals (e.g. "3,6,9" -> [3.0, 6.0, 9.0])
        delay_intervals_h: list[float] | None = None
        raw_intervals = data.get(CONF_DELAY_INTERVALS)
        if raw_intervals:
            delay_intervals_h = _parse_delay_intervals(str(raw_intervals))

        requests.append(
            ManualDeviceScheduleRequest(
                subentry_id=subentry.subentry_id,
                name=subentry.title,
                peak_usage_w=float(data[CONF_PEAK_USAGE]),
                avg_usage_w=float(data.get(CONF_AVG_USAGE, 0)) or None,
                cycle_duration_min=cycle_duration,
                priority=int(data.get(CONF_PRIORITY, 5)),
                power_sensor=data.get(CONF_POWER_SENSOR),
                delay_intervals_h=delay_intervals_h,
            )
        )
    return requests


def _read_number_entity_value(
    hass: HomeAssistant,
    ent_reg: EntityRegistry,
    entry_id: str,
    subentry_id: str,
    default: float,
) -> float:
    """Read the cycle duration number entity value, falling back to default."""
    expected_uid = f"{entry_id}_{subentry_id}_manual_cycle_duration"
    for ent_entry in ent_reg.entities.get_entries_for_config_entry_id(entry_id):
        if ent_entry.unique_id == expected_uid:
            state = hass.states.get(ent_entry.entity_id)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    return float(state.state)
                except (ValueError, TypeError):
                    pass
            break
    return default


def _parse_delay_intervals(raw: str) -> list[float] | None:
    """Parse comma-separated delay hours string into a sorted list."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    intervals: list[float] = []
    for p in parts:
        try:
            val = float(p)
            if val > 0:
                intervals.append(val)
        except ValueError:
            continue
    return sorted(intervals) if intervals else None


def apply_reservations_to_slot_info(
    slot_info: dict[datetime, _SlotInfo],
    reservations: dict[str, tuple[datetime, datetime]],
    manual_requests: list[ManualDeviceScheduleRequest],
) -> None:
    """
    Deduct reserved manual device power from the shared solar pool.

    For each active reservation, finds the corresponding request's
    peak_usage_w and deducts it from ``remaining_solar_w`` for all
    slots within the reservation window.
    """
    request_by_id = {r.subentry_id: r for r in manual_requests}
    for subentry_id, (start, end) in reservations.items():
        req = request_by_id.get(subentry_id)
        if req is None:
            continue
        for st, slot in slot_info.items():
            slot_end = st + timedelta(minutes=SLOT_DURATION_MIN)
            if st >= start and slot_end <= end:
                slot.remaining_solar_w = max(
                    0.0, slot.remaining_solar_w - req.peak_usage_w
                )
