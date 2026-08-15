"""Constants for the TrueTemp integration.

No `homeassistant.*` imports here: `heuristic.py`, `learner.py` and `lag.py`
are meant to be importable and unit-testable with zero Home Assistant
dependency, and they would transitively pull in `homeassistant` through this
module if it imported anything from here.

Everything that used to be a hand-typed control coefficient is gone. The
controller no longer has a proportional gain to tune, a price gain to size, or
a cold taper to draw: the offset learner measures what this house actually
needs (see `learner.py`) and the lag estimator measures how long it takes to
get there (see `lag.py`). What remains in config is either an entity to read,
an entity to write, or a genuine occupant preference.
"""

DOMAIN = "truetemp"

# --- Config / options keys: sources to read -------------------------------
CONF_INDOOR_TEMP_SENSOR = "indoor_temp_sensor"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_NORDPOOL_PRICE_ENTITY = "nordpool_price_entity"
# Optional: the heat pump's live electrical power draw. Purely for local
# history logging and a diagnostic echo — never an input to the controller
# (it's an effect of past decisions, not something a control decision should
# react to). On many installs this circuit also serves hot water production,
# so it is NOT a clean space-heating-only signal.
CONF_POWER_SENSOR = "power_sensor"

# --- Config / options keys: targets to write ------------------------------
# Which mechanism carries the published value to the heat pump. The two
# outdoor-spoofing channels below are two ways of reaching the SAME thing (a
# faked outdoor reading) and may be combined; heat-curve-offset writes a
# different quantity (a delta, not a temperature) to a different pump
# parameter, so it is an alternative to both rather than a third independent
# channel — running it alongside outdoor spoofing would compensate twice.
CONF_OUTPUT_MODE = "output_mode"
OUTPUT_MODE_OUTDOOR_SPOOF = "outdoor_temp_spoof"
OUTPUT_MODE_HEAT_CURVE_OFFSET = "heat_curve_offset"
OUTPUT_MODES = (OUTPUT_MODE_OUTDOOR_SPOOF, OUTPUT_MODE_HEAT_CURVE_OFFSET)

# Optional: a number entity to push the published compensated outdoor
# temperature into every cycle (e.g. `number.nibe_ohmigo_temperature`). Only
# used while CONF_OUTPUT_MODE is OUTPUT_MODE_OUTDOOR_SPOOF.
CONF_OUTPUT_NUMBER_ENTITY = "output_number_entity"
# Optional, independent of the above: talk to an OhmOnWifi/Ohmigo device's own
# local HTTP API directly (http://<host>/AT/?T=<value>). If both are set, both
# are pushed to every cycle. Only used while CONF_OUTPUT_MODE is
# OUTPUT_MODE_OUTDOOR_SPOOF.
CONF_OHMONWIFI_HOST = "ohmonwifi_host"
# A number entity backing the pump's own "heat curve offset" ("värmekurvans
# förskjutning") parameter, for pumps that expose one directly instead of
# needing a spoofed outdoor sensor. Only used while CONF_OUTPUT_MODE is
# OUTPUT_MODE_HEAT_CURVE_OFFSET.
CONF_HEAT_CURVE_OFFSET_ENTITY = "heat_curve_offset_entity"
# A hardware fact about the specific pump model, asked once — like
# CONF_HEATING_TYPE, not a tunable gain. This codebase's own offset is
# negative when more heat is wanted (see learner.py's "Sign convention"), but
# most pumps' native curve offset is the other way round: positive means more
# heat. False (the default) flips the sign to match that common convention;
# True is for the pumps that don't.
CONF_HEAT_CURVE_OFFSET_INVERT = "heat_curve_offset_invert"

# --- Config / options keys: occupant preferences --------------------------
CONF_INDOOR_TARGET_TEMPERATURE = "indoor_target_temperature"
# The emitter type. Its only job is to seed the rise/fall lag fallbacks used
# before `lag.py` has measured this house's own response (see
# `lag.fallback_minutes`). Set once at install; not something that changes.
CONF_HEATING_TYPE = "heating_type"
CONF_ENABLE_PRICE_COMPENSATION = "enable_price_compensation"
# The absolute indoor floor for price compensation: how cold the house is
# allowed to get while chasing a cheap hour. The ONLY comfort bound left —
# there is no upper bound because pre-charging is already limited by the tier's
# own boost allowance, so a second clamp above never bound anything.
CONF_COMFORT_MIN_C = "comfort_min_c"
# Master toggles for the two optional weather-derived feedforward terms. Each
# switches its term off entirely rather than relying on a zeroed coefficient.
# With both off, no weather entity is needed at all.
CONF_ENABLE_SOLAR_INPUT = "enable_solar_input"
CONF_ENABLE_WIND_INPUT = "enable_wind_input"

# --- Config / options keys: plumbing --------------------------------------
CONF_ENABLE_DATA_LOGGING = "enable_data_logging"

# --- Config entry schema ---------------------------------------------------
# Bumped whenever a stored entry needs rewriting; see `async_migrate_entry`.
#   1 -> 2  prune the keys left behind by the pre-v0.2.0 RC/MPC/gains stack.
CONFIG_ENTRY_VERSION = 2

# Every key this integration still reads, across both `entry.data` and
# `entry.options`. Which of the two a key lives in is not fixed — `_entry_value`
# layers options over data, and several keys moved between the two as the setup
# flow was reorganized — so this is one flat set rather than two.
#
# The migration prunes by this allow-list rather than by naming the dead keys,
# because the options flow saves `{**existing_options, **page_fields}`: any key
# ever written survives every later edit, so the set of possible strays is the
# union of every version's schema and not worth enumerating. Adding a new
# setting means adding it here too, or it will be pruned on the next bump.
KNOWN_CONFIG_KEYS = frozenset(
    {
        "name",  # homeassistant.const.CONF_NAME, inlined to keep this module HA-free
        CONF_INDOOR_TEMP_SENSOR,
        CONF_OUTDOOR_TEMP_SENSOR,
        CONF_WEATHER_ENTITY,
        CONF_NORDPOOL_PRICE_ENTITY,
        CONF_POWER_SENSOR,
        CONF_OUTPUT_MODE,
        CONF_OUTPUT_NUMBER_ENTITY,
        CONF_OHMONWIFI_HOST,
        CONF_HEAT_CURVE_OFFSET_ENTITY,
        CONF_HEAT_CURVE_OFFSET_INVERT,
        CONF_INDOOR_TARGET_TEMPERATURE,
        CONF_HEATING_TYPE,
        CONF_ENABLE_PRICE_COMPENSATION,
        CONF_COMFORT_MIN_C,
        CONF_ENABLE_SOLAR_INPUT,
        CONF_ENABLE_WIND_INPUT,
        CONF_ENABLE_DATA_LOGGING,
    }
)

# --- Defaults -------------------------------------------------------------
DEFAULT_INDOOR_TARGET_TEMPERATURE = 21.0
DEFAULT_ENABLE_PRICE_COMPENSATION = False
DEFAULT_COMFORT_MIN_C = 18.0
DEFAULT_ENABLE_SOLAR_INPUT = True
DEFAULT_ENABLE_WIND_INPUT = False
DEFAULT_ENABLE_DATA_LOGGING = False
DEFAULT_OUTPUT_MODE = OUTPUT_MODE_OUTDOOR_SPOOF
DEFAULT_HEAT_CURVE_OFFSET_INVERT = False

# The controller runs on a fixed cadence rather than a configurable one. The
# 2026-08-07 validation traced its single largest data-quality failure to the
# estimator being stepped on every event-driven refresh (median 6.6 min, 11% of
# steps under 2 min) instead of on a regular clock: at a 0.1 degC sensor
# resolution that put the signal-to-noise ratio at 0.12 and left 63% of
# transitions reading exactly zero change. The learner now steps on this
# interval and only this interval; refreshes triggered by a source entity
# recovering still republish the output, they just don't advance learning.
UPDATE_INTERVAL_MINUTES = 15
LEARNER_STEP_SECONDS = UPDATE_INTERVAL_MINUTES * 60

# Above this outdoor temperature, compensation is suppressed entirely and the
# raw outdoor temperature is published unmodified. Derived from the target
# rather than configured: heating is not wanted once it is nearly as warm
# outside as the temperature being held indoors, and the margin is what stops a
# cold indoor reading from calling for heat on a warm day.
HEATING_CUTOFF_MARGIN_C = 3.0

# Bundled Lovelace card. The whole www/ directory is served under this
# prefix (see _async_register_frontend) so the card can `import` its
# per-language files out of www/lang/ alongside the main script. The version
# is appended as a cache-busting query string, so bump it whenever the JS
# changes — the main file or any of the language files.
FRONTEND_STATIC_URL_PREFIX = "/truetemp"
FRONTEND_CARD_URL = f"{FRONTEND_STATIC_URL_PREFIX}/truetemp-card.js"
FRONTEND_JS_VERSION = "8"
