"""
Thermal learning model for Zeus thermostat devices.

Provides two self-learning capabilities:

1. **Learned average power draw** — queries the HA recorder to compute
   the time-weighted average power consumption of a heater during on-hours
   over a configurable window (default 7 days).  Used in place of the
   static ``peak_usage_w`` for more accurate solar surplus decisions.

2. **Energy per degree (Wh/°C)** — tracks heating sessions (on→off cycles)
   and maintains an exponential moving average of how much energy is needed
   to raise the zone temperature by one degree Celsius.  Enables the
   scheduler to estimate thermal headroom and make smarter coast-vs-heat
   decisions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import history
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# Learning configuration
LEARNING_WINDOW_DAYS = 7
MIN_ON_HOURS_FOR_LEARNING = 1.0

# Thermal tracker configuration
EMA_ALPHA = 0.3
MIN_SESSION_MINUTES = 5.0
MIN_DELTA_TEMP = 0.2

# Blend configuration
BLEND_FULL_CONFIDENCE_SAMPLES = 10
BLEND_LOWER_CLAMP_FACTOR = 0.1
BLEND_UPPER_CLAMP_FACTOR = 1.2


# ---------------------------------------------------------------------------
# Learned average power draw
# ---------------------------------------------------------------------------


def _get_power_stats_and_switch_history(
    hass: HomeAssistant,
    power_sensor: str,
    switch_entity: str,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """
    Fetch power sensor statistics and switch state history.

    This is a blocking function that must be called in the executor.
    """
    # Hourly mean power stats over the learning window
    stats_raw = statistics_during_period(
        hass,
        start_time=start,
        end_time=end,
        statistic_ids={power_sensor},
        period="hour",
        units=None,
        types={"mean"},
    )
    power_stats = stats_raw.get(power_sensor, [])

    # Switch on/off state changes over the same window
    switch_changes_raw = history.state_changes_during_period(
        hass,
        start_time=start,
        end_time=end,
        entity_id=switch_entity,
        no_attributes=True,
        include_start_time_state=True,
    )
    switch_changes = switch_changes_raw.get(switch_entity, [])

    return power_stats, switch_changes


def _compute_on_intervals(
    switch_changes: list[Any],
    start: datetime,  # noqa: ARG001
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """
    Compute (on_start, on_end) intervals from switch state changes.

    Returns a list of non-overlapping time intervals where the switch
    was in the "on" state.
    """
    intervals: list[tuple[datetime, datetime]] = []
    current_on_start: datetime | None = None

    for state in switch_changes:
        ts = state.last_changed
        if state.state == "on":
            if current_on_start is None:
                current_on_start = ts
        elif current_on_start is not None:
            intervals.append((current_on_start, ts))
            current_on_start = None

    # If still on at the end of the window
    if current_on_start is not None:
        intervals.append((current_on_start, end))

    return intervals


def _hour_overlap_fraction(
    hour_start: datetime,
    hour_end: datetime,
    intervals: list[tuple[datetime, datetime]],
) -> float:
    """Compute fraction of [hour_start, hour_end] overlapping on-intervals."""
    hour_duration = (hour_end - hour_start).total_seconds()
    if hour_duration <= 0:
        return 0.0

    overlap_seconds = 0.0
    for on_start, on_end in intervals:
        # Clamp interval to hour bounds
        effective_start = max(on_start, hour_start)
        effective_end = min(on_end, hour_end)
        if effective_start < effective_end:
            overlap_seconds += (effective_end - effective_start).total_seconds()

    return overlap_seconds / hour_duration


def _compute_weighted_avg_power(
    power_stats: list[dict[str, Any]],
    on_intervals: list[tuple[datetime, datetime]],
) -> tuple[float | None, float]:
    """
    Compute time-weighted average power during on-intervals.

    Returns:
        (weighted_avg_power_w, total_on_hours)

    """
    if not power_stats or not on_intervals:
        return None, 0.0

    total_weighted_power = 0.0
    total_on_fraction = 0.0

    for row in power_stats:
        mean_power = row.get("mean")
        if mean_power is None:
            continue

        # Each row covers one hour
        hour_start = datetime.fromtimestamp(row["start"], tz=dt_util.UTC)
        hour_end = hour_start + timedelta(hours=1)

        on_fraction = _hour_overlap_fraction(hour_start, hour_end, on_intervals)
        if on_fraction > 0:
            total_weighted_power += mean_power * on_fraction
            total_on_fraction += on_fraction

    total_on_hours = total_on_fraction  # each fraction is of a 1-hour bucket

    if total_on_hours < MIN_ON_HOURS_FOR_LEARNING:
        return None, total_on_hours

    return total_weighted_power / total_on_fraction, total_on_hours


async def async_get_learned_avg_power_w(
    hass: HomeAssistant,
    power_sensor: str,
    switch_entity: str,
) -> tuple[float | None, float]:
    """
    Query the recorder for the learned average power consumption.

    Computes the time-weighted average power draw of the power sensor
    during hours when the switch was on, over the last 7 days.

    Returns:
        (learned_avg_power_w, total_on_hours)
        ``learned_avg_power_w`` is None if insufficient data (< 1 hour on-time).

    """
    end = dt_util.utcnow()
    start = end - timedelta(days=LEARNING_WINDOW_DAYS)

    try:
        power_stats, switch_changes = await get_instance(hass).async_add_executor_job(
            _get_power_stats_and_switch_history,
            hass,
            power_sensor,
            switch_entity,
            start,
            end,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "Failed to query recorder for learned power data for %s",
            power_sensor,
            exc_info=True,
        )
        return None, 0.0

    on_intervals = _compute_on_intervals(switch_changes, start, end)
    return _compute_weighted_avg_power(power_stats, on_intervals)


# ---------------------------------------------------------------------------
# Thermal tracker (energy per degree)
# ---------------------------------------------------------------------------


@dataclass
class ThermalTracker:
    """
    Tracks heating sessions to learn the energy cost per degree Celsius.

    Maintains an exponential moving average (EMA) of Wh/°C across
    completed heating sessions.  A session starts when the heater turns
    on and ends when it turns off.  Sessions shorter than 5 minutes or
    producing less than 0.2°C of temperature change are discarded.
    """

    wh_per_degree: float | None = None
    sample_count: int = 0

    # Current session tracking (not persisted)
    _session_start_temp: float | None = field(default=None, repr=False)
    _session_start_time: datetime | None = field(default=None, repr=False)

    def on_heater_started(self, current_temp: float, now: datetime) -> None:
        """Record the start of a heating session."""
        self._session_start_temp = current_temp
        self._session_start_time = now

    def on_heater_stopped(
        self,
        current_temp: float,
        avg_power_w: float,
        now: datetime,
    ) -> None:
        """
        Record the end of a heating session and update the EMA.

        Args:
            current_temp: Temperature at heater turn-off.
            avg_power_w: Average power draw during this session (from power sensor).
            now: Current time.

        """
        if self._session_start_temp is None or self._session_start_time is None:
            return

        duration_minutes = (now - self._session_start_time).total_seconds() / 60.0
        delta_temp = current_temp - self._session_start_temp

        # Reset session state
        self._session_start_temp = None
        self._session_start_time = None

        # Filter out noise and short sessions
        if duration_minutes < MIN_SESSION_MINUTES:
            _LOGGER.debug(
                "Discarding heating session: too short (%.1f min)", duration_minutes
            )
            return

        if delta_temp < MIN_DELTA_TEMP:
            _LOGGER.debug(
                "Discarding heating session: insufficient temp change (%.2f°C)",
                delta_temp,
            )
            return

        if avg_power_w <= 0:
            _LOGGER.debug("Discarding heating session: zero or negative power")
            return

        # Compute energy per degree for this session
        duration_hours = duration_minutes / 60.0
        energy_wh = avg_power_w * duration_hours
        sample_wh_per_degree = energy_wh / delta_temp

        # Update EMA
        if self.wh_per_degree is None:
            self.wh_per_degree = sample_wh_per_degree
        else:
            self.wh_per_degree = (
                EMA_ALPHA * sample_wh_per_degree + (1 - EMA_ALPHA) * self.wh_per_degree
            )

        self.sample_count += 1

        _LOGGER.debug(
            "Thermal session: %.1fWh for %.2f°C = %.0f Wh/°C "
            "(EMA: %.0f Wh/°C, samples: %d)",
            energy_wh,
            delta_temp,
            sample_wh_per_degree,
            self.wh_per_degree,
            self.sample_count,
        )

    @property
    def has_session(self) -> bool:
        """Return True if a heating session is currently in progress."""
        return self._session_start_time is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a dict for persistence."""
        return {
            "wh_per_degree": self.wh_per_degree,
            "sample_count": self.sample_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ThermalTracker:
        """Deserialize from a dict."""
        return cls(
            wh_per_degree=data.get("wh_per_degree"),
            sample_count=data.get("sample_count", 0),
        )


# ---------------------------------------------------------------------------
# Blending learned values with peak
# ---------------------------------------------------------------------------


def blend_with_peak(
    learned: float | None,
    peak: float,
    sample_count: int,
) -> float:
    """
    Blend a learned power value with the configured peak.

    Confidence increases linearly with sample count, reaching full
    confidence after ``BLEND_FULL_CONFIDENCE_SAMPLES`` samples.

    The result is clamped to ``[peak * 0.1, peak * 1.2]`` as a safety
    measure against extreme outliers.

    Returns ``peak`` when ``learned`` is None.
    """
    if learned is None or peak <= 0:
        return peak

    weight = min(1.0, sample_count / BLEND_FULL_CONFIDENCE_SAMPLES)
    blended = weight * learned + (1 - weight) * peak

    lower = peak * BLEND_LOWER_CLAMP_FACTOR
    upper = peak * BLEND_UPPER_CLAMP_FACTOR
    return max(lower, min(upper, blended))
