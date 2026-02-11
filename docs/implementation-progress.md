# Thermostat Device Implementation Progress

## Overview

Adding a new `thermostat_device` subentry type that manages heating zones with temperature-aware, price/solar-optimized control. The user sets a target temperature and margin per zone. Zeus guarantees temperature stays within the margin while optimizing when to heat.

## Target Use Case

The user's home heating system:
- **Downstairs**: Heat pump (400W) + flow heater (2000W), infloor heating, on/off signal, temp sensor -- single zone control
- **Bedroom 1**: 600W radiator with smart plug + power monitoring, room temp sensor
- **Bedroom 2**: 600W radiator with smart plug + power monitoring, room temp sensor
- **Boiler**: 1700W electric boiler with smart plug -- uses existing switch device subentry (no temp sensor, boiler has internal thermostat)

Total controllable heating: 5300W peak.

## Design Decisions

- **Thermostat devices use a real-time decision engine**, not the slot-counting scheduler used by switch devices
- **3-tier logic**: FORCE ON (at lower bound) / OPTIMIZE (within margin) / FORCE OFF (at upper bound)
- **Urgency-weighted price threshold**: willingness to pay depends on how close temperature is to the lower margin
- **Solar look-ahead**: consider upcoming solar forecast -- coast if free solar is expected soon
- **Multi-device solar sharing**: thermostat devices share solar surplus with switch devices via priority
- **Boiler uses existing switch device subentry**: no thermostat logic needed, just schedule allowed heating windows
- **User-configurable margins**: per zone, allowing e.g. 1C for living room, 2C for bedrooms

## Implementation Steps

### Step 1: Constants (`const.py`)
- [x] Add `SUBENTRY_THERMOSTAT_DEVICE`, `CONF_TEMPERATURE_SENSOR`, `CONF_TARGET_TEMPERATURE`, `CONF_TEMPERATURE_MARGIN`

### Step 2: Config Flow (`config_flow.py`)
- [x] Add `ThermostatDeviceSubentryFlow` with user + reconfigure steps
- [x] Register in `async_get_supported_subentry_types()`
- [x] Schema: switch_entity, power_sensor, temperature_sensor, peak_usage, target_temperature, temperature_margin, priority, min_cycle_time

### Step 3: Data Model (`scheduler.py`)
- [x] Add `ThermostatScheduleRequest` dataclass
- [x] Add `_build_thermostat_requests()` to create requests from subentries

### Step 4: Thermostat Decision Engine (`scheduler.py`)
- [x] Implement `compute_thermostat_decisions()` -- the core algorithm
- [x] 3-tier logic: force on / optimize / force off
- [x] Urgency-weighted price threshold within margin
- [x] Solar surplus awareness (free energy = heat)
- [x] Solar forecast look-ahead (coast if solar coming)
- [x] Multi-device solar sharing by priority

### Step 5: Tests (`tests/test_thermostat.py`)
- [x] Force on at lower bound
- [x] Force off at upper bound
- [x] Optimization within margin -- cheap price heats
- [x] Optimization within margin -- expensive price coasts
- [x] Solar surplus triggers heating
- [x] Solar forecast look-ahead delays heating
- [x] Urgency scaling (near lower = more willing to pay)
- [x] No temperature reading -- safe fallback
- [x] Multiple thermostat devices share solar by priority

### Step 6: Coordinator Integration (`coordinator.py`)
- [x] Add temperature sensor listeners
- [x] Call thermostat decision engine alongside switch scheduler
- [x] Store thermostat results in `schedule_results`
- [x] Check for thermostat devices in `_has_managed_devices()`

### Step 7: Binary Sensor (`binary_sensor.py`)
- [x] Add `ZeusThermostatScheduleSensor` class
- [x] Control underlying switch based on thermostat decisions
- [x] Expose temperature attributes (current, target, margin, bounds)
- [x] Min cycle time enforcement (reuse existing pattern)
- [x] Set up thermostat sensors in `async_setup_entry()`

### Step 8: Sensor (`sensor.py`)
- [x] Add thermostat runtime today sensor (reuse existing pattern)
- [x] Set up in `async_setup_entry()` for thermostat subentries

### Step 9: Strings & Translations
- [x] `strings.json` -- thermostat subentry type, config fields, entity names
- [x] `translations/en.json` -- same

### Step 10: Init (`__init__.py`)
- No changes needed -- platforms already registered, coordinator reloads on subentry changes.

### Step 11: Run Tests & Lint
- [x] All tests pass (62 tests)
- [x] Ruff check clean

## Findings & Notes

- The existing `ScheduleResult` dataclass works perfectly for thermostat decisions -- `should_be_on` maps to "heat or not", `reason` explains why
- The `_get_device_info_for_entity()` pattern in binary_sensor.py can be reused for thermostat devices
- Solar sharing between thermostat and switch devices happens at the coordinator level -- thermostat decisions consume solar from the same pool
- Temperature sensor unavailability is handled as a safety fallback (default to heating if below target, off if above)
- The coordinator's `_has_switch_devices()` was renamed to `_has_managed_devices()` to cover both switch and thermostat subentries
