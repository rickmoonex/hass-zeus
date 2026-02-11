"""Constants for the Zeus integration."""

DOMAIN = "zeus"

# Energy providers
ENERGY_PROVIDER_TIBBER = "tibber"
ENERGY_PROVIDERS = [ENERGY_PROVIDER_TIBBER]

# Config keys
CONF_ENERGY_PROVIDER = "energy_provider"

# Solar inverter subentry config keys
CONF_PRODUCTION_ENTITY = "production_entity"
CONF_OUTPUT_CONTROL_ENTITY = "output_control_entity"
CONF_MAX_POWER_OUTPUT = "max_power_output"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_FEED_IN_RATE = "feed_in_rate"

# Home energy monitor subentry config keys
CONF_ENERGY_USAGE_ENTITY = "energy_usage_entity"

# Switch device subentry config keys
CONF_SWITCH_ENTITY = "switch_entity"
CONF_POWER_SENSOR = "power_sensor"
CONF_PEAK_USAGE = "peak_usage"
CONF_DAILY_RUNTIME = "daily_runtime"
CONF_DEADLINE = "deadline"
CONF_PRIORITY = "priority"
CONF_MIN_CYCLE_TIME = "min_cycle_time"

# Subentry types
SUBENTRY_SOLAR_INVERTER = "solar_inverter"
SUBENTRY_HOME_MONITOR = "home_monitor"
SUBENTRY_SWITCH_DEVICE = "switch_device"
