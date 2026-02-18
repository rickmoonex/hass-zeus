# Entities & Services

## Price Sensors

| Entity | State | Unit | Description |
|---|---|---|---|
| **Current energy price** | Current total price (energy + tax) | EUR/kWh | The price you pay for grid consumption |
| **Current energy-only price** | Energy-only price (no tax) | EUR/kWh | The price relevant for grid export / feed-in |
| **Next slot price** | Next 15-min slot total price | EUR/kWh | |
| **Today average price** | Average across all today's slots | EUR/kWh | Attribute: `slot_count` |
| **Today minimum price** | Lowest price today | EUR/kWh | Attribute: `slot_start` (when it occurs) |
| **Today maximum price** | Highest price today | EUR/kWh | Attribute: `slot_start` (when it occurs) |
| **Cheapest upcoming price** | Lowest price in remaining slots | EUR/kWh | Attribute: `slot_start` |
| **Energy prices** | Number of hourly entries today | -- | Dashboard sensor (see below) |

### Current energy price attributes

- `slot_start` -- ISO timestamp of the current 15-min slot
- `energy_price` -- energy-only price (no tax)
- `min_price` -- today's lowest price
- `max_price` -- today's highest price
- `price_override` -- override value if set

### Energy prices attributes

Designed for dashboard charts (e.g., apexcharts-card `data_generator`):

- `prices_today` -- array of `{start, price}` objects, one per hour (averaged from 15-min slots)
- `prices_tomorrow` -- same format for tomorrow (populated when Tibber publishes day-ahead prices, typically after ~13:00 CET)
- `min_price` -- today's lowest price
- `max_price` -- today's highest price
- `current_price` -- current slot's price

## Solar & Energy Sensors

| Entity | State | Unit | Description |
|---|---|---|---|
| **Solar surplus** | Production minus consumption | W | 0 when consumption exceeds production |
| **Solar self-consumption ratio** | min(consumption, production) / production | % | How much solar you use vs export |
| **Home consumption** | Live home energy usage | W | From the home energy monitor entity |
| **Grid import** | max(0, consumption - production) | W | What you're drawing from the grid |
| **Solar fraction** | min(100, production / consumption) | % | How much of consumption is solar-covered |
| **Solar forecast today** | Total forecasted production today | kWh | See attributes below |

### Solar forecast attributes

- `today_total_kwh` -- total forecasted production today
- `tomorrow_total_kwh` -- total forecasted production tomorrow
- `hourly_today` -- dict of `"HH:MM" -> kWh` for each hour today
- `hourly_tomorrow` -- same for tomorrow

## Device Sensors

| Entity | State | Description |
|---|---|---|
| **Recommended inverter output** | Optimal output % | Per solar inverter. 100% when prices positive; reduced to match home consumption when negative. |
| **{Device} runtime today** | Minutes run today | Per switch device. State class: `total_increasing` |
| **{Device} heating runtime today** | Minutes heated today | Per thermostat device. State class: `total_increasing` |
| **{Device} recommended start** | Recommended start time (HH:MM) | Per manual device |

### Recommended inverter output attributes

- `energy_price_is_negative` -- whether the current energy-only price is negative
- `current_production_w` -- live solar production in watts
- `home_consumption_w` -- live home consumption in watts
- `max_power_output_w` -- configured max inverter power
- `forecast_production_w` -- current forecast production (only when a forecast entity is configured)
- `price_override` -- override value if set

### Manual device recommendation attributes

- `subentry_id` -- the subentry identifier
- `cycle_duration_min` -- current cycle duration in minutes
- `peak_usage_w` -- configured peak power
- `dynamic_cycle_duration` -- whether dynamic cycle duration is enabled
- `number_entity_id` -- entity ID of the cycle duration number (if dynamic)
- `has_delay_intervals` -- whether delay intervals are configured
- `recommended_start` / `recommended_end` -- ISO timestamps of the cheapest window
- `estimated_cost` -- estimated cost in EUR for the recommended window
- `delay_hours` -- recommended delay in hours (only for devices with delay intervals)
- `cost_if_now` -- what it would cost to run right now
- `savings_pct` -- percentage saved vs running now
- `ranked_windows` -- top 10 windows sorted by cost, each with `start`, `end`, `cost`, `solar_pct` (and `delay_hours` for delay-interval devices)
- `reserved` -- whether a slot is currently reserved
- `reservation_start` / `reservation_end` -- reserved window timestamps

## Binary Sensors

| Entity | Description |
|---|---|
| **Negative energy price** | ON when the current energy-only price is negative |
| **{Device} schedule** | ON when Zeus wants the device running (per switch device) |
| **{Device} heating** | ON when Zeus wants the heater running (per thermostat device) |
| **{Device} reserved** | ON when a manual device has a reserved time slot |

### Device schedule attributes

- `managed_entity` -- the switch entity being controlled
- `power_sensor` -- the power monitoring sensor
- `current_usage_w` -- live power draw from the sensor
- `peak_usage_w` -- configured peak power
- `daily_runtime_min` -- configured daily runtime target
- `deadline` -- configured deadline
- `priority` -- configured priority
- `min_cycle_time_min` -- configured minimum cycle time
- `cycle_locked` -- whether the device is currently held by the cycle guard
- `remaining_runtime_min` -- minutes still needed today
- `schedule_reason` -- human-readable explanation of the current decision
- `scheduled_slots` -- list of scheduled slot start times

**Schedule reasons:**

- `Scheduled: solar surplus available` -- running on free solar
- `Scheduled: optimal price slot` -- cheapest grid slot
- `Forced on: deadline pressure` -- must run, no time to wait
- `Waiting for cheaper slot` -- a better slot is coming
- `Daily runtime already met` -- done for the day
- `Daily runtime met` -- runtime target reached (device is off)

### Thermostat heating attributes

- `managed_entity` -- the switch entity being controlled
- `power_sensor` -- the power monitoring sensor
- `current_usage_w` -- live power draw
- `peak_usage_w` -- configured peak power
- `temperature_sensor` -- the temperature sensor entity
- `current_temperature` -- live temperature reading
- `target_temperature` -- configured target
- `temperature_tolerance` -- configured tolerance
- `heating_lower_bound` / `heating_upper_bound` -- computed comfort range
- `priority` -- configured priority
- `min_cycle_time_min` -- configured minimum cycle time
- `cycle_locked` -- whether the heater is held by the cycle guard
- `heating_reason` -- human-readable explanation of the current decision

**Heating reasons:**

- `Forced on: temperature X.X C at or below minimum Y.Y C`
- `Forced off: temperature X.X C at or above maximum Y.Y C`
- `Heating: solar surplus available`
- `Heating: cheap price (rank N%, urgency M%)`
- `Coasting: waiting for cheaper slot (rank N%, urgency M%)`
- `Coasting: solar surplus expected soon`
- `Coasting: thermal headroom X.Xh, cheaper slot available`
- `Heating: low thermal headroom (X.Xh to lower bound)`
- `Heating: no price data, urgency-based fallback`
- `Coasting: no price data, urgency-based fallback`
- `Thermostat off` -- HVAC mode set to off
- `No temperature reading -- holding current state`

## Other Entities

| Entity | Type | Description |
|---|---|---|
| **Master switch** | Switch | Enable/disable all Zeus management globally |
| **{Device} reserve slot** | Button | Per manual device. Reserves the recommended time window. |
| **{Device} cycle duration** | Number | Per manual device (when dynamic cycle duration is enabled). Adjust cycle length before reserving. |
| **{Device} thermostat** | Climate | Per thermostat device. Set target temperature and mode (heat/off). |

## Services

### `zeus.set_price_override`

Override the current energy price for testing. All sensors immediately reflect the override.

```yaml
service: zeus.set_price_override
data:
  price: -0.05  # EUR/kWh
```

### `zeus.clear_price_override`

Remove the price override and revert to real prices.

```yaml
service: zeus.clear_price_override
```

### `zeus.run_scheduler`

Manually trigger the scheduler. Recomputes schedules and applies switch control immediately.

```yaml
service: zeus.run_scheduler
```

### `zeus.reserve_manual_device`

Reserve a time window for a manual device. Other smart devices will plan around this reservation.

```yaml
service: zeus.reserve_manual_device
data:
  subentry_id: "abc123"  # the manual device subentry ID
  start_time: "2026-02-14T10:00:00"  # optional, defaults to recommended start
```

### `zeus.cancel_reservation`

Cancel an active manual device reservation.

```yaml
service: zeus.cancel_reservation
data:
  subentry_id: "abc123"  # the manual device subentry ID
```
