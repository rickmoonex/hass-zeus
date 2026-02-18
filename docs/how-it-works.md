# How It Works

## Price Fetching

Zeus fetches prices from the Tibber GraphQL API every **15 minutes** and caches the 15-minute price slots. This ensures new prices (especially tomorrow's day-ahead data published around 13:00 CET) are picked up promptly.

If the Tibber API fails, Zeus retries with **exponential backoff** (30s, 60s, 120s) before giving up. Authentication errors fail immediately without retry. After all retries are exhausted, the next regular 15-minute poll tries again.

At every 15-minute boundary (`:00`, `:15`, `:30`, `:45`), the scheduler reruns to re-evaluate device schedules with the current slot.

## Recommended Inverter Output

When the energy price is **positive** (you earn money for exporting), the sensor recommends **100%** output.

When the price is **negative** (you pay to export), it calculates the minimum output needed to match home consumption:

```
recommended_pct = (home_consumption / max_power_output) * 100%
```

This avoids exporting to the grid during negative pricing while still powering your home.

## Device Scheduling

The scheduler uses a **global cost optimization** algorithm that considers all devices together:

### Phase 1: Deadline pressure (hard constraints)

For each device, if the remaining runtime needed equals or exceeds the number of available slots before its deadline, the device is **forced on** in all remaining slots. There's no room to wait for cheaper times.

### Phase 2: Cost-optimal assignment

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

## Thermostat Control

Thermostat devices use a different algorithm from switch devices. Instead of scheduling fixed runtime, they maintain a target temperature within a configurable margin while optimizing *when* to heat.

### Three-tier decision model

1. **FORCE ON** -- Temperature at or below the lower bound (target - tolerance). Zeus turns on the heater regardless of price. Comfort is guaranteed.
2. **FORCE OFF** -- Temperature at or above the upper bound (target + tolerance). Zeus turns off the heater regardless of price.
3. **OPTIMIZE** -- Temperature within the tolerance range. Zeus decides based on price, solar, and urgency.

### Urgency-weighted price threshold

Within the tolerance range, Zeus computes an *urgency score* (0.0 at upper bound, 1.0 at lower bound). It then compares the current price's rank against upcoming prices:

- **Urgency 0.3** (near upper bound): only heat if the current price is in the bottom 30% of upcoming prices
- **Urgency 0.7** (near lower bound): heat if the current price is in the bottom 70%
- **Urgency 1.0**: always heat (equivalent to FORCE ON)

This means Zeus pre-heats during cheap slots (pushing temperature toward the upper bound) and coasts through expensive slots (letting temperature drift toward the lower bound).

### Solar-aware decisions

- **Solar surplus available**: Always heat -- free energy is always used
- **Solar forecast look-ahead**: If urgency is low and solar surplus is expected in the next 1-3 slots, Zeus coasts and waits for free energy
- **High urgency overrides solar wait**: If temperature is approaching the lower bound, Zeus heats immediately even if solar is coming soon

### Multi-device solar sharing

Thermostat devices share solar surplus with switch devices. Devices are processed by priority (1 = highest). Higher-priority zones consume solar first; lower-priority zones see reduced surplus and may need to use grid power.

## Real-time Solar Surplus

The scheduler doesn't just use the forecast. When the solar inverter's production entity reports a state change, the scheduler reruns immediately.

For the **current slot**, if the live solar surplus (production minus consumption) exceeds the forecast surplus, the live value is used instead. This means:

- **Sun producing more than predicted?** Devices activate opportunistically to use the surplus.
- **Cloud temporarily reduces production?** The forecast value is kept -- live values never downgrade the forecast.
- **The full device peak must fit.** If a device needs 1000W and only 800W surplus is available, it's not treated as free solar. The cost is calculated proportionally.

## Managed Device Power Deduction

When Zeus turns on a device (e.g., a boiler at 1700W), the home energy monitor reports the increased total household load. Without correction, the scheduler would see reduced solar surplus, recalculate the slot as more expensive, and turn the device off -- creating an on/off feedback loop.

Zeus prevents this by subtracting the live power draw of all managed devices (switches and thermostats that are currently ON) from the raw home consumption reading before computing solar surplus. This ensures the scheduler sees only the unmanaged background load when evaluating slot costs.

## Forecast Bias Correction

When live solar surplus exceeds the forecast for the current slot, Zeus computes a **bias correction factor** and applies it to all future slots. This compensates for systematic forecast under-prediction.

**Example:** The forecast predicts 1000W surplus for the current slot, but live production is 1500W. Zeus computes a bias of 1.5x and scales all future slot surpluses accordingly. A future slot forecasted at 800W becomes 1200W for scheduling purposes.

This only applies when live exceeds forecast (bias > 1.0). It never reduces future slot values.

## Solar Opportunity Cost

The scheduler automatically accounts for the revenue you lose by using solar to power a device instead of exporting it to the grid. Each slot's spot price (energy-only price from Tibber) is used as the opportunity cost of consuming solar:

- If a future grid slot is cheaper than the current spot price, the scheduler may prefer to **export solar now** and **run the device on cheap grid later**
- If grid prices are high, it still prefers solar (using it avoids a high grid cost)
- When the spot price is zero or negative, solar is always preferred (cost = -1.0)

Because the spot price varies every 15 minutes, the opportunity cost is different for each slot -- producing more economically optimal schedules than a static feed-in rate.

## Actual Device Power Usage

When a device is ON and reporting a live reading via its power sensor, the scheduler uses the **actual draw** instead of the configured peak power for solar consumption calculations in the current slot:

- A device in a low-draw phase frees up solar surplus for other devices
- Future slots still use peak power as a safe upper bound for planning
- If the device is OFF or no reading is available, peak power is used

**Example:** Device A (peak 1500W) is ON but currently drawing only 200W. With 2000W solar surplus, the scheduler sees 1800W remaining (not 500W), letting Device B (1000W peak) also run on solar.

## Minimum Cycle Time

When configured, the minimum cycle time prevents the switch from toggling more frequently than the specified interval. This protects devices with compressors, motors, or heating elements from damage caused by rapid on/off cycling.

- Zeus tracks when it last changed the switch state
- Before applying a new schedule decision, it checks: has enough time passed since the last change?
- If the elapsed time is less than `min_cycle_time`, the current state is held regardless of what the scheduler wants

**Example:** A heat pump with `min_cycle_time: 15` is turned ON at 10:03. At 10:10, a cloud causes the scheduler to want it OFF. Zeus holds it ON because only 7 minutes have passed. At 10:18 (15 minutes elapsed), the next scheduler run can turn it OFF.

The `cycle_locked` attribute on the binary sensor shows whether the device is currently being held.
