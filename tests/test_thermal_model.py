"""Tests for the Zeus thermal learning model."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from custom_components.zeus.thermal_model import (
    MIN_ON_HOURS_FOR_LEARNING,
    ThermalTracker,
    _compute_on_intervals,
    _compute_weighted_avg_power,
    _hour_overlap_fraction,
    blend_with_peak,
)

TZ = timezone(timedelta(hours=1))


# ---------------------------------------------------------------------------
# Helper to create mock state objects for switch history
# ---------------------------------------------------------------------------


class _MockState:
    """Minimal mock for homeassistant.core.State."""

    def __init__(self, state: str, last_changed: datetime) -> None:
        self.state = state
        self.last_changed = last_changed


# ---------------------------------------------------------------------------
# _compute_on_intervals
# ---------------------------------------------------------------------------


def test_on_intervals_basic() -> None:
    """Single on/off cycle produces one interval."""
    start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 2, 9, 12, 0, 0, tzinfo=TZ)
    changes = [
        _MockState("off", start),
        _MockState("on", start + timedelta(minutes=30)),
        _MockState("off", start + timedelta(minutes=90)),
    ]
    intervals = _compute_on_intervals(changes, start, end)
    assert len(intervals) == 1
    assert intervals[0][0] == start + timedelta(minutes=30)
    assert intervals[0][1] == start + timedelta(minutes=90)


def test_on_intervals_still_on_at_end() -> None:
    """Switch still on at end of window extends to end."""
    start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 2, 9, 12, 0, 0, tzinfo=TZ)
    changes = [
        _MockState("on", start + timedelta(minutes=30)),
    ]
    intervals = _compute_on_intervals(changes, start, end)
    assert len(intervals) == 1
    assert intervals[0][1] == end


def test_on_intervals_multiple_cycles() -> None:
    """Multiple on/off cycles produce multiple intervals."""
    start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 2, 9, 14, 0, 0, tzinfo=TZ)
    changes = [
        _MockState("on", start),
        _MockState("off", start + timedelta(hours=1)),
        _MockState("on", start + timedelta(hours=2)),
        _MockState("off", start + timedelta(hours=3)),
    ]
    intervals = _compute_on_intervals(changes, start, end)
    assert len(intervals) == 2


def test_on_intervals_empty() -> None:
    """No state changes produces empty list."""
    start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    end = datetime(2026, 2, 9, 12, 0, 0, tzinfo=TZ)
    intervals = _compute_on_intervals([], start, end)
    assert intervals == []


# ---------------------------------------------------------------------------
# _hour_overlap_fraction
# ---------------------------------------------------------------------------


def test_hour_overlap_full() -> None:
    """On for the entire hour -> fraction = 1.0."""
    hour_start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    hour_end = hour_start + timedelta(hours=1)
    intervals = [(hour_start - timedelta(hours=1), hour_end + timedelta(hours=1))]
    assert _hour_overlap_fraction(hour_start, hour_end, intervals) == 1.0


def test_hour_overlap_half() -> None:
    """On for half the hour -> fraction = 0.5."""
    hour_start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    hour_end = hour_start + timedelta(hours=1)
    mid = hour_start + timedelta(minutes=30)
    intervals = [(mid, hour_end)]
    fraction = _hour_overlap_fraction(hour_start, hour_end, intervals)
    assert abs(fraction - 0.5) < 0.001


def test_hour_overlap_none() -> None:
    """Not on during this hour -> fraction = 0.0."""
    hour_start = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    hour_end = hour_start + timedelta(hours=1)
    intervals = [(hour_end + timedelta(hours=1), hour_end + timedelta(hours=2))]
    assert _hour_overlap_fraction(hour_start, hour_end, intervals) == 0.0


# ---------------------------------------------------------------------------
# _compute_weighted_avg_power
# ---------------------------------------------------------------------------


def test_weighted_avg_power_basic() -> None:
    """Simple case: on for full hours with known mean power."""
    base = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    power_stats = [
        {"start": base.timestamp(), "mean": 400.0},
        {"start": (base + timedelta(hours=1)).timestamp(), "mean": 600.0},
    ]
    # On for both hours
    on_intervals = [(base, base + timedelta(hours=2))]
    avg, hours = _compute_weighted_avg_power(power_stats, on_intervals)
    assert avg is not None
    assert abs(avg - 500.0) < 0.1  # (400 + 600) / 2
    assert abs(hours - 2.0) < 0.01


def test_weighted_avg_power_partial_overlap() -> None:
    """On for only half of each hour."""
    base = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    power_stats = [
        {"start": base.timestamp(), "mean": 600.0},
        {"start": (base + timedelta(hours=1)).timestamp(), "mean": 600.0},
    ]
    # On from 10:30-11:30 (half of each hour)
    on_intervals = [(base + timedelta(minutes=30), base + timedelta(minutes=90))]
    avg, hours = _compute_weighted_avg_power(power_stats, on_intervals)
    assert avg is not None
    assert abs(avg - 600.0) < 0.1  # same power in both halves
    assert abs(hours - 1.0) < 0.01  # 0.5 + 0.5 hours


def test_weighted_avg_power_insufficient_data() -> None:
    """Less than MIN_ON_HOURS_FOR_LEARNING on-time returns None."""
    base = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    power_stats = [{"start": base.timestamp(), "mean": 600.0}]
    # On for only 30 minutes (0.5 hours < MIN_ON_HOURS threshold)
    on_intervals = [(base, base + timedelta(minutes=30))]
    avg, hours = _compute_weighted_avg_power(power_stats, on_intervals)
    assert avg is None
    assert hours < MIN_ON_HOURS_FOR_LEARNING


def test_weighted_avg_power_no_data() -> None:
    """Empty stats or intervals returns None."""
    avg, hours = _compute_weighted_avg_power([], [])
    assert avg is None
    assert hours == 0.0


# ---------------------------------------------------------------------------
# ThermalTracker
# ---------------------------------------------------------------------------


def test_tracker_basic_session() -> None:
    """Complete heating session computes correct Wh/degree."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    tracker.on_heater_started(19.0, now)
    tracker.on_heater_stopped(20.0, 600.0, now + timedelta(minutes=30))

    # 600W * 0.5h = 300Wh for 1.0 degree = 300 Wh/degree
    assert tracker.wh_per_degree is not None
    assert abs(tracker.wh_per_degree - 300.0) < 0.1
    assert tracker.sample_count == 1


def test_tracker_ema_convergence() -> None:
    """Multiple sessions converge the EMA toward the true value."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    # Feed 10 identical sessions: 500Wh/degree
    for i in range(10):
        t = now + timedelta(hours=i * 2)
        tracker.on_heater_started(19.0, t)
        # 500W for 1h heating 1 degree = 500 Wh/degree
        tracker.on_heater_stopped(20.0, 500.0, t + timedelta(hours=1))

    assert tracker.wh_per_degree is not None
    # After 10 sessions of identical value, EMA should be very close
    assert abs(tracker.wh_per_degree - 500.0) < 5.0
    assert tracker.sample_count == 10


def test_tracker_ignores_short_session() -> None:
    """Sessions shorter than MIN_SESSION_MINUTES are discarded."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    tracker.on_heater_started(19.0, now)
    tracker.on_heater_stopped(20.0, 600.0, now + timedelta(minutes=3))

    assert tracker.wh_per_degree is None
    assert tracker.sample_count == 0


def test_tracker_ignores_small_temp_change() -> None:
    """Sessions with delta_temp < MIN_DELTA_TEMP are discarded."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    tracker.on_heater_started(20.0, now)
    tracker.on_heater_stopped(20.1, 600.0, now + timedelta(minutes=30))

    assert tracker.wh_per_degree is None
    assert tracker.sample_count == 0


def test_tracker_ignores_zero_power() -> None:
    """Sessions with zero avg_power_w are discarded."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    tracker.on_heater_started(19.0, now)
    tracker.on_heater_stopped(20.0, 0.0, now + timedelta(minutes=30))

    assert tracker.wh_per_degree is None
    assert tracker.sample_count == 0


def test_tracker_no_start_ignores_stop() -> None:
    """Calling stop without a start is a no-op."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)
    tracker.on_heater_stopped(20.0, 600.0, now)
    assert tracker.wh_per_degree is None
    assert tracker.sample_count == 0


def test_tracker_has_session() -> None:
    """has_session reflects whether a session is in progress."""
    tracker = ThermalTracker()
    now = datetime(2026, 2, 9, 10, 0, 0, tzinfo=TZ)

    assert not tracker.has_session
    tracker.on_heater_started(19.0, now)
    assert tracker.has_session
    tracker.on_heater_stopped(20.0, 600.0, now + timedelta(minutes=30))
    assert not tracker.has_session


def test_tracker_persistence_roundtrip() -> None:
    """to_dict / from_dict preserves state."""
    tracker = ThermalTracker(wh_per_degree=350.0, sample_count=5)
    data = tracker.to_dict()
    restored = ThermalTracker.from_dict(data)

    assert restored.wh_per_degree == 350.0
    assert restored.sample_count == 5


def test_tracker_persistence_empty() -> None:
    """Empty tracker round-trips correctly."""
    tracker = ThermalTracker()
    data = tracker.to_dict()
    restored = ThermalTracker.from_dict(data)

    assert restored.wh_per_degree is None
    assert restored.sample_count == 0


# ---------------------------------------------------------------------------
# blend_with_peak
# ---------------------------------------------------------------------------


def test_blend_no_learned_returns_peak() -> None:
    """When learned is None, returns peak unchanged."""
    assert blend_with_peak(None, 1000.0, 10) == 1000.0


def test_blend_full_confidence() -> None:
    """After enough samples, returns the learned value."""
    result = blend_with_peak(400.0, 1000.0, 10)
    # weight = 1.0, so result = 400, but clamped to [100, 1200]
    assert abs(result - 400.0) < 0.1


def test_blend_partial_confidence() -> None:
    """5 out of 10 samples -> 50% blend."""
    result = blend_with_peak(400.0, 1000.0, 5)
    # weight = 0.5, result = 0.5 * 400 + 0.5 * 1000 = 700
    assert abs(result - 700.0) < 0.1


def test_blend_zero_samples() -> None:
    """0 samples -> returns peak."""
    result = blend_with_peak(400.0, 1000.0, 0)
    assert abs(result - 1000.0) < 0.1


def test_blend_clamping_lower() -> None:
    """Extremely low learned value gets clamped to peak * 0.1."""
    result = blend_with_peak(10.0, 1000.0, 10)
    # Learned=10, weight=1.0 -> blended=10, but clamped to 100 (1000 * 0.1)
    assert abs(result - 100.0) < 0.1


def test_blend_clamping_upper() -> None:
    """Learned value above peak * 1.2 gets clamped."""
    result = blend_with_peak(2000.0, 1000.0, 10)
    # Learned=2000, weight=1.0 -> blended=2000, clamped to 1200 (1000 * 1.2)
    assert abs(result - 1200.0) < 0.1


def test_blend_zero_peak_returns_zero() -> None:
    """Edge case: peak=0 returns 0."""
    assert blend_with_peak(500.0, 0.0, 10) == 0.0
