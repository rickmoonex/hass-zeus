"""Scheduler for Zeus switch device management."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.components.recorder import history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

try:
    from homeassistant.components.forecast_solar.energy import (
        async_get_solar_forecast as _get_forecast,
    )
except ImportError:
    _get_forecast = None

from .const import (
    CONF_DAILY_RUNTIME,
    CONF_DEADLINE,
    CONF_ENERGY_USAGE_ENTITY,
    CONF_MIN_CYCLE_TIME,
    CONF_PEAK_USAGE,
    CONF_POWER_SENSOR,
    CONF_PRIORITY,
    CONF_PRODUCTION_ENTITY,
    CONF_SWITCH_ENTITY,
    CONF_TARGET_TEMPERATURE,
    CONF_TEMPERATURE_MARGIN,
    CONF_TEMPERATURE_SENSOR,
    SUBENTRY_HOME_MONITOR,
    SUBENTRY_SOLAR_INVERTER,
    SUBENTRY_SWITCH_DEVICE,
    SUBENTRY_THERMOSTAT_DEVICE,
)
from .coordinator import PriceCoordinator, PriceSlot

_LOGGER = logging.getLogger(__name__)

# Slot duration in minutes (matches Tibber's 15-minute slots)
SLOT_DURATION_MIN = 15

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

    @property
    def effective_usage_w(self) -> float:
        """
        Power draw used for solar consumption calculations.

        When the device is currently ON and reporting a live reading,
        use the actual draw (which may be much lower than peak, e.g.
        a washing machine in rinse vs. heat phase).  For planning
        future slots or when the device is off, use peak as a safe
        upper bound.
        """
        if self.is_on and self.actual_usage_w is not None and self.actual_usage_w > 0:
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
    target_temperature: float
    temperature_margin: float
    priority: int
    min_cycle_time_min: float = 5.0
    # Live state (populated at runtime)
    current_temperature: float | None = None
    is_on: bool = False
    actual_usage_w: float | None = None

    @property
    def lower_bound(self) -> float:
        """Temperature at which heating is forced on."""
        return self.target_temperature - self.temperature_margin

    @property
    def upper_bound(self) -> float:
        """Temperature at which heating is forced off."""
        return self.target_temperature + self.temperature_margin

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
) -> dict[str, Any] | None:
    """Get hourly solar forecast from forecast.solar integration."""
    entries = hass.config_entries.async_entries("forecast_solar")
    if not entries:
        _LOGGER.debug(
            "No forecast_solar config entries found — solar forecast unavailable"
        )
        return None

    entry = entries[0]
    _LOGGER.debug("Found forecast_solar entry: %s (id=%s)", entry.title, entry.entry_id)

    if _get_forecast is None:
        _LOGGER.warning(
            "Could not import forecast_solar.energy module — "
            "is the forecast_solar integration installed?"
        )
        return None

    try:
        result = await _get_forecast(hass, entry.entry_id)
    except Exception:  # noqa: BLE001
        _LOGGER.warning(
            "Failed to call async_get_solar_forecast for entry %s",
            entry.entry_id,
            exc_info=True,
        )
        return None

    if not result:
        _LOGGER.debug("async_get_solar_forecast returned empty result: %s", result)
        return None

    if "wh_hours" not in result:
        _LOGGER.debug(
            "Forecast result missing 'wh_hours' key. Keys present: %s",
            list(result.keys()),
        )
        return None

    wh_hours = result["wh_hours"]
    _LOGGER.debug(
        "Solar forecast retrieved: %d hourly entries, sample: %s",
        len(wh_hours),
        dict(list(wh_hours.items())[:3]) if wh_hours else "empty",
    )
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
            )
        )
    return requests


def _build_thermostat_requests(
    entry: ConfigEntry,
) -> list[ThermostatScheduleRequest]:
    """Build thermostat schedule requests from thermostat device subentries."""
    requests = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_THERMOSTAT_DEVICE:
            continue
        data = subentry.data
        requests.append(
            ThermostatScheduleRequest(
                subentry_id=subentry.subentry_id,
                name=subentry.title,
                switch_entity=data[CONF_SWITCH_ENTITY],
                power_sensor=data[CONF_POWER_SENSOR],
                temperature_sensor=data[CONF_TEMPERATURE_SENSOR],
                peak_usage_w=float(data[CONF_PEAK_USAGE]),
                target_temperature=float(data[CONF_TARGET_TEMPERATURE]),
                temperature_margin=float(data[CONF_TEMPERATURE_MARGIN]),
                priority=int(data.get(CONF_PRIORITY, 5)),
                min_cycle_time_min=float(data.get(CONF_MIN_CYCLE_TIME, 5)),
            )
        )
    return requests


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
) -> dict[str, ScheduleResult]:
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
        return results

    # Sort by priority for deterministic processing (1 = highest)
    active_devices.sort(key=lambda d: d.priority)

    _apply_deadline_forced(active_devices, states, slot_info, now, current_slot_start)
    _apply_cost_optimal(active_devices, states, slot_info, now, current_slot_start)

    for device in active_devices:
        results[device.subentry_id] = _build_result(
            device, states[device.subentry_id], slot_info, current_slot_start
        )

    return results


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


async def async_run_scheduler(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PriceCoordinator,
) -> dict[str, ScheduleResult]:
    """Run the full scheduling cycle for switch and thermostat devices."""
    results: dict[str, ScheduleResult] = {}

    # Get shared data: price slots, solar forecast, home consumption, live surplus
    price_slots = _get_all_future_slots(coordinator)
    solar_forecast = await async_get_solar_forecast(hass)
    _LOGGER.debug(
        "Scheduler: solar_forecast=%s",
        f"present ({len(solar_forecast)} entries)" if solar_forecast else "None",
    )

    home_consumption_w = _get_home_consumption(hass, entry)
    _LOGGER.debug("Scheduler: home_consumption_w=%.1f", home_consumption_w)

    live_solar_surplus_w = _get_live_solar_surplus(hass, entry, home_consumption_w)
    _LOGGER.debug(
        "Scheduler: live_solar_surplus_w=%s",
        f"{live_solar_surplus_w:.0f}" if live_solar_surplus_w is not None else "None",
    )

    now = dt_util.now()

    # --- Switch devices ---
    devices = _build_device_requests(entry)
    if devices:
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

        results.update(
            compute_schedules(
                devices,
                price_slots,
                solar_forecast,
                home_consumption_w,
                now,
                live_solar_surplus_w=live_solar_surplus_w,
            )
        )

    # --- Thermostat devices ---
    thermostats = _build_thermostat_requests(entry)
    if thermostats:
        for therm in thermostats:
            # Read live temperature
            temp_state = hass.states.get(therm.temperature_sensor)
            if temp_state and temp_state.state not in ("unknown", "unavailable"):
                try:
                    therm.current_temperature = float(temp_state.state)
                except (ValueError, TypeError):
                    therm.current_temperature = None

            # Read live switch state and power draw
            switch_state = hass.states.get(therm.switch_entity)
            therm.is_on = switch_state is not None and switch_state.state == "on"
            power_state = hass.states.get(therm.power_sensor)
            if power_state and power_state.state not in ("unknown", "unavailable"):
                try:
                    therm.actual_usage_w = float(power_state.state)
                except (ValueError, TypeError):
                    therm.actual_usage_w = None

        results.update(
            compute_thermostat_decisions(
                thermostats,
                price_slots,
                solar_forecast,
                home_consumption_w,
                now,
                live_solar_surplus_w=live_solar_surplus_w,
            )
        )

    return results


def _get_all_future_slots(coordinator: PriceCoordinator) -> list[PriceSlot]:
    """Get all available price slots from the coordinator."""
    if not coordinator.data:
        return []

    home = coordinator.get_first_home_name()
    if not home:
        return []

    return coordinator.data.get(home, [])


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
    """
    if not thermostats:
        return {}

    # Build slot info for solar awareness (reuse existing infrastructure)
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
            consumption = thermostat.actual_usage_w or thermostat.peak_usage_w
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
    slot_info: dict[datetime, _SlotInfo],  # noqa: ARG001
    current_slot_start: datetime,
) -> ScheduleResult:
    """
    Optimization logic when temperature is within the margin range.

    Uses urgency-weighted price threshold: the closer to the lower margin,
    the more willing Zeus is to accept higher-priced slots for heating.
    Solar surplus is always used (free energy). Solar forecast is considered
    for look-ahead: coast if free solar is expected soon.
    """
    urgency = thermostat.temp_urgency

    # Check solar surplus availability in current slot
    has_solar = False
    if current_slot is not None:
        has_solar = current_slot.remaining_solar_w >= thermostat.peak_usage_w

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
        upcoming_slots, thermostat.peak_usage_w
    ):
        return ScheduleResult(
            subentry_id=thermostat.subentry_id,
            should_be_on=False,
            remaining_runtime_min=0.0,
            scheduled_slots=[],
            reason="Coasting: solar surplus expected soon",
        )

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
