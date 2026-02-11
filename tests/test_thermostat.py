"""Tests for the Zeus thermostat decision engine."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.zeus.coordinator import PriceSlot
from custom_components.zeus.scheduler import (
    ThermostatScheduleRequest,
    compute_thermostat_decisions,
)

TZ = timezone(timedelta(hours=1))


def _make_thermostat(  # noqa: PLR0913
    subentry_id: str = "therm1",
    peak_usage_w: float = 600.0,
    target_temperature: float = 20.0,
    temperature_margin: float = 1.5,
    current_temperature: float | None = 20.0,
    priority: int = 5,
    *,
    is_on: bool = False,
) -> ThermostatScheduleRequest:
    """Create a thermostat schedule request for testing."""
    return ThermostatScheduleRequest(
        subentry_id=subentry_id,
        name="Test Thermostat",
        switch_entity="switch.test_heater",
        power_sensor="sensor.test_heater_power",
        temperature_sensor="sensor.test_room_temp",
        peak_usage_w=peak_usage_w,
        target_temperature=target_temperature,
        temperature_margin=temperature_margin,
        current_temperature=current_temperature,
        priority=priority,
        is_on=is_on,
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
    """Temperature at lower bound → force on regardless of price."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # target=20, margin=1.5 → lower=18.5, upper=21.5
    therm = _make_thermostat(current_temperature=18.5)
    # Expensive prices — should still force on
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Forced on" in results["therm1"].reason


def test_force_on_below_lower_bound() -> None:
    """Temperature below lower bound → force on."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=17.0)
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Forced on" in results["therm1"].reason


def test_force_off_at_upper_bound() -> None:
    """Temperature at upper bound → force off regardless of price."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    therm = _make_thermostat(current_temperature=21.5)
    # Cheapest prices — should still force off
    slots = _make_price_slots(now, [0.01, 0.40, 0.50, 0.60])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "Forced off" in results["therm1"].reason


def test_force_off_above_upper_bound() -> None:
    """Temperature above upper bound → force off."""
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
    """Within margin + cheapest slot → heat."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=19.5, target=20, margin=1.5 → urgency ~0.67
    # Current slot is cheapest (0.05), upcoming are all more expensive
    therm = _make_thermostat(current_temperature=19.5)
    slots = _make_price_slots(now, [0.05, 0.30, 0.35, 0.40, 0.45])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on
    assert "Heating" in results["therm1"].reason


def test_optimize_expensive_price_coasts() -> None:
    """Within margin + most expensive slot + near upper → coast."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=21.0, target=20, margin=1.5 → urgency ~0.17 (near upper)
    # Current slot is most expensive, upcoming are cheaper
    therm = _make_thermostat(current_temperature=21.0)
    slots = _make_price_slots(now, [0.50, 0.10, 0.08, 0.05, 0.03])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on
    assert "Coasting" in results["therm1"].reason


def test_optimize_high_urgency_accepts_expensive() -> None:
    """Near lower bound + moderately expensive → still heats (high urgency)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=18.8, target=20, margin=1.5 → urgency ~0.9 (very close to lower)
    # Current is 3rd cheapest out of 5 upcoming → rank ~0.4
    # urgency 0.9 > rank 0.4 → heat
    therm = _make_thermostat(current_temperature=18.8)
    slots = _make_price_slots(now, [0.25, 0.10, 0.20, 0.30, 0.35, 0.40])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert results["therm1"].should_be_on


def test_optimize_low_urgency_waits_for_cheaper() -> None:
    """Near upper bound + moderately expensive → coasts (low urgency)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=21.2, target=20, margin=1.5 → urgency ~0.1 (near upper)
    # Current is most expensive → rank = 1.0
    # urgency 0.1 < rank 1.0 → coast
    therm = _make_thermostat(current_temperature=21.2)
    slots = _make_price_slots(now, [0.50, 0.10, 0.20, 0.30, 0.05])

    results = compute_thermostat_decisions([therm], slots, None, 0.0, now)

    assert not results["therm1"].should_be_on


# -----------------------------------------------------------------------
# Solar awareness
# -----------------------------------------------------------------------


def test_solar_surplus_heats() -> None:
    """Solar surplus covers device → heat (free energy)."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=20.5 → within margin, near middle
    # Even though price is expensive, solar is free
    therm = _make_thermostat(current_temperature=20.5, peak_usage_w=600.0)
    slots = _make_price_slots(now, [0.50, 0.40, 0.30, 0.20])

    results = compute_thermostat_decisions(
        [therm], slots, None, 0.0, now, live_solar_surplus_w=1000.0
    )

    assert results["therm1"].should_be_on
    assert "solar" in results["therm1"].reason.lower()


def test_solar_forecast_look_ahead_coasts() -> None:
    """Solar expected soon + low urgency → coast and wait for free solar.

    Current hour (09:xx) has no solar forecast. Next hour (10:00) has forecast
    with surplus. The 10:00 slot is within the 3-slot look-ahead window.
    The thermostat should coast and wait for the free solar.
    """
    # now is 09:35 — current slot is 09:30
    now = datetime(2026, 2, 9, 9, 35, 0, tzinfo=TZ)
    # temp=20.5, target=20, margin=1.5 → urgency ~0.33 (below threshold 0.6)
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

    # Solar forecast: 1500 Wh at hour 10 → surplus = 1500 - 500 = 1000W
    # No forecast at hour 9 → current slot (09:30) has 0 solar
    forecast_dt_10 = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    solar_forecast = {forecast_dt_10.isoformat(): 1500}

    results = compute_thermostat_decisions([therm], slots, solar_forecast, 500.0, now)

    assert not results["therm1"].should_be_on
    assert "solar" in results["therm1"].reason.lower()


def test_solar_look_ahead_high_urgency_heats_anyway() -> None:
    """Solar expected soon but urgency is high → heat anyway, can't wait."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # temp=18.8, target=20, margin=1.5 → urgency ~0.9 (above threshold 0.6)
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

    # Solar: 800W surplus — enough for one device (600W) but not two
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
    # Must decide based on price — and current slot is most expensive
    # with urgency ~0.67 and rank = 1.0 (most expensive), coast
    assert not results["b"].should_be_on


# -----------------------------------------------------------------------
# Edge cases
# -----------------------------------------------------------------------


def test_no_temperature_reading_holds_state() -> None:
    """No temperature sensor reading → hold current state."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_price_slots(now, [0.20, 0.30, 0.25, 0.15])

    # Device currently ON — should hold ON
    therm_on = _make_thermostat(current_temperature=None, is_on=True)
    results = compute_thermostat_decisions([therm_on], slots, None, 0.0, now)
    assert results["therm1"].should_be_on
    assert "holding" in results["therm1"].reason.lower()

    # Device currently OFF — should hold OFF
    therm_off = _make_thermostat(current_temperature=None, is_on=False)
    results = compute_thermostat_decisions([therm_off], slots, None, 0.0, now)
    assert not results["therm1"].should_be_on


def test_empty_thermostat_list() -> None:
    """No thermostats → empty results."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_price_slots(now, [0.20])

    results = compute_thermostat_decisions([], slots, None, 0.0, now)
    assert results == {}


def test_no_price_data_urgency_fallback() -> None:
    """No price slots → decide based on urgency alone."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    # High urgency → heat
    therm_urgent = _make_thermostat(current_temperature=18.8)
    results = compute_thermostat_decisions([therm_urgent], [], None, 0.0, now)
    assert results["therm1"].should_be_on
    assert "fallback" in results["therm1"].reason.lower()

    # Low urgency → coast
    therm_comfortable = _make_thermostat(current_temperature=21.0)
    results = compute_thermostat_decisions([therm_comfortable], [], None, 0.0, now)
    assert not results["therm1"].should_be_on


def test_thermostat_properties() -> None:
    """Test ThermostatScheduleRequest computed properties."""
    therm = _make_thermostat(
        target_temperature=20.0,
        temperature_margin=1.5,
        current_temperature=19.0,
    )

    assert therm.lower_bound == 18.5
    assert therm.upper_bound == 21.5
    # urgency = (21.5 - 19.0) / (21.5 - 18.5) = 2.5 / 3.0 ≈ 0.833
    assert abs(therm.temp_urgency - (2.5 / 3.0)) < 0.001

    # No temperature → urgency defaults to 0.5
    therm_no_temp = _make_thermostat(current_temperature=None)
    assert therm_no_temp.temp_urgency == 0.5
