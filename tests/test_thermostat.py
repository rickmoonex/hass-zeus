"""Tests for the Zeus thermostat decision engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from datetime import time as dt_time

from custom_components.zeus.coordinator import PriceSlot
from custom_components.zeus.scheduler import (
    DeviceScheduleRequest,
    ThermostatScheduleRequest,
    compute_schedules,
    compute_thermostat_decisions,
)

TZ = timezone(timedelta(hours=1))


def _make_thermostat(  # noqa: PLR0913
    subentry_id: str = "therm1",
    peak_usage_w: float = 600.0,
    target_temp_low: float = 18.5,
    target_temp_high: float = 21.5,
    current_temperature: float | None = 20.0,
    priority: int = 5,
    *,
    is_on: bool = False,
    hvac_mode: str = "heat",
) -> ThermostatScheduleRequest:
    """Create a thermostat schedule request for testing."""
    return ThermostatScheduleRequest(
        subentry_id=subentry_id,
        name="Test Thermostat",
        switch_entity="switch.test_heater",
        power_sensor="sensor.test_heater_power",
        temperature_sensor="sensor.test_room_temp",
        peak_usage_w=peak_usage_w,
        target_temp_low=target_temp_low,
        target_temp_high=target_temp_high,
        current_temperature=current_temperature,
        priority=priority,
        is_on=is_on,
        hvac_mode=hvac_mode,
    )


def _make_price_slots(
    base: datetime,
    prices: list[float],
) -> list[PriceSlot]:
    """Create price slots from a list of prices."""
    return [
        PriceSlot(
            start_time=base + timedelta(minutes=15 * i),
            price=p,
            energy_price=p * 0.8,
        )
        for i, p in enumerate(prices)
    ]


# -----------------------------------------------------------------------
# Tier 1 & 2: Force on / Force off at boundaries
# -----------------------------------------------------------------------


def test_force_on_at_lower_bound() -> None:
    """Temperature at lower bound -> force on regardless of price."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # target_temp_low=18.5, target_temp_high=21.5
    therm = _make_thermostat(current_temperature=18.5)
    # Expensive prices -- should still force on
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Forced on" in results["therm1"].reason


def test_force_on_below_lower_bound() -> None:
    """Temperature below lower bound -> force on."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=17.0)
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Forced on" in results["therm1"].reason


def test_force_off_at_upper_bound() -> None:
    """Temperature at upper bound -> force off regardless of price."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=21.5)
    # Cheapest prices -- should still force off
    slots = _make_price_slots(now, [0.01, 0.40, 0.50, 0.60])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "Forced off" in results["therm1"].reason


def test_force_off_above_upper_bound() -> None:
    """Temperature above upper bound -> force off."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=23.0)
    slots = _make_price_slots(now, [0.01, 0.40, 0.50, 0.60])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "Forced off" in results["therm1"].reason


# -----------------------------------------------------------------------
# Tier 3: Optimization within margins
# -----------------------------------------------------------------------


def test_optimize_cheap_price_heats() -> None:
    """Within margin + cheapest slot -> heat."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=19.5, low=18.5, high=21.5 -> urgency ~0.67
    # Current slot is cheapest (0.05), upcoming are all more expensive
    therm = _make_thermostat(current_temperature=19.5)
    slots = _make_price_slots(now, [0.05, 0.30, 0.35, 0.40, 0.45])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Heating" in results["therm1"].reason


def test_optimize_expensive_price_coasts() -> None:
    """Within margin + most expensive slot + near upper -> coast."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=21.0, low=18.5, high=21.5 -> urgency ~0.17 (near upper)
    # Current slot is most expensive, upcoming are cheaper
    therm = _make_thermostat(current_temperature=21.0)
    slots = _make_price_slots(now, [0.50, 0.10, 0.08, 0.05, 0.03])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "Coasting" in results["therm1"].reason


def test_optimize_high_urgency_accepts_expensive() -> None:
    """Near lower bound + moderately expensive -> still heats (high urgency)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=18.8, low=18.5, high=21.5 -> urgency ~0.9 (very close to lower)
    # Current is 3rd cheapest out of 5 upcoming -> rank ~0.4
    # urgency 0.9 > rank 0.4 -> heat
    therm = _make_thermostat(current_temperature=18.8)
    slots = _make_price_slots(now, [0.25, 0.10, 0.20, 0.30, 0.35, 0.40])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on


def test_optimize_low_urgency_waits_for_cheaper() -> None:
    """Near upper bound + moderately expensive -> coasts (low urgency)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=21.2, low=18.5, high=21.5 -> urgency ~0.1 (near upper)
    # Current is most expensive -> rank = 1.0
    # urgency 0.1 < rank 1.0 -> coast
    therm = _make_thermostat(current_temperature=21.2)
    slots = _make_price_slots(now, [0.50, 0.10, 0.20, 0.30, 0.05])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on


# -----------------------------------------------------------------------
# Solar awareness
# -----------------------------------------------------------------------


def test_solar_surplus_heats() -> None:
    """Solar surplus covers device -> heat (free energy)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=20.5 -> within margin, near middle
    # Even though price is expensive, solar is free
    therm = _make_thermostat(current_temperature=20.5, peak_usage_w=600.0)
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, live_solar_surplus_w=1000.0
    )

    assert results["therm1"].should_be_on
    assert "solar" in results["therm1"].reason.lower()


def test_solar_forecast_look_ahead_coasts() -> None:
    """Solar expected soon + low urgency -> coast and wait for free solar.

    Current hour (09:xx) has no solar forecast. Next hour (10:00) has forecast
    with surplus. The 10:00 slot is within the 3-slot look-ahead window.
    The thermostat should coast and wait for the free solar.
    """
    # now is 09:35 -- current slot is 09:30
    now = datetime(2026, 2, 9, 9, 35, 0, tzinfo=TZ)
    # temp=20.5, low=18.5, high=21.5 -> urgency ~0.33 (below threshold 0.6)
    therm = _make_thermostat(current_temperature=20.5, peak_usage_w=600.0)
    # Slots: 09:30 (current), 09:45, 10:00
    # All same-ish price so price rank won't trigger heating
    base = datetime(2026, 2, 9, 9, 30, 0, tzinfo=TZ)
    slots = [
        PriceSlot(start_time=base, price=0.25, energy_price=0.20),
        PriceSlot(
            start_time=base + timedelta(minutes=15),
            price=0.25,
            energy_price=0.20,
        ),
        PriceSlot(
            start_time=base + timedelta(minutes=30),
            price=0.25,
            energy_price=0.20,
        ),
    ]

    # Solar forecast: 1500 Wh at hour 10 -> surplus = 1500 - 500 = 1000W
    # No forecast at hour 9 -> current slot (09:30) has 0 solar
    forecast_dt_10 = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    solar_forecast = {forecast_dt_10.isoformat(): 1500}

    results = compute_thermostat_decisions([therm], slots, solar_forecast, 500.0, now)

    assert not results["therm1"].should_be_on
    assert "solar" in results["therm1"].reason.lower()


def test_solar_look_ahead_high_urgency_heats_anyway() -> None:
    """Solar expected soon but urgency is high -> heat anyway, can't wait."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=18.8, low=18.5, high=21.5 -> urgency ~0.9 (above threshold 0.6)
    therm = _make_thermostat(current_temperature=18.8, peak_usage_w=600.0)
    slots = _make_price_slots(now, [0.20, 0.20, 0.20, 0.20])

    # Solar forecast with surplus in upcoming slot
    forecast_dt = now.replace(minute=0, second=0)
    solar_forecast = {forecast_dt.isoformat(): 1500}

    results = compute_thermostat_decisions([therm], slots, solar_forecast, 500.0, now)

    # Urgency is 0.9, above _SOLAR_WAIT_URGENCY_THRESHOLD (0.6)
    # Should heat now despite upcoming solar
    assert results["therm1"].should_be_on


# -----------------------------------------------------------------------
# Multi-device solar sharing
# -----------------------------------------------------------------------


def test_multi_thermostat_solar_sharing() -> None:
    """Higher priority thermostat gets solar first, lower priority gets less."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Both are within margin (not forced), both have moderate urgency
    therm_a = _make_thermostat(
        subentry_id="a",
        current_temperature=19.5,
        peak_usage_w=600.0,
        priority=1,
    )
    therm_b = _make_thermostat(
        subentry_id="b",
        current_temperature=19.5,
        peak_usage_w=600.0,
        priority=5,
    )

    # Solar: 800W surplus -- enough for one device (600W) but not two
    # Most expensive current slot to ensure only solar triggers heating
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20, 0.10])

    results = compute_thermostat_decisions(
        [therm_a, therm_b],
        slots,
        None,
        0.0,
        now,
        live_solar_surplus_w=800.0,
    )

    # A gets solar (priority 1)
    assert results["a"].should_be_on
    assert "solar" in results["a"].reason.lower()

    # B sees 200W remaining (800 - 600), not enough for 600W peak
    # Must decide based on price -- and current slot is most expensive
    # with urgency ~0.67 and rank = 1.0 (most expensive), coast
    assert not results["b"].should_be_on


# -----------------------------------------------------------------------
# HVAC mode OFF
# -----------------------------------------------------------------------


def test_hvac_mode_off_does_not_heat() -> None:
    """When HVAC mode is off, thermostat should never heat."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Temperature below lower bound -- would normally force on
    therm = _make_thermostat(current_temperature=17.0, hvac_mode="off")
    slots = _make_price_slots(now, [0.01, 0.02, 0.03, 0.04])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "off" in results["therm1"].reason.lower()


def test_hvac_mode_off_with_solar_does_not_heat() -> None:
    """Even with solar surplus, HVAC off means no heating."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(
        current_temperature=19.0, peak_usage_w=600.0, hvac_mode="off"
    )
    slots = _make_price_slots(now, [0.01, 0.02, 0.03, 0.04])

    results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, live_solar_surplus_w=2000.0
    )

    assert not results["therm1"].should_be_on


# -----------------------------------------------------------------------
# Shared solar pool with switch devices
# -----------------------------------------------------------------------


def test_shared_slot_info_depleted_by_switch_devices() -> None:
    """Thermostat receives pre-depleted slot_info from switch scheduler."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_price_slots(now, [0.30, 0.25, 0.20, 0.15])

    # Switch device: 1000W, needs 1 slot, gets scheduled into current slot
    switch_dev = DeviceScheduleRequest(
        subentry_id="switch1",
        name="Switch Device",
        switch_entity="switch.test",
        power_sensor="sensor.test_power",
        peak_usage_w=1000.0,
        daily_runtime_min=15.0,
        deadline=dt_time(23, 0),
        priority=1,
    )

    # Solar surplus: 1200W -- enough for switch (1000W) but after deduction
    # only 200W remains, not enough for thermostat (600W)
    switch_results, shared_slot_info = compute_schedules(
        [switch_dev], slots, None, 0.0, now, live_solar_surplus_w=1200.0
    )

    # Switch should get solar
    assert switch_results["switch1"].should_be_on
    assert "solar" in switch_results["switch1"].reason.lower()

    # Thermostat uses the same depleted slot_info
    therm = _make_thermostat(current_temperature=20.0, peak_usage_w=600.0, priority=5)
    therm_results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, slot_info=shared_slot_info
    )

    # Thermostat should NOT get solar (only 200W remains after switch)
    # It should decide based on price instead
    result = therm_results["therm1"]
    assert "solar" not in result.reason.lower() or not result.should_be_on


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


def test_no_temperature_reading_holds_state() -> None:
    """No temperature sensor reading -> hold current state."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_price_slots(now, [0.20, 0.30, 0.25, 0.15])

    # Device currently ON -- should hold ON
    therm_on = _make_thermostat(current_temperature=None, is_on=True)
    results = compute_thermostat_decisions([therm_on], slots, None, 0.0, now)
    assert results["therm1"].should_be_on
    assert "holding" in results["therm1"].reason.lower()

    # Device currently OFF -- should hold OFF
    therm_off = _make_thermostat(current_temperature=None, is_on=False)
    results = compute_thermostat_decisions([therm_off], slots, None, 0.0, now)
    assert not results["therm1"].should_be_on


def test_empty_thermostat_list() -> None:
    """No thermostats -> empty results."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_price_slots(now, [0.20])

    results = compute_thermostat_decisions([], slots, None, 0.0, now)
    assert results == {}


def test_no_price_data_urgency_fallback() -> None:
    """No price slots -> decide based on urgency alone."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    # High urgency -> heat
    therm_urgent = _make_thermostat(current_temperature=18.8)
    results = compute_thermostat_decisions([therm_urgent], [], None, 0.0, now)
    assert results["therm1"].should_be_on
    assert "fallback" in results["therm1"].reason.lower()

    # Low urgency -> coast
    therm_comfortable = _make_thermostat(current_temperature=21.0)
    results = compute_thermostat_decisions([therm_comfortable], [], None, 0.0, now)
    assert not results["therm1"].should_be_on


def test_thermostat_properties() -> None:
    """Test ThermostatScheduleRequest computed properties."""
    therm = _make_thermostat(
        target_temp_low=18.5,
        target_temp_high=21.5,
        current_temperature=19.0,
    )

    assert therm.lower_bound == 18.5
    assert therm.upper_bound == 21.5
    # urgency = (21.5 - 19.0) / (21.5 - 18.5) = 2.5 / 3.0 ~ 0.833
    assert abs(therm.temp_urgency - (2.5 / 3.0)) < 0.001

    # No temperature -> urgency defaults to 0.5
    therm_no_temp = _make_thermostat(current_temperature=None)
    assert therm_no_temp.temp_urgency == 0.5


# -----------------------------------------------------------------------
# Effective power (learned vs. peak)
# -----------------------------------------------------------------------


def test_effective_power_uses_learned_when_available() -> None:
    """When learned_avg_power_w is set, effective_power_w should use it."""
    therm = _make_thermostat(peak_usage_w=600.0)
    therm.learned_avg_power_w = 400.0

    assert therm.effective_power_w == 400.0


def test_effective_power_falls_back_to_peak() -> None:
    """When learned_avg_power_w is None, effective_power_w returns peak."""
    therm = _make_thermostat(peak_usage_w=600.0)
    assert therm.learned_avg_power_w is None
    assert therm.effective_power_w == 600.0


def test_solar_decision_with_learned_power() -> None:
    """Learned power < solar surplus -> heats on solar even if peak wouldn't fit."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Peak is 600W but learned is 400W.  Solar surplus is 500W.
    # With peak: 500 < 600 -> no solar.
    # With learned: 500 >= 400 -> solar!
    therm = _make_thermostat(current_temperature=20.0, peak_usage_w=600.0)
    therm.learned_avg_power_w = 400.0
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20, 0.10])

    results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, live_solar_surplus_w=500.0
    )

    assert results["therm1"].should_be_on
    assert "solar" in results["therm1"].reason.lower()


def test_solar_decision_without_learned_power_uses_peak() -> None:
    """Without learned power, solar check uses peak — 500W < 600W -> no solar."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=20.0, peak_usage_w=600.0)
    # No learned power set
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20, 0.10])

    results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, live_solar_surplus_w=500.0
    )

    # 500W < 600W peak -> no solar
    # Price-based: most expensive current slot with urgency ~0.33 -> coast
    assert (
        not results["therm1"].should_be_on
        or "solar" not in results["therm1"].reason.lower()
    )


# -----------------------------------------------------------------------
# Thermal headroom
# -----------------------------------------------------------------------


def test_thermal_headroom_coasts_when_safe() -> None:
    """When wh_per_degree is known and headroom is large, coast for cheaper slot."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=20.0, low=18.5 -> 1.5°C above lower bound
    # wh_per_degree=500, effective_power=600W
    # coast_time = 1.5 * 500 / 600 = 1.25h ... that's < 2h threshold
    # Let's use a wider margin: temp=21.0, low=18.5 -> 2.5°C
    # coast_time = 2.5 * 500 / 600 = 2.08h > 2h threshold
    therm = _make_thermostat(current_temperature=21.0, peak_usage_w=600.0)
    therm.wh_per_degree = 500.0

    # Current slot expensive (0.50), cheaper slot (0.10) coming within coast window
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20, 0.10])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "headroom" in results["therm1"].reason.lower()


def test_thermal_headroom_heats_when_urgent() -> None:
    """When wh_per_degree shows very little time to lower bound, heat immediately."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=18.7, low=18.5 -> 0.2°C above lower bound
    # wh_per_degree=500, effective_power=600W
    # coast_time = 0.2 * 500 / 600 = 0.167h < 0.5h threshold -> urgent
    therm = _make_thermostat(
        current_temperature=18.7,
        target_temp_low=18.5,
        target_temp_high=21.5,
        peak_usage_w=600.0,
    )
    therm.wh_per_degree = 500.0

    # Even with expensive prices, should heat due to low headroom
    slots = _make_price_slots(now, [0.50, 0.10, 0.08, 0.05, 0.03])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "headroom" in results["therm1"].reason.lower()
