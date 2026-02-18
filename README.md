# Zeus Energy Manager

A Home Assistant custom integration for managing dynamic energy bills. Zeus fetches real-time energy prices, schedules devices to run at the cheapest times, prioritizes your own solar production over grid power, and controls your solar inverter output during negative pricing.

## Features

- **Dynamic energy pricing** -- fetches 15-minute price slots from Tibber every 15 minutes with automatic retry on failure
- **Device scheduling** -- automatically turns devices on/off at optimal times based on price, solar forecast, and real-time solar production
- **Thermostat control** -- software thermostat for heating zones with price/solar-optimized scheduling
- **Manual device recommendations** -- recommends cheapest run windows for non-smart devices with one-tap reservation
- **Solar inverter output control** -- reduces inverter output during negative prices to avoid paying for grid export
- **Solar forecast integration** -- built-in Forecast.Solar API client with 1-hour caching
- **Energy dashboard sensors** -- hourly price arrays, today/tomorrow forecasts, and price analytics for custom dashboards

## Requirements

- Home Assistant 2025.3.0+
- A Tibber account with a personal access token ([create one here](https://developer.tibber.com/settings/access-token))
- Optional: Solar panels with known declination, azimuth, and kWp for Forecast.Solar integration

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Search for "Zeus" and install it
3. Restart Home Assistant

### Manual

1. Copy the `custom_components/zeus` folder to your `config/custom_components/` directory
2. Restart Home Assistant

## Quick Start

1. **Add the integration** -- Settings > Devices & Services > Add Integration > Zeus. Enter your Tibber access token.
2. **Add a solar inverter** (optional) -- connects to Forecast.Solar for production forecasts
3. **Add a home energy monitor** (optional) -- enables solar surplus calculations
4. **Add devices** -- switch devices (auto-scheduled), thermostat devices (temperature-managed), or manual devices (recommendations only)

See the [Setup Guide](docs/setup.md) for detailed configuration of each subentry type.

## Documentation

| Document | Description |
|---|---|
| [Setup Guide](docs/setup.md) | Detailed setup and configuration for all device types |
| [How It Works](docs/how-it-works.md) | Scheduling algorithms, thermostat control, solar optimization |
| [Entities & Services](docs/entities.md) | All sensors, binary sensors, attributes, and service calls |

## Services

| Service | Description |
|---|---|
| `zeus.set_price_override` | Override current price for testing |
| `zeus.clear_price_override` | Revert to real prices |
| `zeus.run_scheduler` | Manually trigger the scheduler |
| `zeus.reserve_manual_device` | Reserve a time window for a manual device |
| `zeus.cancel_reservation` | Cancel a manual device reservation |

## Debugging

Enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.zeus: debug
```

## Development

```bash
nix develop  # enter dev shell and create .venv (first time only)
nix run      # starts HA dev server on port 8123
pytest tests/ -v  # run tests
ruff check .      # lint
```
