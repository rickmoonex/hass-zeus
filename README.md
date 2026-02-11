# Zeus Energy Manager

A Home Assistant custom integration for managing dynamic energy bills. Zeus fetches real-time energy prices, schedules devices to run at the cheapest times, prioritizes your own solar production over grid power, and controls your solar inverter output during negative pricing.

## Features

- **Dynamic energy pricing** -- fetches 15-minute price slots from Tibber
- **Solar inverter output control** -- reduces inverter output during negative prices to avoid paying for grid export
- **Device scheduling** -- automatically turns devices on/off at optimal times based on price, solar forecast, and real-time solar production
- **Real-time solar surplus** -- opportunistically activates devices when live solar exceeds the forecast
- **Global cost optimization** -- minimizes total energy cost across all devices, with shared solar surplus and concurrent slot usage
- **Minimum cycle time protection** -- prevents rapid toggling that could damage devices like compressors or washing machines
- **Solar forecast integration** -- uses forecast.solar hourly data for scheduling and sensor attributes

## Requirements

- Home Assistant 2024.1.0+
- An energy provider integration set up in HA (currently Tibber only)
- Optional: [forecast.solar](https://www.home-assistant.io/integrations/forecast_solar/) for solar-aware scheduling

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
| **Feed-in rate** | (Optional) EUR/kWh earned for exporting solar to the grid. Used for opportunity cost calculations in the scheduler. |

The inverter subentry enables the **Recommended inverter output** sensor and provides live solar data to the scheduler.

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

## How it works

### Price fetching

Zeus calls `tibber.get_prices` every hour and caches the 15-minute price slots. At every 15-minute boundary (`:00`, `:15`, `:30`, `:45`), it re-evaluates the current slot and reruns the scheduler.

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
   - **Full solar surplus** covers the device's peak usage: cost = **-feed_in_rate** (or -1.0 if no feed-in rate configured)
   - **Partial solar surplus**: cost = price x (1 - solar_fraction) - feed_in_rate x solar_fraction
   - **No solar**: cost = grid price
2. Pick the cheapest combination across all devices
3. Deduct the device's power draw from the slot's remaining solar surplus
4. Repeat until all devices have enough slots

**Key behaviors:**

- **Multiple devices can share a slot.** If two 500W devices both want a slot with 2000W solar surplus, they can both run there.
- **Solar surplus is a shared resource.** After device A claims 1000W of a 2000W surplus, device B sees only 1000W remaining.
- **Priority breaks ties.** When two devices have the same cost for the same slot, the higher-priority device wins.
- **Deadlines are absolute.** A device under deadline pressure is forced on regardless of cost.

### Real-time solar surplus

The scheduler doesn't just use the forecast. When the solar inverter's production entity reports a state change, the scheduler reruns immediately.

For the **current slot**, if the live solar surplus (production minus consumption) exceeds the forecast surplus, the live value is used instead. This means:

- **Sun producing more than predicted?** Devices activate opportunistically to use the surplus.
- **Cloud temporarily reduces production?** The forecast value is kept -- live values never downgrade the forecast.
- **The full device peak must fit.** If a device needs 1000W and only 800W surplus is available, it's not treated as free solar. The cost is calculated proportionally.

This ensures that excess solar production is used to power devices rather than being exported.

### Forecast bias correction

When live solar surplus exceeds the forecast for the current slot, Zeus computes a **bias correction factor** and applies it to all future slots. This compensates for systematic forecast under-prediction.

**Example:** The forecast predicts 1000W surplus for the current slot, but live production is 1500W. Zeus computes a bias of 1.5x and scales all future slot surpluses accordingly. A future slot forecasted at 800W becomes 1200W for scheduling purposes.

This only applies when live exceeds forecast (bias > 1.0). It never reduces future slot values.

### Feed-in opportunity cost

When a **feed-in rate** is configured on the solar inverter subentry, the scheduler accounts for the revenue you lose by using solar to power a device instead of exporting it to the grid.

Without a feed-in rate, running a device on solar is considered free (cost = -1.0, always preferred). With a feed-in rate, using solar has an **opportunity cost** equal to the export revenue you forgo. This means:

- If a future grid slot is cheaper than the feed-in rate, the scheduler may prefer to **export solar now** and **run the device on cheap grid later**
- If grid prices are high, it still prefers solar (using it avoids a high grid cost)
- Without solar surplus in a slot, the feed-in rate has no effect -- the device simply pays the grid price

This produces more economically optimal schedules for users with export tariffs.

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

### Sensors

| Entity | Description |
|---|---|
| **Current energy price** | Current 15-minute slot price in EUR/kWh |
| **Next slot price** | Price for the upcoming 15-minute slot |
| **Recommended inverter output** | Optimal inverter output percentage (per solar inverter) |

The recommended output sensor includes these extra attributes when a forecast entity is configured:

- `forecast_production_w` -- current forecast production
- `forecast_energy_today_remaining_wh`
- `forecast_energy_today_wh`
- `forecast_energy_current_hour_wh`
- `forecast_energy_next_hour_wh`

### Binary sensors

| Entity | Description |
|---|---|
| **Negative energy price** | ON when the current price is negative |
| **{Device} schedule** | ON when Zeus wants the device running (per switch device) |

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
