"""DataUpdateCoordinator for ClimateOptimizer.

Fetches indoor/outdoor temperature, a weather forecast (wind + sun
enrichment only), sun geometry, and optionally a Nordpool price, normalizes
units, and hands everything to the pure `heuristic.compute()` function.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfSpeed, UnitOfTemperature
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    CONF_COMFORT_MAX_C,
    CONF_COMFORT_MIN_C,
    CONF_ENABLE_PRICE_COMPENSATION,
    CONF_INDOOR_TARGET_TEMPERATURE,
    CONF_INDOOR_TEMP_SENSOR,
    CONF_K_INDOOR,
    CONF_K_SUN,
    CONF_K_WIND,
    CONF_NORDPOOL_PRICE_ENTITY,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRICE_MAX_DROP_C,
    CONF_PRICE_THRESHOLD_MAX,
    CONF_PRICE_THRESHOLD_START,
    CONF_UPDATE_INTERVAL_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_COMFORT_MAX_C,
    DEFAULT_COMFORT_MIN_C,
    DEFAULT_ENABLE_PRICE_COMPENSATION,
    DEFAULT_INDOOR_TARGET_TEMPERATURE,
    DEFAULT_K_INDOOR,
    DEFAULT_K_SUN,
    DEFAULT_K_WIND,
    DEFAULT_PRICE_MAX_DROP_C,
    DEFAULT_PRICE_THRESHOLD_MAX,
    DEFAULT_PRICE_THRESHOLD_START,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DOMAIN,
)
from .heuristic import HeuristicInputs, HeuristicParams, HeuristicResult, compute

_LOGGER = logging.getLogger(__name__)


def _entry_value(entry: ConfigEntry, key: str, default):
    """Options override data, both fall back to `default`."""
    return entry.options.get(key, entry.data.get(key, default))


def _state_is_usable(state: State | None) -> bool:
    return state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)


def _as_float(state: State, attribute: str | None = None) -> float | None:
    raw = state.attributes.get(attribute) if attribute else state.state
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class ClimateOptimizerCoordinator(DataUpdateCoordinator[HeuristicResult]):
    """Fetches inputs each cycle and computes the heuristic result."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        interval_minutes = _entry_value(
            entry, CONF_UPDATE_INTERVAL_MINUTES, DEFAULT_UPDATE_INTERVAL_MINUTES
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=interval_minutes),
        )

    def _params(self) -> HeuristicParams:
        entry = self.entry
        return HeuristicParams(
            indoor_target_c=_entry_value(
                entry, CONF_INDOOR_TARGET_TEMPERATURE, DEFAULT_INDOOR_TARGET_TEMPERATURE
            ),
            enable_price_compensation=_entry_value(
                entry, CONF_ENABLE_PRICE_COMPENSATION, DEFAULT_ENABLE_PRICE_COMPENSATION
            ),
            k_indoor=_entry_value(entry, CONF_K_INDOOR, DEFAULT_K_INDOOR),
            k_wind=_entry_value(entry, CONF_K_WIND, DEFAULT_K_WIND),
            k_sun=_entry_value(entry, CONF_K_SUN, DEFAULT_K_SUN),
            comfort_min_c=_entry_value(entry, CONF_COMFORT_MIN_C, DEFAULT_COMFORT_MIN_C),
            comfort_max_c=_entry_value(entry, CONF_COMFORT_MAX_C, DEFAULT_COMFORT_MAX_C),
            price_threshold_start=_entry_value(
                entry, CONF_PRICE_THRESHOLD_START, DEFAULT_PRICE_THRESHOLD_START
            ),
            price_threshold_max=_entry_value(
                entry, CONF_PRICE_THRESHOLD_MAX, DEFAULT_PRICE_THRESHOLD_MAX
            ),
            price_max_drop_c=_entry_value(
                entry, CONF_PRICE_MAX_DROP_C, DEFAULT_PRICE_MAX_DROP_C
            ),
        )

    def _read_indoor_temp_c(self) -> tuple[float | None, bool]:
        """Return (indoor_temp_c, indoor_data_available).

        Unlike the weather entity, a missing indoor sensor doesn't leave us
        with nothing to report: we can still fall back to publishing the raw
        outdoor temperature (see heuristic.compute), so this soft-degrades
        rather than raising UpdateFailed.
        """
        entity_id = _entry_value(self.entry, CONF_INDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Indoor temperature sensor %s is unavailable; publishing the "
                "raw outdoor temperature uncompensated for indoor error this cycle",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Indoor temperature sensor %s has no numeric state", entity_id)
            return None, False
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS), True

    def _read_raw_outdoor_temp_c(self) -> float:
        """The dedicated outdoor sensor is the sole source of the current
        outdoor temperature. There's no sane fallback if it's missing (unlike
        the indoor sensor, there's nothing left to publish), so this is the
        one required-source read that still hard-fails via UpdateFailed.
        """
        entity_id = _entry_value(self.entry, CONF_OUTDOOR_TEMP_SENSOR, None)
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} is unavailable")
        value = _as_float(state)
        if value is None:
            raise UpdateFailed(f"Outdoor temperature sensor {entity_id} has no numeric state")
        unit = state.attributes.get("unit_of_measurement", UnitOfTemperature.CELSIUS)
        return TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)

    async def _read_forecast(self) -> tuple[float, float | None, bool]:
        """Return (wind_speed_ms, cloud_coverage_pct, forecast_data_available).

        The weather entity is enrichment only now (raw outdoor temperature
        comes from a dedicated sensor), so any failure here — including the
        entity itself being unavailable — soft-degrades to no wind/sun
        adjustment rather than failing the whole update.
        """
        weather_entity_id = _entry_value(self.entry, CONF_WEATHER_ENTITY, None)
        weather_state = self.hass.states.get(weather_entity_id)
        if not _state_is_usable(weather_state):
            _LOGGER.warning(
                "Weather entity %s is unavailable; continuing without wind/sun forecast data",
                weather_entity_id,
            )
            return 0.0, None, False

        for forecast_type in ("hourly", "daily"):
            try:
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": weather_entity_id, "type": forecast_type},
                    blocking=True,
                    return_response=True,
                )
                forecast = response[weather_entity_id]["forecast"]
                if not forecast:
                    continue
                first = forecast[0]
                wind_speed = first.get("wind_speed")
                cloud_coverage = first.get("cloud_coverage")
                if wind_speed is None:
                    continue
                wind_unit = weather_state.attributes.get(
                    "wind_speed_unit", UnitOfSpeed.METERS_PER_SECOND
                )
                wind_speed_ms = SpeedConverter.convert(
                    float(wind_speed), wind_unit, UnitOfSpeed.METERS_PER_SECOND
                )
                return (
                    wind_speed_ms,
                    float(cloud_coverage) if cloud_coverage is not None else None,
                    True,
                )
            except Exception as err:  # noqa: BLE001 - soft-degrade on any forecast failure
                _LOGGER.debug(
                    "Forecast type %s unavailable for %s: %s",
                    forecast_type,
                    weather_entity_id,
                    err,
                )
        _LOGGER.warning(
            "Could not retrieve wind/cloud forecast for %s; continuing without it",
            weather_entity_id,
        )
        return 0.0, None, False

    def _read_sun_elevation(self) -> float:
        sun_state = self.hass.states.get("sun.sun")
        if sun_state is None:
            return 0.0
        try:
            return float(sun_state.attributes.get("elevation", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _read_price(self) -> tuple[float | None, bool]:
        entity_id = _entry_value(self.entry, CONF_NORDPOOL_PRICE_ENTITY, None)
        if not entity_id:
            return None, False
        state = self.hass.states.get(entity_id)
        if not _state_is_usable(state):
            _LOGGER.warning(
                "Nordpool price entity %s is unavailable; price compensation "
                "will contribute 0 this cycle",
                entity_id,
            )
            return None, False
        value = _as_float(state)
        if value is None:
            _LOGGER.warning("Nordpool price entity %s has no numeric state", entity_id)
            return None, False
        return value, True

    async def _async_update_data(self) -> HeuristicResult:
        raw_outdoor_temp_c = self._read_raw_outdoor_temp_c()
        indoor_temp_c, indoor_ok = self._read_indoor_temp_c()
        wind_speed_ms, cloud_coverage_pct, forecast_ok = await self._read_forecast()
        sun_elevation_deg = self._read_sun_elevation()
        current_price, price_ok = self._read_price()

        inputs = HeuristicInputs(
            indoor_temp_c=indoor_temp_c,
            indoor_data_available=indoor_ok,
            raw_outdoor_temp_c=raw_outdoor_temp_c,
            wind_speed_ms=wind_speed_ms,
            sun_elevation_deg=sun_elevation_deg,
            cloud_coverage_pct=cloud_coverage_pct,
            forecast_data_available=forecast_ok,
            current_price=current_price,
            price_data_available=price_ok,
        )
        return compute(inputs, self._params())
