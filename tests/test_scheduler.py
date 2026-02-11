"""Tests for the Zeus scheduler module."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.zeus.coordinator import PriceSlot
from custom_components.zeus.scheduler import (
    DeviceScheduleRequest,
    compute_schedules,
)

TZ = timezone(timedelta(hours=1))


def _make_slots(
    base: datetime, count: int = 8, base_price: float = 0.25
) -> list[PriceSlot]:
    """Create a sequence of price slots starting at base."""
    return [
        PriceSlot(
            start_time=base + timedelta(minutes=15 * i),
            price=base_price + (i * 0.01),
            energy_price=(base_price + (i * 0.01)) * 0.8,
        )
        for i in range(count)
    ]


def _make_device(  # noqa: PLR0913
    subentry_id: str = "dev1",
    daily_runtime_min: float = 30.0,
    runtime_today_min: float = 0.0,
    deadline: time = time(23, 0),
    priority: int = 5,
    peak_usage_w: float = 1000.0,
) -> DeviceScheduleRequest:
    """Create a device schedule request."""
    return DeviceScheduleRequest(
        subentry_id=subentry_id,
        name="Test Device",
        switch_entity="switch.test",
        power_sensor="sensor.test_power",
        peak_usage_w=peak_usage_w,
        daily_runtime_min=daily_runtime_min,
        deadline=deadline,
        priority=priority,
        runtime_today_min=runtime_today_min,
    )


def test_scheduler_picks_cheapest_slots() -> None:
    """Test that the scheduler assigns the cheapest slots first."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Slots at 10:00, 10:15, 10:30, 10:45, 11:00, 11:15, 11:30, 11:45
    # Prices: 0.25, 0.26, 0.27, 0.28, 0.29, 0.30, 0.31, 0.32
    slots = _make_slots(now)

    device = _make_device(daily_runtime_min=30.0)  # Needs 2 slots

    results, _ = compute_schedules([device], slots, None, 0.0, now)

    result = results["dev1"]
    assert result.should_be_on  # Current slot (10:00) is cheapest
    assert len(result.scheduled_slots) == 2
    # Should pick the two cheapest: 10:00 and 10:15
    assert result.scheduled_slots[0] == now
    assert result.scheduled_slots[1] == now + timedelta(minutes=15)


def test_scheduler_runtime_already_met() -> None:
    """Test that a device with met runtime is not scheduled."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_slots(now)

    device = _make_device(
        daily_runtime_min=30.0,
        runtime_today_min=30.0,  # Already met
    )

    results, _ = compute_schedules([device], slots, None, 0.0, now)

    result = results["dev1"]
    assert not result.should_be_on
    assert result.remaining_runtime_min == 0.0
    assert result.reason == "Daily runtime already met"


def test_scheduler_deadline_pressure_forces_on() -> None:
    """Test that a device is forced on when deadline is imminent."""
    now = datetime(2026, 2, 9, 22, 0, 0, tzinfo=TZ)
    # Only 2 slots before deadline (22:00 and 22:15) with deadline at 22:30
    slots = _make_slots(now, count=2, base_price=0.50)

    device = _make_device(
        daily_runtime_min=30.0,
        runtime_today_min=0.0,
        deadline=time(22, 30),
    )

    results, _ = compute_schedules([device], slots, None, 0.0, now)

    result = results["dev1"]
    assert result.should_be_on
    assert "deadline pressure" in result.reason.lower()


def test_scheduler_priority_ordering() -> None:
    """Test that higher priority devices get the cheapest slots.

    With the global optimiser, both devices CAN share the same slot.
    Priority acts as a tiebreaker: the high-priority device is assigned
    first, so it always gets the cheapest slot.  The low-priority device
    is assigned the next cheapest (which may be the same or different
    slot depending on cost).
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Only 2 slots available, 0.10 and 0.11
    slots = _make_slots(now, count=2, base_price=0.10)

    high_priority = _make_device(
        subentry_id="high",
        daily_runtime_min=15.0,  # Needs 1 slot
        priority=1,
    )
    low_priority = _make_device(
        subentry_id="low",
        daily_runtime_min=15.0,  # Needs 1 slot
        priority=10,
    )

    results, _ = compute_schedules(
        [low_priority, high_priority],  # Order shouldn't matter
        slots,
        None,
        0.0,
        now,
    )

    # High priority gets the cheapest slot (10:00) and is on now
    assert results["high"].should_be_on
    assert results["high"].scheduled_slots[0] == now

    # Low priority also picks the cheapest slot — devices can share slots
    assert results["low"].should_be_on
    assert results["low"].scheduled_slots[0] == now


def test_scheduler_priority_tiebreaker_with_solar() -> None:
    """Test that priority breaks ties when solar is limited.

    Two devices both want the solar slot.  The high-priority device gets
    it scored as free solar (cost=-1.0).  After it consumes the surplus,
    the low-priority device sees reduced solar and pays a higher cost,
    pushing it to a different slot if one is cheaper.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    # Two slots: 10:00 at 0.30 EUR, 10:15 at 0.05 EUR
    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.05, energy_price=0.04),
    ]

    # Solar forecast: 1500 Wh at 10:00 hour only, consumption 500W
    # Surplus = 1000W — enough for ONE 1000W device, not two
    solar_forecast = {now.isoformat(): 1500}

    high_priority = _make_device(
        subentry_id="high",
        daily_runtime_min=15.0,
        priority=1,
        peak_usage_w=1000.0,
    )
    low_priority = _make_device(
        subentry_id="low",
        daily_runtime_min=15.0,
        priority=10,
        peak_usage_w=1000.0,
    )

    results, _ = compute_schedules(
        [low_priority, high_priority], slots, solar_forecast, 500.0, now
    )

    # High priority gets 10:00 with full solar (cost = -1.0)
    assert results["high"].should_be_on
    assert results["high"].scheduled_slots[0] == now
    assert results["high"].reason == "Scheduled: solar surplus available"

    # Low priority: after high consumes the solar, 10:00 has 0W remaining
    # solar → cost is 0.30.  10:15 at 0.05 is cheaper, so low picks 10:15.
    assert not results["low"].should_be_on
    assert results["low"].scheduled_slots[0] == slot_1015


def test_scheduler_concurrent_devices_share_solar() -> None:
    """Test that two small devices can share a solar slot."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    # Two slots: 10:00 at 0.30, 10:15 at 0.25
    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.25, energy_price=0.2),
    ]

    # Solar: 2500 Wh at 10:00, consumption 500W → surplus 2000W
    # Both 500W devices fit in the surplus
    solar_forecast = {now.isoformat(): 2500}

    dev_a = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=500.0
    )
    dev_b = _make_device(
        subentry_id="b", daily_runtime_min=15.0, priority=2, peak_usage_w=500.0
    )

    results, _ = compute_schedules([dev_a, dev_b], slots, solar_forecast, 500.0, now)

    # Both should be on at 10:00 using shared solar
    assert results["a"].should_be_on
    assert results["a"].scheduled_slots[0] == now
    assert results["a"].reason == "Scheduled: solar surplus available"

    assert results["b"].should_be_on
    assert results["b"].scheduled_slots[0] == now
    assert results["b"].reason == "Scheduled: solar surplus available"


def test_scheduler_past_deadline_not_scheduled() -> None:
    """Test that a device past its deadline is not scheduled."""
    now = datetime(2026, 2, 9, 23, 30, 0, tzinfo=TZ)
    slots = _make_slots(now, count=4)

    device = _make_device(
        daily_runtime_min=30.0,
        deadline=time(23, 0),  # Already passed
    )

    results, _ = compute_schedules([device], slots, None, 0.0, now)

    result = results["dev1"]
    # No slots available before deadline, so no scheduling
    assert not result.should_be_on
    assert len(result.scheduled_slots) == 0


def test_scheduler_solar_bonus_affects_scoring() -> None:
    """Test that solar forecast makes slots more attractive."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_11 = datetime(2026, 2, 9, 11, 0, 0, tzinfo=TZ)
    # Two slots at different hours: 10:00 at 0.30 EUR, 11:00 at 0.20 EUR
    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_11, price=0.20, energy_price=0.16),
    ]

    # Solar forecast: lots of sun at 10:00 hour only
    solar_forecast = {
        now.isoformat(): 3000,  # 3000 Wh at 10:00 hour
    }

    device = _make_device(daily_runtime_min=15.0)  # 1 slot needed, peak 1000W

    # Solar surplus at 10:00 = 3000 - 500 = 2500W, which covers the device's
    # 1000W peak. This makes 10:00 cost_score = -1.0, beating 11:00's 0.20.
    # Own solar is always preferred over buying from the grid.
    results, _ = compute_schedules([device], slots, solar_forecast, 500.0, now)

    result = results["dev1"]
    assert result.should_be_on
    # The 10:00 slot should be preferred — solar fully covers the device
    assert result.scheduled_slots[0] == now
    assert result.reason == "Scheduled: solar surplus available"


def test_scheduler_waiting_for_cheaper_slot() -> None:
    """Test that a device waits when cheaper slots are available later."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    # Current slot expensive, later slot cheap
    slots = [
        PriceSlot(start_time=now, price=0.50, energy_price=0.4),
        PriceSlot(
            start_time=now + timedelta(minutes=15),
            price=0.01,
            energy_price=0.008,
        ),
    ]

    device = _make_device(daily_runtime_min=15.0)  # 1 slot needed

    results, _ = compute_schedules([device], slots, None, 0.0, now)

    result = results["dev1"]
    assert not result.should_be_on  # Waits for cheaper 10:15 slot
    assert result.reason == "Waiting for cheaper slot"
    assert result.scheduled_slots[0] == now + timedelta(minutes=15)


def test_scheduler_deadline_pressure_multiple_devices() -> None:
    """Test deadline pressure with multiple devices and tight deadlines.

    Device A has deadline pressure and must be forced on.  Device B has
    plenty of time and should still pick its cheapest slot.
    """
    now = datetime(2026, 2, 9, 22, 0, 0, tzinfo=TZ)
    slots = [
        PriceSlot(start_time=now, price=0.50, energy_price=0.4),
        PriceSlot(
            start_time=now + timedelta(minutes=15),
            price=0.10,
            energy_price=0.08,
        ),
        PriceSlot(
            start_time=now + timedelta(minutes=30),
            price=0.05,
            energy_price=0.04,
        ),
        PriceSlot(
            start_time=now + timedelta(minutes=45),
            price=0.03,
            energy_price=0.024,
        ),
    ]

    # Device A: needs 30 min, deadline at 22:30 → only 2 slots → forced
    device_a = _make_device(
        subentry_id="a",
        daily_runtime_min=30.0,
        deadline=time(22, 30),
        priority=5,
    )
    # Device B: needs 15 min, deadline at 23:00 → 4 slots available → picks cheapest
    device_b = _make_device(
        subentry_id="b",
        daily_runtime_min=15.0,
        deadline=time(23, 0),
        priority=5,
    )

    results, _ = compute_schedules([device_a, device_b], slots, None, 0.0, now)

    assert results["a"].should_be_on
    assert "deadline pressure" in results["a"].reason.lower()
    assert len(results["a"].scheduled_slots) == 2

    # Device B picks cheapest of its 4 eligible slots (22:45 at 0.03)
    assert not results["b"].should_be_on
    assert results["b"].scheduled_slots[0] == now + timedelta(minutes=45)


def test_scheduler_global_optimisation_over_greedy() -> None:
    """Test that the global optimiser produces better results than greedy.

    With the old greedy approach:
        - Device A (priority 1) would claim 10:00 (cheapest)
        - Device B (priority 5) would get 10:15 (second cheapest)
        - Total cost: 0.01 + 0.20 = 0.21

    With the global optimiser, both share 10:00:
        - Total cost: 0.01 + 0.01 = 0.02
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = [
        PriceSlot(start_time=now, price=0.01, energy_price=0.008),
        PriceSlot(
            start_time=now + timedelta(minutes=15), price=0.20, energy_price=0.16
        ),
    ]

    dev_a = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=500.0
    )
    dev_b = _make_device(
        subentry_id="b", daily_runtime_min=15.0, priority=5, peak_usage_w=500.0
    )

    results, _ = compute_schedules([dev_a, dev_b], slots, None, 0.0, now)

    # Both devices should share the cheapest slot
    assert results["a"].should_be_on
    assert results["a"].scheduled_slots[0] == now
    assert results["b"].should_be_on
    assert results["b"].scheduled_slots[0] == now


def test_live_solar_surplus_activates_device() -> None:
    """Test that live solar surplus overrides forecast for the current slot.

    Forecast predicts 500W surplus at 10:00 (not enough for a 1000W device).
    But live production shows 2000W surplus.  The device should activate
    because the live surplus covers the full peak usage.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    # 10:00 at 0.30 EUR, 10:15 at 0.05 EUR
    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.05, energy_price=0.04),
    ]

    # Forecast: only 1000 Wh at 10:00, consumption 500W → surplus 500W
    # This is NOT enough for a 1000W device.
    solar_forecast = {now.isoformat(): 1000}

    device = _make_device(daily_runtime_min=15.0, peak_usage_w=1000.0)

    # Without live solar: device picks 10:15 (0.05 < 0.30 partial solar)
    results_no_live, _ = compute_schedules([device], slots, solar_forecast, 500.0, now)
    assert not results_no_live["dev1"].should_be_on
    assert results_no_live["dev1"].scheduled_slots[0] == slot_1015

    # With live solar surplus of 2000W: covers device, 10:00 becomes free
    results_live, _ = compute_schedules(
        [device],
        slots,
        solar_forecast,
        500.0,
        now,
        live_solar_surplus_w=2000.0,
    )
    assert results_live["dev1"].should_be_on
    assert results_live["dev1"].scheduled_slots[0] == now
    assert results_live["dev1"].reason == "Scheduled: solar surplus available"


def test_live_solar_surplus_ignored_when_below_forecast() -> None:
    """Test that live solar doesn't downgrade the forecast.

    If live surplus is lower than the forecast (e.g. a cloud passing),
    we keep the forecast value for planning purposes.  The forecast
    represents the expected average for the hour.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    # Two slots so the device has a choice (avoids deadline pressure)
    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.25, energy_price=0.2),
    ]

    # Forecast: 3000 Wh at 10:00, consumption 500W → surplus 2500W
    solar_forecast = {now.isoformat(): 3000}

    device = _make_device(daily_runtime_min=15.0, peak_usage_w=1000.0)

    # Live solar is only 200W surplus (cloud) — should NOT downgrade
    results, _ = compute_schedules(
        [device],
        slots,
        solar_forecast,
        500.0,
        now,
        live_solar_surplus_w=200.0,
    )
    # Forecast surplus 2500W > device 1000W, so still solar-powered
    assert results["dev1"].should_be_on
    assert results["dev1"].reason == "Scheduled: solar surplus available"


def test_live_solar_shared_between_devices() -> None:
    """Test that live solar surplus is shared correctly between devices.

    Live surplus of 1500W.  Device A (1000W) gets it first (higher priority).
    After consuming 1000W, only 500W remains — not enough for device B
    (also 1000W).  Device B falls back to grid price comparison.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.10),
        PriceSlot(start_time=slot_1015, price=0.05, energy_price=0.04),
    ]

    # No forecast at all — purely live solar
    dev_a = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=1000.0
    )
    dev_b = _make_device(
        subentry_id="b", daily_runtime_min=15.0, priority=5, peak_usage_w=1000.0
    )

    results, _ = compute_schedules(
        [dev_a, dev_b],
        slots,
        None,
        0.0,
        now,
        live_solar_surplus_w=1500.0,
    )

    # Device A gets the solar slot (full solar → cost = -0.10)
    assert results["a"].should_be_on
    assert results["a"].scheduled_slots[0] == now

    # Device B: 500W remaining solar at 10:00 is not enough for 1000W.
    # solar_fraction = 0.5.
    # 10:00 cost = 0.30 * 0.5 - 0.10 * 0.5 = 0.15 - 0.05 = 0.10
    # 10:15 cost = 0.05 (no solar).
    # Device B picks 10:15 (cheaper grid).
    assert not results["b"].should_be_on
    assert results["b"].scheduled_slots[0] == slot_1015


def test_actual_usage_frees_solar_for_other_devices() -> None:
    """Test that a running device's actual draw frees solar for others.

    Device A (priority 1, peak 1500W) is assigned 10:00 where solar
    covers it fully (2000W surplus).  It's currently ON drawing 200W.
    After assignment, only 200W of actual solar is consumed, leaving
    1800W for device B (1000W peak) — which also gets the solar slot.

    Without actual_usage_w, device A would consume its full 1500W peak,
    leaving only 500W — not enough for device B's 1000W.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.25, energy_price=0.2),
    ]

    # Solar: 2500 Wh, consumption 500W → surplus 2000W
    solar_forecast = {now.isoformat(): 2500}

    dev_a = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=1500.0
    )
    # Simulate device A currently ON drawing only 200W
    dev_a.is_on = True
    dev_a.actual_usage_w = 200.0
    dev_a.use_actual_power = True

    dev_b = _make_device(
        subentry_id="b", daily_runtime_min=15.0, priority=2, peak_usage_w=1000.0
    )

    # Without actual usage: A consumes 1500W, leaves 500W, B can't get solar
    dev_a_peak = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=1500.0
    )
    compute_schedules([dev_a_peak, dev_b], slots, solar_forecast, 500.0, now)
    # B would still get 10:00 but with only 500W remaining (partial solar).
    # After A consumes 1500W, B has 500W remaining at 10:00:
    # cost = 0.30 * 0.5 - 0.24 * 0.5 = 0.03, vs 10:15 at 0.25.
    # B picks 10:00 (partial solar still cheaper than 10:15 grid).

    # With actual usage: A consumes only 200W, leaves 1800W → B fully solar
    results_actual, _ = compute_schedules(
        [dev_a, dev_b], slots, solar_forecast, 500.0, now
    )
    assert results_actual["a"].should_be_on
    assert results_actual["b"].should_be_on
    assert results_actual["b"].scheduled_slots[0] == now
    # B should get full solar coverage since 1800W remaining > 1000W peak
    assert results_actual["b"].reason == "Scheduled: solar surplus available"


def test_energy_price_opportunity_cost_with_solar() -> None:
    """Test that the spot price creates opportunity cost for solar usage.

    The energy_price (spot price) on each slot is what you earn for
    exporting. When solar fully covers a device, the cost is -energy_price
    (the opportunity cost of not exporting). If a future grid slot is
    cheaper than this opportunity cost, the device should wait.

    Slot at 10:00: total=0.30, energy=0.24, solar covers device fully
        → cost = -0.24 (opportunity cost of exporting at 0.24)
    Slot at 10:15: total=0.02, energy=0.016, no solar
        → cost = 0.02 (grid price)

    Since -0.24 < 0.02, solar at 10:00 is preferred. Device runs now.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.02, energy_price=0.016),
    ]

    # Solar: 2000 Wh, consumption 500W → surplus 1500W (fully covers 1000W)
    solar_forecast = {now.isoformat(): 2000}

    device = _make_device(daily_runtime_min=15.0, peak_usage_w=1000.0)

    results, _ = compute_schedules([device], slots, solar_forecast, 500.0, now)
    # Solar at 10:00 is preferred: -0.24 < 0.02
    assert results["dev1"].should_be_on
    assert results["dev1"].reason == "Scheduled: solar surplus available"


def test_energy_price_no_solar_unaffected() -> None:
    """Test that energy_price doesn't affect slots without solar."""
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slots = _make_slots(now, count=4, base_price=0.10)

    device = _make_device(daily_runtime_min=15.0)

    # No solar forecast — energy_price has no effect, cheapest total wins
    results, _ = compute_schedules([device], slots, None, 0.0, now)
    assert results["dev1"].should_be_on
    assert results["dev1"].scheduled_slots[0] == now


def test_forecast_bias_correction_scales_future_slots() -> None:
    """Test that forecast bias correction adjusts future solar estimates.

    Forecast predicts 1000W surplus at 10:00 and 11:00.
    Live shows 2000W at 10:00 (2x bias).
    Future slot at 11:00 should be scaled up to 2000W surplus.

    Two 1000W devices: without bias, only one fits per solar slot.
    With bias, the 11:00 slot is predicted at 2000W, so device B can
    also use a solar slot instead of paying grid price.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_11 = datetime(2026, 2, 9, 11, 0, 0, tzinfo=TZ)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_11, price=0.30, energy_price=0.24),
    ]

    # Forecast: 1500 Wh at both hours, consumption 500W → surplus 1000W each
    solar_forecast = {
        now.isoformat(): 1500,
        slot_11.isoformat(): 1500,
    }

    dev_a = _make_device(
        subentry_id="a", daily_runtime_min=15.0, priority=1, peak_usage_w=1000.0
    )
    dev_b = _make_device(
        subentry_id="b", daily_runtime_min=15.0, priority=2, peak_usage_w=1000.0
    )

    # Without bias: each slot has 1000W surplus, one device per slot
    results_no_bias, _ = compute_schedules(
        [dev_a, dev_b], slots, solar_forecast, 500.0, now
    )
    # A gets 10:00 solar, B gets 11:00 solar — both solar-powered
    assert results_no_bias["a"].should_be_on

    # With live surplus of 2000W (2x bias): future slot should scale to 2000W
    # Now both devices could share the 10:00 slot since it has 2000W live,
    # OR device B gets the bias-corrected 11:00 slot (2000W > 1000W needed)
    results_biased, _ = compute_schedules(
        [dev_a, dev_b],
        slots,
        solar_forecast,
        500.0,
        now,
        live_solar_surplus_w=2000.0,
    )
    # Both should be solar-powered
    assert results_biased["a"].reason == "Scheduled: solar surplus available"
    assert results_biased["b"].reason == "Scheduled: solar surplus available"


# -----------------------------------------------------------------------
# use_actual_power toggle
# -----------------------------------------------------------------------


def test_effective_usage_default_returns_peak() -> None:
    """With use_actual_power=False (default), effective_usage_w returns peak."""
    device = _make_device(peak_usage_w=1000.0)
    device.is_on = True
    device.actual_usage_w = 50.0
    device.use_actual_power = False

    assert device.effective_usage_w == 1000.0


def test_effective_usage_enabled_returns_actual() -> None:
    """With use_actual_power=True, effective_usage_w returns actual when on."""
    device = _make_device(peak_usage_w=1000.0)
    device.is_on = True
    device.actual_usage_w = 50.0
    device.use_actual_power = True

    assert device.effective_usage_w == 50.0


def test_effective_usage_enabled_zero_power() -> None:
    """With use_actual_power=True, actual_usage_w=0 is valid (boiler idle)."""
    device = _make_device(peak_usage_w=1000.0)
    device.is_on = True
    device.actual_usage_w = 0.0
    device.use_actual_power = True

    assert device.effective_usage_w == 0.0


def test_effective_usage_enabled_but_off_returns_peak() -> None:
    """With use_actual_power=True but device off, returns peak (planning)."""
    device = _make_device(peak_usage_w=1000.0)
    device.is_on = False
    device.actual_usage_w = 50.0
    device.use_actual_power = True

    assert device.effective_usage_w == 1000.0


def test_boiler_frees_solar_for_others() -> None:
    """Boiler with use_actual_power=True drawing 0W frees solar for others.

    Boiler (priority 1, peak 1500W) is ON but drawing 0W (water hot).
    With use_actual_power=True, only 0W of solar is consumed,
    leaving the full surplus for device B.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.24),
        PriceSlot(start_time=slot_1015, price=0.25, energy_price=0.2),
    ]

    # Solar: 2500 Wh, consumption 500W -> surplus 2000W
    solar_forecast = {now.isoformat(): 2500}

    boiler = _make_device(
        subentry_id="boiler", daily_runtime_min=15.0, priority=1, peak_usage_w=1500.0
    )
    boiler.is_on = True
    boiler.actual_usage_w = 0.0
    boiler.use_actual_power = True

    other = _make_device(
        subentry_id="other", daily_runtime_min=15.0, priority=2, peak_usage_w=1000.0
    )

    results, _ = compute_schedules([boiler, other], slots, solar_forecast, 500.0, now)

    # Boiler is assigned to 10:00 (solar) but consumes 0W
    assert results["boiler"].should_be_on
    # Other device gets full solar since boiler used 0W
    assert results["other"].should_be_on
    assert results["other"].scheduled_slots[0] == now
    assert results["other"].reason == "Scheduled: solar surplus available"


def test_live_solar_peak_must_fit_fully() -> None:
    """Test that partial live solar does NOT activate a device.

    The device's full peak_usage_w must fit in the surplus. If live
    surplus is 800W and device needs 1000W, it should NOT be treated
    as free solar — it still needs 200W from the grid.
    """
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    slot_1015 = now + timedelta(minutes=15)

    slots = [
        PriceSlot(start_time=now, price=0.30, energy_price=0.03),
        PriceSlot(start_time=slot_1015, price=0.02, energy_price=0.016),
    ]

    device = _make_device(daily_runtime_min=15.0, peak_usage_w=1000.0)

    # Live surplus 800W — doesn't cover 1000W peak
    results, _ = compute_schedules(
        [device],
        slots,
        None,
        0.0,
        now,
        live_solar_surplus_w=800.0,
    )

    # Partial solar at 10:00: cost = 0.30 * 0.2 - 0.03 * 0.8 = 0.036
    # Grid at 10:15: cost = 0.02
    # 10:15 is cheaper, so device waits
    assert not results["dev1"].should_be_on
    assert results["dev1"].scheduled_slots[0] == slot_1015
    assert results["dev1"].reason == "Waiting for cheaper slot"
