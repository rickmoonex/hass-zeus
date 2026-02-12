"""Constants for the Zeus integration."""

DOMAIN = "zeus"

# Energy providers
ENERGY_PROVIDER_TIBBER = "tibber"
ENERGY_PROVIDERS = [ENERGY_PROVIDER_TIBBER]

# Config keys
CONF_ENERGY_PROVIDER = "energy_provider"
CONF_ACCESS_TOKEN = "access_token"  # noqa: S105

# Tibber API
TIBBER_API_ENDPOINT = "https://api.tibber.com/v1-beta/gql"

# Forecast.Solar API
FORECAST_SOLAR_API_BASE = "https://api.forecast.solar"

# Solar inverter subentry config keys
CONF_PRODUCTION_ENTITY = "production_entity"
CONF_OUTPUT_CONTROL_ENTITY = "output_control_entity"
CONF_MAX_POWER_OUTPUT = "max_power_output"
CONF_FORECAST_ENTITY = "forecast_entity"
CONF_SOLAR_DECLINATION = "solar_declination"
CONF_SOLAR_AZIMUTH = "solar_azimuth"
CONF_SOLAR_KWP = "solar_kwp"
CONF_FORECAST_API_KEY = "forecast_api_key"

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
CONF_USE_ACTUAL_POWER = "use_actual_power"

# Thermostat device subentry config keys
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_TEMPERATURE_TOLERANCE = "temperature_tolerance"

# Manual device subentry config keys
CONF_CYCLE_DURATION = "cycle_duration"
CONF_DYNAMIC_CYCLE_DURATION = "dynamic_cycle_duration"
CONF_DELAY_INTERVALS = "delay_intervals"
CONF_AVG_USAGE = "avg_usage"

# Scheduler
SLOT_DURATION_MIN = 15

# Subentry types
SUBENTRY_SOLAR_INVERTER = "solar_inverter"
SUBENTRY_HOME_MONITOR = "home_monitor"
SUBENTRY_SWITCH_DEVICE = "switch_device"
SUBENTRY_THERMOSTAT_DEVICE = "thermostat_device"
SUBENTRY_MANUAL_DEVICE = "manual_device"
