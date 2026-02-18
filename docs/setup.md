# Setup Guide

Zeus uses a single config entry with subentries for each component. Only one Zeus instance is allowed.

## 1. Add the integration

Go to **Settings > Devices & Services > Add Integration** and search for **Zeus**.

Select your energy price provider (currently only Tibber). You will then be prompted to enter your **Tibber personal access token**. Zeus connects directly to the Tibber GraphQL API -- no separate Tibber HA integration is needed.

You can create a token at [developer.tibber.com](https://developer.tibber.com/settings/access-token).

## 2. Add a solar inverter (optional, max 1)

From the Zeus integration page, click **Add solar inverter**.

| Field | Description |
|---|---|
| **Name** | Display name for this inverter |
| **Current production entity** | Sensor reporting current solar production in watts |
| **Output control entity** | Number entity (0-100%) controlling inverter output |
| **Maximum power output** | Max inverter power in watts |
| **Solar forecast entity** | (Optional) Power sensor for extra attributes on the recommended output sensor. Does NOT drive the scheduler -- the built-in Forecast.Solar API client handles that. |
| **Panel declination** | Tilt angle of panels in degrees (0 = horizontal, 90 = vertical) |
| **Panel azimuth** | Compass direction panels face (-180 = north, 0 = south, 90 = west) |
| **Installed capacity (kWp)** | Total peak power of this panel array in kilowatt-peak |
| **Forecast.Solar API key** | (Optional) API key for higher rate limits. Free tier: 12 requests/hour |

The inverter subentry enables the **Recommended inverter output** sensor, provides live solar data to the scheduler, and fetches solar production forecasts from the Forecast.Solar API.

## 3. Add a home energy monitor (optional, max 1)

Click **Add home energy monitor**.

| Field | Description |
|---|---|
| **Name** | Display name |
| **Energy usage entity** | Sensor reporting live home energy usage in watts (positive = consumption, negative = production) |

The home monitor provides consumption data used to calculate solar surplus and recommended inverter output.

## 4. Add switch devices (unlimited)

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
| **Use actual power** | (Optional, default off) When enabled, uses live power sensor readings instead of peak power for solar surplus calculations. Enable for devices that may draw no power while on (e.g., a boiler with an internal thermostat). Disable for devices with fluctuating power (e.g., a washing machine). |

## 5. Add thermostat devices (unlimited)

Click **Add thermostat device** for each heating zone you want Zeus to manage as a software thermostat.

| Field | Description |
|---|---|
| **Name** | Display name (e.g., "Bedroom 1 Radiator") |
| **Switch entity** | The `switch.*` or `input_boolean.*` entity controlling the heater (e.g., a smart plug) |
| **Power sensor** | Sensor reporting the heater's current power consumption in watts |
| **Temperature sensor** | Sensor reporting the zone temperature in degrees Celsius |
| **Peak power usage** | Maximum power the heater draws in watts |
| **Temperature tolerance** | Allowed deviation from target temperature. Zeus heats between target minus and target plus this value. |
| **Priority** | 1 (highest) to 10 (lowest) -- higher priority zones get solar surplus first |
| **Minimum cycle time** | (Optional, default 5) Minimum minutes the heater must stay on or off before switching |

After adding a thermostat device, set the desired target temperature on the **climate entity** (`climate.{name}_thermostat`) in Home Assistant. Zeus will maintain the temperature within target +/- tolerance.

## 6. Add manual devices (unlimited)

Click **Add manual device** for non-smart devices that you start manually (dishwashers, ovens, etc.). Zeus recommends the cheapest time window and lets you reserve it so smart devices plan around it.

| Field | Description |
|---|---|
| **Name** | Display name (e.g., "Dishwasher") |
| **Peak power usage** | Peak power consumption in watts. Used to determine if solar surplus can fully cover the device. |
| **Average power usage** | Average consumption over a full cycle in watts. Used for cost calculation to give a realistic picture of actual energy use. |
| **Cycle duration** | Default cycle duration in minutes (e.g., 90 for a dishwasher) |
| **Dynamic cycle duration** | Allow changing the cycle duration before each run via a number entity |
| **Power sensor** | (Optional) Sensor reporting current power consumption in watts |
| **Delay intervals** | (Optional) Comma-separated delay hours the device supports (e.g., `3,6,9`). When set, Zeus recommends the cheapest delay interval instead of an exact start time. |
| **Priority** | 1 (highest) to 10 (lowest) -- higher priority devices get solar surplus first when reserving |

Manual device recommendations are limited to slots until the next 06:00 local time, keeping suggestions within an actionable overnight horizon.
