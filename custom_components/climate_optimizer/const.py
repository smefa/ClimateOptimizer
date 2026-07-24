"""Constants for the ClimateOptimizer integration.

No `homeassistant.*` imports here: `heuristic.py` is meant to be importable
and unit-testable with zero Home Assistant dependency, and it would
transitively pull in `homeassistant` through this module if it imported
anything from here.
"""

DOMAIN = "climate_optimizer"

# Config / options keys
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_sensor"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_NORDPOOL_PRICE_ENTITY = "nordpool_price_entity"
CONF_INDOOR_TARGET_TEMPERATURE = "indoor_target_temperature"
CONF_ENABLE_PRICE_COMPENSATION = "enable_price_compensation"
CONF_K_INDOOR = "k_indoor"
CONF_K_WIND = "k_wind"
CONF_K_SUN = "k_sun"
CONF_COMFORT_MIN_C = "comfort_min_c"
CONF_COMFORT_MAX_C = "comfort_max_c"
CONF_PRICE_THRESHOLD_START = "price_threshold_start"
CONF_PRICE_THRESHOLD_MAX = "price_threshold_max"
CONF_PRICE_MAX_DROP_C = "price_max_drop_c"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"
CONF_HEATING_CUTOFF_C = "heating_cutoff_c"
CONF_ENABLE_WIND_RC = "enable_wind_rc"
CONF_RC_WIND_REFERENCE_MS = "rc_wind_reference_ms"

# Defaults
DEFAULT_INDOOR_TARGET_TEMPERATURE = 21.0
DEFAULT_ENABLE_PRICE_COMPENSATION = False
DEFAULT_K_INDOOR = 1.5
DEFAULT_K_WIND = 0.3
DEFAULT_K_SUN = 3.0
DEFAULT_COMFORT_MIN_C = 18.0
DEFAULT_COMFORT_MAX_C = 23.0
DEFAULT_PRICE_THRESHOLD_START = 1.5
DEFAULT_PRICE_THRESHOLD_MAX = 3.0
DEFAULT_PRICE_MAX_DROP_C = 1.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_HEATING_CUTOFF_C = 18.0
DEFAULT_ENABLE_WIND_RC = False
DEFAULT_RC_WIND_REFERENCE_MS = 5.0
