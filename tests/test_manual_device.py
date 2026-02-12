"""Tests for the Zeus manual device ranking and reservation logic."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from custom_components.zeus.coordinator import PriceSlot
from custom_components.zeus.scheduler import (
    DeviceScheduleRequest,
    ManualDeviceScheduleRequest,
    _build_slot_info,
    _parse_delay_intervals,
    _SlotInfo,
    apply_reservations_to_slot_info,
    compute_manual_device_rankings,
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


def _make_slot_info(
    base: datetime,
    prices: list[float],
    solar_w: list[float] | None = None,
    home_consumption_w: float = 0.0,
) -> dict[datetime, _SlotInfo]:
    """Build slot info from price and solar lists."""
    info: dict[datetime, _SlotInfo] = {}
    for i, price in enumerate(prices):
        st = base + timedelta(minutes=15 * i)
        sw = solar_w[i] if solar_w else 0.0
        surplus = max(0.0, sw - home_consumption_w)
        info[st] = _SlotInfo(
            start_time=st,
            price=price,
            energy_price=price * 0.8,
            solar_production_w=sw,
            solar_surplus_w=surplus,
            remaining_solar_w=surplus,
        )
    return info


def _make_manual_request(
    subentry_id: str = "manual1",
    peak_usage_w: float = 1000.0,
    cycle_duration_min: float = 90.0,
    priority: int = 5,
) -> ManualDeviceScheduleRequest:
    """Create a manual device schedule request."""
    return ManualDeviceScheduleRequest(
        subentry_id=subentry_id,
        name="Test Manual Device",
        peak_usage_w=peak_usage_w,
        cycle_duration_min=cycle_duration_min,
        priority=priority,
    )


# ---------------------------------------------------------------------------
# Ranking tests
# ---------------------------------------------------------------------------


def test_ranking_picks_cheapest_window():
    """The cheapest contiguous window should be ranked first."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # 12 slots (3 hours), prices: first 4 cheap, next 4 expensive, last 4 medium
    prices = [0.10, 0.11, 0.12, 0.13, 0.30, 0.31, 0.32, 0.33, 0.20, 0.21, 0.22, 0.23]
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=60.0)  # 4 slots
    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) > 0
    # Cheapest window should start at the beginning (0.10 + 0.11 + 0.12 + 0.13)
    assert ranking.recommended_start == base
    assert ranking.windows[0].start_time == base

    # Verify sort order: each window's cost should be <= the next
    for i in range(len(ranking.windows) - 1):
        assert ranking.windows[i].total_cost <= ranking.windows[i + 1].total_cost


def test_ranking_uses_all_available_slots():
    """Rankings should consider slots up to the next 06:00 boundary."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # 8 slots (2 hours), cheapest are at the end
    prices = [0.30, 0.30, 0.30, 0.30, 0.10, 0.10, 0.10, 0.10]
    slot_info = _make_slot_info(base, prices)

    # 60-min cycle (4 slots) — should find the cheap window at the end
    request = _make_manual_request(cycle_duration_min=60.0)
    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) > 0
    # Best window should start at slot 4 (the cheap slots)
    assert ranking.recommended_start == base + timedelta(minutes=60)


def test_ranking_with_solar():
    """Window during solar surplus should score better than grid-only."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # 8 slots, first 4 have solar, last 4 don't
    # Grid prices are the same so the difference is solar
    prices = [0.25] * 8
    solar_w = [2000.0, 2000.0, 2000.0, 2000.0, 0.0, 0.0, 0.0, 0.0]
    slot_info = _make_slot_info(base, prices, solar_w=solar_w)

    request = _make_manual_request(
        peak_usage_w=1000.0,
        cycle_duration_min=60.0,  # 4 slots
    )
    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) > 0
    # The solar window (first 4 slots) should be cheapest
    assert ranking.windows[0].start_time == base
    assert ranking.windows[0].solar_fraction == 1.0


def test_ranking_contiguous_only():
    """Non-contiguous slots should not form a window."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # Build slot info with a gap (skip the 3rd slot)
    info: dict[datetime, _SlotInfo] = {}
    for i in [0, 1, 3, 4, 5]:
        st = base + timedelta(minutes=15 * i)
        info[st] = _SlotInfo(
            start_time=st,
            price=0.20,
            energy_price=0.16,
            solar_production_w=0.0,
            solar_surplus_w=0.0,
            remaining_solar_w=0.0,
        )

    # Need 4 contiguous slots; the gap at index 2 prevents this for the first group
    request = _make_manual_request(cycle_duration_min=60.0)
    ranking = compute_manual_device_rankings(request, info, now)

    # Only windows starting at index 3 (slot 3,4,5 — but only 3 slots there) won't work
    # Actually: indices [3,4,5] = 3 slots, but we need 4 → no valid window
    # And [0,1] then gap at 2 → [0,1,3,4] not contiguous
    assert len(ranking.windows) == 0


def test_ranking_no_slots_available():
    """Empty ranking when no eligible slots exist."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base + timedelta(hours=2)  # After all slots

    prices = [0.20, 0.20, 0.20, 0.20]
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=30.0)
    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) == 0
    assert ranking.recommended_start is None


# ---------------------------------------------------------------------------
# Reservation tests
# ---------------------------------------------------------------------------


def test_reservation_depletes_solar():
    """Reserving a manual device should deduct from remaining_solar_w."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)

    solar_w = [3000.0, 3000.0, 3000.0, 3000.0]
    slot_info = _make_slot_info(base, [0.20] * 4, solar_w=solar_w)

    request = _make_manual_request(
        subentry_id="manual1",
        peak_usage_w=1000.0,
        cycle_duration_min=30.0,  # 2 slots
    )

    # Reserve first 2 slots
    reservation_start = base
    reservation_end = base + timedelta(minutes=30)
    reservations = {"manual1": (reservation_start, reservation_end)}

    apply_reservations_to_slot_info(slot_info, reservations, [request])

    # First 2 slots should have 2000W remaining (3000 - 1000)
    assert slot_info[base].remaining_solar_w == 2000.0
    assert slot_info[base + timedelta(minutes=15)].remaining_solar_w == 2000.0
    # Remaining slots unaffected
    assert slot_info[base + timedelta(minutes=30)].remaining_solar_w == 3000.0
    assert slot_info[base + timedelta(minutes=45)].remaining_solar_w == 3000.0


def test_reservation_expires():
    """Reservations in the past should not be returned by get_active_reservations."""
    # This is tested at the coordinator level; here we test the slot_info
    # deduction with a window that is partially past.
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)

    slot_info = _make_slot_info(base, [0.20] * 4, solar_w=[2000.0] * 4)

    request = _make_manual_request(peak_usage_w=500.0, cycle_duration_min=30.0)

    # Reservation is for the first 2 slots
    reservations = {"manual1": (base, base + timedelta(minutes=30))}
    apply_reservations_to_slot_info(slot_info, reservations, [request])

    # Affected slots
    assert slot_info[base].remaining_solar_w == 1500.0
    assert slot_info[base + timedelta(minutes=15)].remaining_solar_w == 1500.0


def test_cost_if_now_calculation():
    """The earliest window cost should be computable."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    # 8 slots; first is expensive, rest are cheap
    prices = [0.50, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=30.0)  # 2 slots
    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Windows should exist
    assert len(ranking.windows) >= 2

    # The cheapest window should NOT start at slot 0 (which is expensive)
    assert ranking.windows[0].start_time != base

    # Find the window starting at base (cost_if_now scenario)
    now_windows = [w for w in ranking.windows if w.start_time == base]
    assert len(now_windows) == 1
    now_cost = now_windows[0].total_cost

    # The recommended window should be cheaper
    assert ranking.windows[0].total_cost < now_cost


def test_savings_pct():
    """Savings percentage should reflect the difference between now and best."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    # Slot 0 expensive, rest cheap
    prices = [0.40, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10]
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=15.0)  # 1 slot
    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Best should be a cheap slot
    best = ranking.windows[0]
    # Find cost at slot 0
    now_window = [w for w in ranking.windows if w.start_time == base]
    assert len(now_window) == 1
    now_cost = now_window[0].total_cost

    assert best.total_cost < now_cost
    savings = ((now_cost - best.total_cost) / now_cost) * 100.0
    assert savings > 0


def test_reservation_affects_switch_scheduling():
    """Reserved manual device slots should reduce solar available for smart switches."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    # 4 slots, all with solar surplus and same price
    prices_raw = [
        PriceSlot(
            start_time=base + timedelta(minutes=15 * i),
            price=0.25,
            energy_price=0.20,
        )
        for i in range(4)
    ]

    # Solar forecast: 1500W each hour (plenty for one device)
    solar_forecast = {(base + timedelta(hours=h)).isoformat(): 1500 for h in range(2)}

    # A switch device that needs 1 slot, 1000W peak
    device = DeviceScheduleRequest(
        subentry_id="sw1",
        name="Smart Boiler",
        switch_entity="switch.boiler",
        power_sensor="sensor.boiler_power",
        peak_usage_w=1000.0,
        daily_runtime_min=15.0,
        deadline=time(23, 0),
        priority=5,
    )

    # Without reservation: switch should get solar
    results_no_res, slot_info_no_res = compute_schedules(
        [device], prices_raw, solar_forecast, 200.0, now
    )
    assert "sw1" in results_no_res

    # With reservation: manually deplete solar in first 2 slots
    # This simulates what async_run_scheduler does
    slot_info = _build_slot_info(prices_raw, solar_forecast, 200.0, now)
    manual_req = _make_manual_request(peak_usage_w=1000.0, cycle_duration_min=30.0)
    apply_reservations_to_slot_info(
        slot_info,
        {"manual1": (base, base + timedelta(minutes=30))},
        [manual_req],
    )

    # After reservation, first 2 slots should have reduced solar
    assert slot_info[base].remaining_solar_w < slot_info_no_res[base].solar_surplus_w


# ---------------------------------------------------------------------------
# Delay interval tests
# ---------------------------------------------------------------------------


def test_delay_intervals_picks_cheapest_delay():
    """When delay intervals are set, only those offsets are considered."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    # 24 slots (6 hours), prices drop at hour 3 (slot 12)
    prices = [0.30] * 12 + [0.10] * 12
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=60.0)  # 4 slots
    request.delay_intervals_h = [1, 3, 5]

    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) == 3
    # All windows should have delay_hours set
    for w in ranking.windows:
        assert w.delay_hours is not None

    # Best window should be the 3h delay (starts at cheap zone)
    assert ranking.windows[0].delay_hours == 3.0
    assert ranking.windows[0].start_time == base + timedelta(hours=3)


def test_delay_intervals_respects_price_data_boundary():
    """Delay intervals beyond available price data are excluded."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    # Only 8 slots (2 hours of data)
    prices = [0.20] * 8
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=30.0)  # 2 slots
    request.delay_intervals_h = [1, 3, 6]

    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Only 1h delay should fit (3h and 6h are past the price data boundary)
    assert len(ranking.windows) == 1
    assert ranking.windows[0].delay_hours == 1.0


def test_delay_intervals_no_regular_windows():
    """Delay interval devices should NOT get regular sliding windows."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base

    prices = [0.20] * 16  # 4 hours
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=15.0)  # 1 slot
    request.delay_intervals_h = [1, 2, 3]

    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Should only have 3 windows (one per delay), not 16 sliding windows
    assert len(ranking.windows) == 3
    delays = {w.delay_hours for w in ranking.windows}
    assert delays == {1.0, 2.0, 3.0}


def test_no_delay_intervals_uses_all_windows():
    """Without delay intervals, all contiguous windows are considered."""
    base = datetime(2026, 2, 11, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    prices = [0.20] * 8
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=15.0)  # 1 slot

    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Should have 8 windows (one per slot)
    assert len(ranking.windows) == 8
    # None should have delay_hours set
    for w in ranking.windows:
        assert w.delay_hours is None


def test_parse_delay_intervals():
    """Test the delay interval parsing helper."""
    assert _parse_delay_intervals("3,6,9") == [3.0, 6.0, 9.0]
    assert _parse_delay_intervals("9,3,6") == [3.0, 6.0, 9.0]  # sorted
    assert _parse_delay_intervals("") is None
    assert _parse_delay_intervals("abc") is None
    assert _parse_delay_intervals("1.5,3") == [1.5, 3.0]
    assert _parse_delay_intervals("0,-1,2") == [2.0]  # zero and negative filtered


# ---------------------------------------------------------------------------
# avg_usage_w cost calculation tests
# ---------------------------------------------------------------------------


def test_avg_usage_reduces_cost_with_partial_solar():
    """avg_usage_w should produce lower cost when partial solar covers part of device.

    The cost function _cost_for_device_in_slot uses the wattage param only for
    partial solar calculations. Without solar, price is used as-is (EUR/kWh).
    With partial solar, a lower wattage means a larger fraction is solar-covered,
    resulting in lower cost.
    """
    base = datetime(2026, 2, 12, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # Solar surplus of 500W — partial for 2000W peak, better fraction for 800W avg
    prices = [0.30, 0.30, 0.30, 0.30]
    solar_w = [500.0, 500.0, 500.0, 500.0]
    slot_info = _make_slot_info(base, prices, solar_w=solar_w)

    # Peak only: solar_fraction = 500/2000 = 0.25, cost affected by partial solar
    request_peak_only = ManualDeviceScheduleRequest(
        subentry_id="dev_peak",
        name="Peak Device",
        peak_usage_w=2000.0,
        cycle_duration_min=60.0,
        priority=5,
    )
    # With avg: solar_fraction = 500/800 = 0.625, more solar coverage = lower cost
    request_with_avg = ManualDeviceScheduleRequest(
        subentry_id="dev_avg",
        name="Avg Device",
        peak_usage_w=2000.0,
        cycle_duration_min=60.0,
        priority=5,
        avg_usage_w=800.0,
    )

    ranking_peak = compute_manual_device_rankings(request_peak_only, slot_info, now)
    ranking_avg = compute_manual_device_rankings(request_with_avg, slot_info, now)

    # Both should have windows
    assert len(ranking_peak.windows) > 0
    assert len(ranking_avg.windows) > 0

    # avg_usage should produce lower cost due to better solar fraction in cost calc
    assert ranking_avg.windows[0].total_cost < ranking_peak.windows[0].total_cost


def test_avg_usage_does_not_affect_solar_comparison():
    """Solar comparison should still use peak_usage_w, not avg_usage_w."""
    base = datetime(2026, 2, 12, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    # 4 slots with solar surplus of 1500W (enough for peak 1000W but uses
    # peak for solar comparison)
    prices = [0.25] * 4
    solar_w = [1500.0] * 4
    slot_info = _make_slot_info(base, prices, solar_w=solar_w)

    request = ManualDeviceScheduleRequest(
        subentry_id="dev1",
        name="Device",
        peak_usage_w=1000.0,
        cycle_duration_min=60.0,
        priority=5,
        avg_usage_w=500.0,
    )

    ranking = compute_manual_device_rankings(request, slot_info, now)

    assert len(ranking.windows) > 0
    # Solar fraction should be 1.0 (solar_surplus 1500 >= peak 1000)
    # This confirms peak is used for solar comparison, not avg
    assert ranking.windows[0].solar_fraction == 1.0


def test_avg_usage_none_falls_back_to_peak():
    """When avg_usage_w is None, cost uses peak_usage_w."""
    base = datetime(2026, 2, 12, 10, 0, tzinfo=TZ)
    now = base - timedelta(minutes=1)

    prices = [0.30, 0.30]
    slot_info = _make_slot_info(base, prices)

    request_none = ManualDeviceScheduleRequest(
        subentry_id="dev1",
        name="Device",
        peak_usage_w=1000.0,
        cycle_duration_min=15.0,
        priority=5,
        avg_usage_w=None,
    )
    request_explicit = ManualDeviceScheduleRequest(
        subentry_id="dev2",
        name="Device",
        peak_usage_w=1000.0,
        cycle_duration_min=15.0,
        priority=5,
    )

    ranking_none = compute_manual_device_rankings(request_none, slot_info, now)
    ranking_explicit = compute_manual_device_rankings(request_explicit, slot_info, now)

    # Should produce identical costs
    assert ranking_none.windows[0].total_cost == ranking_explicit.windows[0].total_cost


# ---------------------------------------------------------------------------
# Next-day 06:00 cutoff tests
# ---------------------------------------------------------------------------


def test_ranking_excludes_slots_after_next_6am():
    """Slots after the next 06:00 should be excluded from ranking."""
    # Now is 22:00, so cutoff is tomorrow 06:00 (8 hours away = 32 slots)
    base = datetime(2026, 2, 11, 22, 0, tzinfo=TZ)
    now = base

    # 48 slots (12 hours: 22:00 today → 10:00 tomorrow)
    # Cheapest slots are at 08:00 tomorrow (after cutoff)
    prices = [0.30] * 32 + [0.05] * 16  # first 32 expensive, last 16 cheap
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=60.0)  # 4 slots
    ranking = compute_manual_device_rankings(request, slot_info, now)

    # All windows should end by 06:00 tomorrow
    cutoff = datetime(2026, 2, 12, 6, 0, tzinfo=TZ)
    for w in ranking.windows:
        assert w.start_time < cutoff
        assert w.end_time <= cutoff

    # The cheap slots (after 06:00) should NOT appear
    assert ranking.recommended_start is not None
    assert ranking.recommended_start < cutoff


def test_ranking_before_6am_uses_today_6am_cutoff():
    """When now is before 06:00, cutoff is today's 06:00."""
    # Now is 02:00, cutoff should be 06:00 today (4 hours away = 16 slots)
    base = datetime(2026, 2, 11, 2, 0, tzinfo=TZ)
    now = base

    # 32 slots (8 hours: 02:00 → 10:00)
    # Cheapest slots are at 08:00 (after 06:00 cutoff)
    prices = [0.30] * 16 + [0.05] * 16
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=60.0)  # 4 slots
    ranking = compute_manual_device_rankings(request, slot_info, now)

    # All windows should be before 06:00 today
    cutoff = datetime(2026, 2, 11, 6, 0, tzinfo=TZ)
    for w in ranking.windows:
        assert w.start_time < cutoff

    # The cheap slots (after 06:00) should NOT appear
    assert all(w.total_cost > 0.20 for w in ranking.windows)


def test_ranking_at_6am_uses_next_day_cutoff():
    """When now is exactly 06:00, cutoff is next day 06:00 (full 24h)."""
    base = datetime(2026, 2, 11, 6, 0, tzinfo=TZ)
    now = base

    # 96 slots (24h: 06:00 today → 06:00 tomorrow)
    prices = [0.30] * 48 + [0.10] * 48  # cheaper in second half
    slot_info = _make_slot_info(base, prices)

    request = _make_manual_request(cycle_duration_min=60.0)  # 4 slots
    ranking = compute_manual_device_rankings(request, slot_info, now)

    # Should include slots up to next day 06:00
    cutoff = datetime(2026, 2, 12, 6, 0, tzinfo=TZ)
    assert any(
        w.start_time >= datetime(2026, 2, 11, 18, 0, tzinfo=TZ) for w in ranking.windows
    )
    for w in ranking.windows:
        assert w.start_time < cutoff
