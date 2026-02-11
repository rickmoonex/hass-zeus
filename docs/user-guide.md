# Zeus Energy Manager - User Guide

Zeus is a Home Assistant custom integration for managing dynamic energy bills. It fetches real-time energy prices from Tibber, controls solar inverter output during negative pricing, and schedules devices to run at optimal times based on price, solar forecast, and real-time solar production.

## Subentry Types

Zeus uses Home Assistant's subentry system. After setting up the main integration (energy provider + API token), you add subentries to configure what Zeus manages.

### Solar Inverter

Configures your solar inverter for production monitoring, forecast integration, and output throttling during negative prices.

| Field | Description |
|---|---|
| Production entity | Sensor reporting current solar production (W) |
| Output control entity | Number entity controlling inverter output (0-100%) |
| Max power output | Maximum inverter output (W) |
| Forecast entity | Optional: forecast.solar power sensor |

**Limit:** 1 per integration.

### Home Energy Monitor

Tracks your household's live energy consumption. Used by the scheduler to calculate solar surplus (production minus consumption).

| Field | Description |
|---|---|
| Energy usage entity | Sensor reporting live home consumption (W) |

**Limit:** 1 per integration.

### Switch Device

A device that needs to run for a fixed number of minutes per day. Zeus schedules it into the cheapest available time slots, respecting solar surplus and deadlines.

| Field | Description |
|---|---|
| Switch entity | The switch or input_boolean controlling the device |
| Power sensor | Sensor reporting the device's current power draw (W) |
| Peak usage | Maximum power consumption (W) |
| Daily runtime | Required minutes of runtime per day |
| Deadline | Time by which runtime must be completed |
| Priority | 1 (highest) to 10 (lowest) for slot assignment |
| Min cycle time | Minimum on/off duration to prevent rapid toggling |

**Use case:** Washing machine, dishwasher, pool pump, electric boiler.

### Thermostat Device

A heating (or cooling) zone managed by Zeus as a software thermostat. Instead of fixed runtime, Zeus maintains a target temperature within a configurable margin. Within that margin, Zeus optimizes *when* to heat based on energy prices and solar availability.

| Field | Description |
|---|---|
| Switch entity | Smart plug or relay controlling the heater |
| Power sensor | Sensor reporting current power draw (W) |
| Temperature sensor | Sensor reporting zone temperature (C) |
| Peak usage | Maximum power consumption (W) |
| Target temperature | Desired temperature (C) |
| Temperature margin | Allowed deviation from target (C) |
| Priority | 1-10 for solar surplus sharing |
| Min cycle time | Minimum on/off duration (min) |

**Use case:** Bedroom radiators with smart plugs, heat pump with on/off control, any heating zone with a temperature sensor.

#### How thermostat control works

Zeus uses a 3-tier decision model:

1. **FORCE ON** -- Temperature at or below the lower bound (target - margin). Zeus turns on the heater regardless of price. Comfort is guaranteed.

2. **FORCE OFF** -- Temperature at or above the upper bound (target + margin). Zeus turns off the heater regardless of price. Prevents overheating.

3. **OPTIMIZE** -- Temperature within the margin range. Zeus decides based on:
   - **Temperature urgency**: How close to the lower bound (closer = more willing to accept expensive energy)
   - **Price attractiveness**: Is the current price cheap compared to upcoming slots?
   - **Solar surplus**: Free energy available from solar production?
   - **Solar forecast**: Will free solar be available soon? If so, coast and wait.

The optimization uses an urgency-weighted price threshold: when temperature is near the lower margin, Zeus accepts prices in the bottom 70-80% of upcoming slots. When near the upper margin, it only heats during very cheap slots (bottom 20-30%) or when solar surplus is available.

#### Pre-heating with cheap energy

When prices are low or solar is abundant, Zeus heats toward the upper margin to "bank" thermal energy. The room then coasts through expensive periods, drifting back toward the lower margin. This shifts electricity consumption from peak prices to off-peak prices.

#### Solar-aware scheduling

Zeus considers the solar forecast when making thermostat decisions. If solar surplus is expected within the next few slots and the temperature isn't urgent, Zeus will coast and wait for free solar energy rather than paying for grid electricity now.

#### Multi-device solar sharing

When multiple thermostat devices (and switch devices) want to heat during a solar surplus slot, Zeus allocates solar by priority. Higher-priority zones get free solar first; lower-priority zones see reduced surplus and may need to wait or use grid power.

## Scheduling Algorithm

### For Switch Devices

1. **Phase 1 (Deadline pressure):** If a device doesn't have enough remaining slots before its deadline, all eligible slots are force-assigned.

2. **Phase 2 (Cost-optimal):** Iteratively pick the globally cheapest (device, slot) pair. Solar surplus reduces cost. After each pick, solar is deducted so the next device sees accurate remaining surplus.

### For Thermostat Devices

Thermostat devices don't use the slot-based scheduler. Instead, they run a real-time decision engine on each 15-minute boundary and on temperature changes:

1. Read current temperature from the sensor
2. Compare against target +/- margin
3. If within margin, score the current slot against upcoming slots
4. Consider solar surplus and forecast
5. Decide: heat now or coast

### Solar Opportunity Cost

For both device types, the scheduler uses the energy-only spot price as the opportunity cost of consuming solar. If you're generating solar and the spot price is high, exporting earns good revenue. Using that solar for heating means losing that revenue. Zeus factors this in.

## Sensors Created

### Global (Zeus Energy Manager device)

- Current energy price (total incl. tax)
- Current energy-only price (spot)
- Next slot price
- Solar surplus (W)
- Solar self-consumption ratio (%)
- Home consumption (W)
- Grid import (W)
- Solar fraction (%)
- Today average/min/max price
- Cheapest upcoming price
- Negative energy price (binary sensor)
- Master switch (on/off for all Zeus management)

### Per Switch Device

- Runtime today (min)
- Device schedule (binary sensor -- on/off + reason)

### Per Thermostat Device

- Thermostat schedule (binary sensor -- heating on/off)
- Runtime today (min)

The thermostat binary sensor exposes attributes:
- `current_temperature`: Live reading from the temperature sensor
- `target_temperature`: Configured target
- `temperature_margin`: Configured margin
- `lower_bound` / `upper_bound`: Computed comfort range
- `heating_reason`: Why Zeus made its current decision
- `managed_entity`, `power_sensor`, `current_usage_w`, `priority`, etc.
