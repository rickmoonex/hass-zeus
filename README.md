# Zeus Energy Manager

A Home Assistant custom integration for managing dynamic energy bills. Zeus fetches real-time energy prices, schedules devices to run at the cheapest times, prioritizes your own solar production over grid power, and controls your solar inverter output during negative pricing.

## Features

- **Dynamic energy pricing** -- fetches 15-minute price slots from Tibber every 15 minutes with automatic retry on failure
- **Solar inverter output control** -- reduces inverter output during negative prices to avoid paying for grid export
- **Device scheduling** -- automatically turns devices on/off at optimal times based on price, solar forecast, and real-time solar production
- **Thermostat control** -- software thermostat for heating zones with temperature-aware, price/solar-optimized scheduling
- **Manual device recommendations** -- recommends cheapest run windows for non-smart devices (dishwashers, ovens, etc.) with one-tap reservation
- **Real-time solar surplus** -- opportunistically activates devices when live solar exceeds the forecast
- **Global cost optimization** -- minimizes total energy cost across all devices, with shared solar surplus and concurrent slot usage
- **Minimum cycle time protection** -- prevents rapid toggling that could damage devices like compressors or washing machines
- **Solar forecast integration** -- built-in Forecast.Solar API client with 1-hour caching for solar-aware scheduling
- **Energy dashboard sensors** -- hourly price arrays, today/tomorrow forecasts, and price analytics for custom dashboards
- **Feedback loop prevention** -- subtracts managed device power from home consumption to prevent on/off cycling

## Requirements

- Home Assistant 2024.1.0+
- An energy provider integration set up in HA (currently Tibber only)
- Optional: Solar panels with known declination, azimuth, and kWp for Forecast.Solar integration

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for "Zeus" and install it
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/zeus` folder to your `config/custom_components/` directory
2. Restart Home Assistant

## Setup

Zeus uses a single config entry with subentries for each component. Only one Zeus instance is allowed.

### 1. Add the integration

Go to **Settings > Devices & Services > Add Integration** and search for **Zeus**.

Select your energy price provider (currently only Tibber). The Tibber integration must already be set up in HA.

### 2. Add a solar inverter (optional, max 1)

From the Zeus integration page, click **Add solar inverter**.

| Field | Description |
|---|---|
| **Name** | Display name for this inverter |
| **Current production entity** | Sensor reporting current solar production in watts |
| **Output control entity** | Number entity (0-100%) controlling inverter output |
| **Maximum power output** | Max inverter power in watts |
| **Solar forecast entity** | (Optional) Power sensor from forecast.solar, e.g., `sensor.power_production_now` |
| **Panel declination** | Tilt angle of panels in degrees (0 = horizontal, 90 = vertical) |
| **Panel azimuth** | Compass direction panels face (-180 = north, 0 = south, 90 = west) |
| **Installed capacity (kWp)** | Total peak power of this panel array in kilowatt-peak |
| **Forecast.Solar API key** | (Optional) API key for higher rate limits. Free tier: 12 requests/hour |

The inverter subentry enables the **Recommended inverter output** sensor, provides live solar data to the scheduler, and fetches solar production forecasts from the Forecast.Solar API.

### 3. Add a home energy monitor (optional, max 1)

Click **Add home energy monitor**.

| Field | Description |
|---|---|
| **Name** | Display name |
| **Energy usage entity** | Sensor reporting live home energy usage in watts (positive = consumption, negative = production) |

The home monitor provides consumption data used to calculate solar surplus and recommended inverter output.

### 4. Add switch devices (unlimited)

Click **Add switch device** for each device you want Zeus to manage.

| Field | Description |
|---|---|
| **Name** | Display name (also used for the device name in HA) |
| **Switch entity** | The `switch.*` or `input_boolean.*` entity that controls the device |
| **Power sensor** | Sensor reporting the device's current power consumption in watts |
| **Peak power usage** | Maximum power the device draws in watts |
| **Required daily runtime** | How many minutes the device should run each day |
| **Deadline** | Time by which the daily runtime must be completed |
| **Priority** | 1 (highest) to 10 (lowest) -- higher priority devices get cheaper slots first |
| **Minimum cycle time** | (Optional) Minimum minutes the device must stay on or off before switching. Protects against rapid toggling. Set to 0 to disable. |

### 5. Add thermostat devices (unlimited)

Click **Add thermostat device** for each heating zone you want Zeus to manage as a software thermostat.

| Field | Description |
|---|---|
| **Name** | Display name (e.g., "Bedroom 1 Radiator") |
| **Switch entity** | The `switch.*` or `input_boolean.*` entity controlling the heater (e.g., a smart plug) |
| **Power sensor** | Sensor reporting the heater's current power consumption in watts |
| **Temperature sensor** | Sensor reporting the zone temperature in degrees Celsius |
| **Peak power usage** | Maximum power the heater draws in watts |
| **Target temperature** | Desired temperature for the zone (5-30 C) |
| **Temperature margin** | Allowed deviation from target (0.5-5.0 C). Zeus keeps temp within target +/- margin |
| **Priority** | 1 (highest) to 10 (lowest) -- higher priority zones get solar surplus first |
| **Minimum cycle time** | (Optional, default 5) Minimum minutes the heater must stay on or off before switching |

### 6. Add manual devices (unlimited)

Click **Add manual device** for non-smart devices that you start manually (dishwashers, ovens, etc.). Zeus recommends the cheapest time window and lets you reserve it so smart devices plan around it.

| Field | Description |
|---|---|
| **Name** | Display name (e.g., "Dishwasher") |
| **Peak power usage** | Peak power consumption in watts. Used to determine if solar surplus can fully cover the device. |
| **Average power usage** | (Optional) Average consumption over a full cycle in watts. Used for more realistic cost calculation. |
| **Cycle duration** | Default cycle duration in minutes (e.g., 90 for a dishwasher) |
| **Dynamic cycle duration** | Allow changing the cycle duration before each run via a number entity |
| **Power sensor** | (Optional) Sensor reporting current power consumption in watts |
| **Delay intervals** | (Optional) Comma-separated delay hours the device supports (e.g., `3,6,9`). When set, Zeus recommends the cheapest delay interval instead of an exact start time. |
| **Priority** | 1 (highest) to 10 (lowest) -- higher priority devices get solar surplus first when reserving |

Manual device recommendations are limited to slots until the next 06:00 local time, keeping suggestions within an actionable overnight horizon.

## How it works

### Price fetching

Zeus fetches prices from the Tibber GraphQL API every **15 minutes** and caches the 15-minute price slots. This ensures new prices (especially tomorrow's day-ahead data published around 13:00 CET) are picked up promptly.

If the Tibber API fails, Zeus retries with **exponential backoff** (30s, 60s, 120s) before giving up. Authentication errors fail immediately without retry. After all retries are exhausted, the next regular 15-minute poll tries again.

At every 15-minute boundary (`:00`, `:15`, `:30`, `:45`), the scheduler reruns to re-evaluate device schedules with the current slot.

### Recommended inverter output

When the energy price is **positive** (you earn money for exporting), the sensor recommends **100%** output.

When the price is **negative** (you pay to export), it calculates the minimum output needed to match home consumption:

```
recommended_pct = (home_consumption / max_power_output) * 100%
```

This avoids exporting to the grid during negative pricing while still powering your home.

### Device scheduling

The scheduler uses a **global cost optimization** algorithm that considers all devices together:

#### Phase 1: Deadline pressure (hard constraints)

For each device, if the remaining runtime needed equals or exceeds the number of available slots before its deadline, the device is **forced on** in all remaining slots. There's no room to wait for cheaper times.

#### Phase 2: Cost-optimal assignment

The scheduler iteratively picks the globally cheapest `(device, slot)` pair:

1. For each slot, compute a cost score:
   - **Full solar surplus** covers the device's peak usage: cost = **-energy_price** (the spot price you'd earn by exporting; -1.0 if spot price is zero or negative)
   - **Partial solar surplus**: cost = price x (1 - solar_fraction) - energy_price x solar_fraction
   - **No solar**: cost = grid price
2. Pick the cheapest combination across all devices
3. Deduct the device's power draw from the slot's remaining solar surplus
4. Repeat until all devices have enough slots

**Key behaviors:**

- **Multiple devices can share a slot.** If two 500W devices both want a slot with 2000W solar surplus, they can both run there.
- **Solar surplus is a shared resource.** After device A claims 1000W of a 2000W surplus, device B sees only 1000W remaining.
- **Priority breaks ties.** When two devices have the same cost for the same slot, the higher-priority device wins.
- **Deadlines are absolute.** A device under deadline pressure is forced on regardless of cost.

### Thermostat control

Thermostat devices use a different algorithm from switch devices. Instead of scheduling fixed runtime, they maintain a target temperature within a configurable margin while optimizing *when* to heat.

#### Three-tier decision model

1. **FORCE ON** -- Temperature at or below the lower bound (target - margin). Zeus turns on the heater regardless of price. Comfort is guaranteed.
2. **FORCE OFF** -- Temperature at or above the upper bound (target + margin). Zeus turns off the heater regardless of price.
3. **OPTIMIZE** -- Temperature within the margin. Zeus decides based on price, solar, and urgency.

#### Urgency-weighted price threshold

Within the margin range, Zeus computes an *urgency score* (0.0 at upper bound, 1.0 at lower bound). It then compares the current price's rank against upcoming prices:

- **Urgency 0.3** (near upper bound): only heat if the current price is in the bottom 30% of upcoming prices
- **Urgency 0.7** (near lower bound): heat if the current price is in the bottom 70%
- **Urgency 1.0**: always heat (equivalent to FORCE ON)

This means Zeus pre-heats during cheap slots (pushing temperature toward the upper margin) and coasts through expensive slots (letting temperature drift toward the lower margin).

#### Solar-aware decisions

- **Solar surplus available**: Always heat -- free energy is always used
- **Solar forecast look-ahead**: If urgency is low and solar surplus is expected in the next 1-3 slots, Zeus coasts and waits for free energy
- **High urgency overrides solar wait**: If temperature is approaching the lower bound, Zeus heats immediately even if solar is coming soon

#### Multi-device solar sharing

Thermostat devices share solar surplus with switch devices. Devices are processed by priority (1 = highest). Higher-priority zones consume solar first; lower-priority zones see reduced surplus and may need to use grid power.

### Real-time solar surplus

The scheduler doesn't just use the forecast. When the solar inverter's production entity reports a state change, the scheduler reruns immediately.

For the **current slot**, if the live solar surplus (production minus consumption) exceeds the forecast surplus, the live value is used instead. This means:

- **Sun producing more than predicted?** Devices activate opportunistically to use the surplus.
- **Cloud temporarily reduces production?** The forecast value is kept -- live values never downgrade the forecast.
- **The full device peak must fit.** If a device needs 1000W and only 800W surplus is available, it's not treated as free solar. The cost is calculated proportionally.

This ensures that excess solar production is used to power devices rather than being exported.

### Managed device power deduction

When Zeus turns on a device (e.g., a boiler at 1700W), the home energy monitor reports the increased total household load. Without correction, the scheduler would see reduced solar surplus, recalculate the slot as more expensive, and turn the device off -- creating an on/off feedback loop.

Zeus prevents this by subtracting the live power draw of all managed devices (switches and thermostats that are currently ON) from the raw home consumption reading before computing solar surplus. This ensures the scheduler sees only the unmanaged background load when evaluating slot costs.

### Forecast bias correction

When live solar surplus exceeds the forecast for the current slot, Zeus computes a **bias correction factor** and applies it to all future slots. This compensates for systematic forecast under-prediction.

**Example:** The forecast predicts 1000W surplus for the current slot, but live production is 1500W. Zeus computes a bias of 1.5x and scales all future slot surpluses accordingly. A future slot forecasted at 800W becomes 1200W for scheduling purposes.

This only applies when live exceeds forecast (bias > 1.0). It never reduces future slot values.

### Solar opportunity cost

The scheduler automatically accounts for the revenue you lose by using solar to power a device instead of exporting it to the grid. Without Dutch salderingsregeling (net metering), feed-in compensation equals the **spot price** (energy-only price from Tibber) -- which Zeus already knows for every 15-minute slot.

Each slot's spot price is used as the opportunity cost of consuming solar. This means:

- If a future grid slot is cheaper than the current spot price, the scheduler may prefer to **export solar now** and **run the device on cheap grid later**
- If grid prices are high, it still prefers solar (using it avoids a high grid cost)
- When the spot price is zero or negative, solar is always preferred (cost = -1.0)
- Without solar surplus in a slot, the spot price has no effect -- the device simply pays the grid price

Because the spot price varies every 15 minutes, the opportunity cost is different for each slot -- producing more economically optimal schedules than a static feed-in rate.

### Actual device power usage

The scheduler uses **live power sensor readings** for devices that are currently ON. Many devices have variable power draw -- a washing machine may use 2000W during heating but only 200W during rinsing.

When a device is ON and reporting a live reading via its power sensor, the scheduler uses the **actual draw** instead of the configured peak power for solar consumption calculations in the current slot. This means:

- A device in a low-draw phase frees up solar surplus for other devices
- Future slots still use peak power as a safe upper bound for planning
- If the device is OFF or no reading is available, peak power is used

**Example:** Device A (peak 1500W) is ON but currently drawing only 200W. With 2000W solar surplus, the scheduler sees 1800W remaining (not 500W), letting Device B (1000W peak) also run on solar.

### Minimum cycle time

When configured, the minimum cycle time prevents the switch from toggling more frequently than the specified interval. This protects devices with compressors, motors, or heating elements from damage caused by rapid on/off cycling.

**How it works:**

- Zeus tracks when it last changed the switch state
- Before applying a new schedule decision, it checks: has enough time passed since the last change?
- If the elapsed time is less than `min_cycle_time`, the current state is held regardless of what the scheduler wants
- This applies to all scheduler triggers: 15-minute boundaries, solar production changes, and manual `run_scheduler` calls

**Example:** A heat pump with `min_cycle_time: 15` is turned ON at 10:03. At 10:10, a cloud causes the scheduler to want it OFF. Zeus holds it ON because only 7 minutes have passed. At 10:18 (15 minutes elapsed), the next scheduler run can turn it OFF.

The `cycle_locked` attribute on the binary sensor shows whether the device is currently being held.

## Entities

### Price sensors

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

The **Current energy price** sensor includes these attributes:
- `slot_start` -- ISO timestamp of the current 15-min slot
- `energy_price` -- energy-only price (no tax)
- `min_price` -- today's lowest price
- `max_price` -- today's highest price
- `price_override` -- override value if set

The **Energy prices** sensor is designed for dashboard charts (e.g., apexcharts-card `data_generator`):
- `prices_today` -- array of `{start, price}` objects, one per hour (averaged from 15-min slots)
- `prices_tomorrow` -- same format for tomorrow (populated when Tibber publishes day-ahead prices, typically after ~13:00 CET)
- `min_price` -- today's lowest price
- `max_price` -- today's highest price
- `current_price` -- current slot's price

### Solar & energy sensors

| Entity | State | Unit | Description |
|---|---|---|---|
| **Solar surplus** | Production minus consumption | W | 0 when consumption exceeds production |
| **Solar self-consumption ratio** | min(consumption, production) / production | % | How much solar you use vs export |
| **Home consumption** | Live home energy usage | W | From the home energy monitor entity |
| **Grid import** | max(0, consumption - production) | W | What you're drawing from the grid |
| **Solar fraction** | min(100, production / consumption) | % | How much of consumption is solar-covered |
| **Solar forecast today** | Total forecasted production today | kWh | Attributes: `hourly_today`, `hourly_tomorrow`, `today_total_kwh`, `tomorrow_total_kwh` |

### Device sensors

| Entity | State | Description |
|---|---|---|
| **Recommended inverter output** | Optimal output % | Per solar inverter. 100% when prices positive; reduced to match home consumption when negative. |
| **{Device} runtime today** | Minutes run today | Per switch device. State class: `total_increasing` |
| **{Device} heating runtime today** | Minutes heated today | Per thermostat device. State class: `total_increasing` |
| **{Device} recommended start** | Recommended start time (HH:MM) | Per manual device (see below) |

The **Recommended inverter output** sensor includes these extra attributes when a forecast entity is configured:

- `forecast_production_w` -- current forecast production
- `forecast_energy_today_remaining_wh`
- `forecast_energy_today_wh`
- `forecast_energy_current_hour_wh`
- `forecast_energy_next_hour_wh`

The **Manual device recommendation** sensor includes:
- `recommended_start` / `recommended_end` -- ISO timestamps of the cheapest window
- `estimated_cost` -- estimated cost in EUR for the recommended window
- `cost_if_now` -- what it would cost to run right now
- `savings_pct` -- percentage saved vs running now
- `ranked_windows` -- top 10 windows sorted by cost, each with `start`, `end`, `cost`, `solar_pct`
- `reserved` -- whether a slot is currently reserved
- `reservation_start` / `reservation_end` -- reserved window timestamps

### Binary sensors

| Entity | Description |
|---|---|
| **Negative energy price** | ON when the current energy-only price is negative |
| **{Device} schedule** | ON when Zeus wants the device running (per switch device) |
| **{Device} heating** | ON when Zeus wants the heater running (per thermostat device) |
| **{Device} reserved** | ON when a manual device has a reserved time slot |

The device schedule binary sensor includes these attributes:

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

Possible schedule reasons:

- `Scheduled: solar surplus available` -- running on free solar
- `Scheduled: optimal price slot` -- cheapest grid slot
- `Forced on: deadline pressure` -- must run, no time to wait
- `Waiting for cheaper slot` -- a better slot is coming
- `Daily runtime already met` -- done for the day

The thermostat heating binary sensor includes these attributes:

- `managed_entity` -- the switch entity being controlled
- `power_sensor` -- the power monitoring sensor
- `current_usage_w` -- live power draw
- `peak_usage_w` -- configured peak power
- `temperature_sensor` -- the temperature sensor entity
- `current_temperature` -- live temperature reading
- `target_temperature` -- configured target
- `temperature_margin` -- configured margin
- `lower_bound` / `upper_bound` -- computed comfort range
- `priority` -- configured priority
- `min_cycle_time_min` -- configured minimum cycle time
- `cycle_locked` -- whether the heater is held by the cycle guard
- `heating_reason` -- human-readable explanation of the current decision

Possible heating reasons:

- `Forced on: temperature X.X C at or below minimum Y.Y C`
- `Forced off: temperature X.X C at or above maximum Y.Y C`
- `Heating: solar surplus available`
- `Heating: cheap price (rank N%, urgency M%)`
- `Coasting: waiting for cheaper slot (rank N%, urgency M%)`
- `Coasting: solar surplus expected soon`
- `No temperature reading -- holding current state`

### Other entities

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

## Debugging

Enable debug logging for Zeus:

```yaml
logger:
  default: warning
  logs:
    custom_components.zeus: debug
```

Key log messages to look for:

- `Found forecast_solar entry: ...` -- confirms forecast.solar is detected
- `Solar forecast retrieved: N hourly entries` -- forecast data is available
- `No forecast_solar config entries found` -- forecast.solar not set up
- `Scheduler: solar_forecast=present (N entries)` -- data passed to scheduler
- `Scheduler: home_consumption_w=...` -- consumption value used
- `Scheduler: live_solar_surplus_w=...` -- real-time surplus value
- `Live solar surplus NW exceeds forecast NW for slot ...` -- live override activated
- `Applying forecast bias correction: Nx to future slots` -- bias correction applied to future slots
- `Cycle lock: ... must stay on/off for N more min` -- min_cycle_time holding state
- `Forecast entity ... not found or unavailable` -- sensor entity missing

## Development

### Prerequisites

- Python 3.13
- Nix (for the dev environment)

### Running

```bash
nix run  # starts HA dev server with Zeus loaded
```

### Testing

```bash
pip install -r requirements_dev.txt
pytest tests/ -v
```

### Linting

```bash
ruff check .
```
