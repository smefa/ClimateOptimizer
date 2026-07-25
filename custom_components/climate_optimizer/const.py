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
# Optional: the heat pump's live electrical power draw. Purely for local
# history logging / a diagnostic echo sensor — never an input to the
# heuristic/RC/MPC (it's an effect of past decisions, not something a control
# decision should react to). On many installs this circuit also serves hot
# water production, so it is NOT a clean space-heating-only signal — see
# README's "Local history logging" section.
CONF_POWER_SENSOR = "power_sensor"
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
# Price compensation v2
CONF_PRICE_COMFORT_TIER = "price_comfort_tier"
CONF_K_PRICE = "k_price"
CONF_COLD_TAPER_START_C = "cold_taper_start_c"
CONF_COLD_TAPER_FULL_C = "cold_taper_full_c"
CONF_COLD_TAPER_MIN_FACTOR = "cold_taper_min_factor"
CONF_PRECHARGE_MAX_BOOST_C = "precharge_max_boost_c"
CONF_UPDATE_INTERVAL_MINUTES = "update_interval_minutes"
CONF_HEATING_CUTOFF_C = "heating_cutoff_c"
CONF_ENABLE_WIND_RC = "enable_wind_rc"
CONF_RC_WIND_REFERENCE_MS = "rc_wind_reference_ms"
# Phase 3 MPC (shadow/advisory only) options
CONF_MPC_HORIZON_HOURS = "mpc_horizon_hours"
CONF_MPC_MAX_HEATING_DELTA_C = "mpc_max_heating_delta_c"
CONF_MPC_MIN_CONFIDENCE = "mpc_min_confidence"
CONF_ENABLE_DATA_LOGGING = "enable_data_logging"

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
# Price compensation v2. Tier "mid" is the balanced default. k_price is the
# dedicated braking gain (decoupled from k_indoor) — deliberately large so a
# spike produces a decisive compensated-outdoor bump. The cold taper scales
# braking authority from full at -10°C down to 40% at -20°C.
DEFAULT_PRICE_COMFORT_TIER = "mid"
DEFAULT_K_PRICE = 5.0
DEFAULT_COLD_TAPER_START_C = -10.0
DEFAULT_COLD_TAPER_FULL_C = -20.0
DEFAULT_COLD_TAPER_MIN_FACTOR = 0.4
# Max pre-heat boost (°C) in the cheap window before a spike. Only the High
# tier pre-charges; set to 0 to disable.
DEFAULT_PRECHARGE_MAX_BOOST_C = 1.0
DEFAULT_UPDATE_INTERVAL_MINUTES = 15
DEFAULT_HEATING_CUTOFF_C = 18.0
DEFAULT_ENABLE_WIND_RC = False
DEFAULT_RC_WIND_REFERENCE_MS = 5.0
# MPC defaults. A 24 h horizon spans a full day-ahead price cycle (Nordpool
# publishes tomorrow's prices ~13:00 local, so most of the day a 24 h horizon
# is fully covered by real data). 8 degC of heating authority matches the
# heuristic's typical compensation-delta magnitude. min_confidence 1.0 means
# "only trust once the RC model has fully warmed up"; it is shadow-mode only
# regardless, so this gate just governs the reported trustworthiness flag.
DEFAULT_MPC_HORIZON_HOURS = 24
DEFAULT_MPC_MAX_HEATING_DELTA_C = 8.0
DEFAULT_MPC_MIN_CONFIDENCE = 1.0
DEFAULT_ENABLE_DATA_LOGGING = False
