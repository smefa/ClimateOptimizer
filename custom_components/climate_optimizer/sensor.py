"""Sensor platform for ClimateOptimizer."""

from __future__ import annotations

from dataclasses import asdict

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ClimateOptimizerConfigEntry
from .const import DOMAIN
from .coordinator import ClimateOptimizerCoordinator
from .heuristic import HeuristicResult
from .mpc import MPCResult
from .rc_model import RCModelResult


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClimateOptimizerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            CompensatedOutdoorTempSensor(coordinator, entry),
            IndoorTemperatureSensor(coordinator, entry),
            OutdoorTemperatureSensor(coordinator, entry),
            IndoorTemperatureErrorSensor(coordinator, entry),
            PriceShiftAppliedSensor(coordinator, entry),
            StatusSensor(coordinator, entry),
            # Phase 2 shadow-mode RC model diagnostics (informational only).
            RCThermalTimeConstantSensor(coordinator, entry),
            RCHeatPumpGainSensor(coordinator, entry),
            RCSolarGainSensor(coordinator, entry),
            RCWindGainSensor(coordinator, entry),
            RCModelConfidenceSensor(coordinator, entry),
            RCPredictionErrorSensor(coordinator, entry),
            # Phase 3 shadow/advisory-mode MPC diagnostics (informational only).
            MPCRecommendedDeltaSensor(coordinator, entry),
            MPCStatusSensor(coordinator, entry),
            MPCProjectedSavingsSensor(coordinator, entry),
            MPCPlannedNextTempSensor(coordinator, entry),
        ]
    )


class ClimateOptimizerEntity(CoordinatorEntity[ClimateOptimizerCoordinator]):
    """Common device grouping for all ClimateOptimizer entities of one entry."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="ClimateOptimizer",
            model="Heuristic v1",
            entry_type=DeviceEntryType.SERVICE,
        )


class CompensatedOutdoorTempSensor(ClimateOptimizerEntity, SensorEntity):
    """The main output: the compensated outdoor temperature, fully explained.

    While the activation switch (switch.py) is off ("learn mode"), this
    publishes the raw outdoor temperature unmodified rather than the
    heuristic's recommendation — the heuristic still runs every cycle
    regardless, and its recommendation is always available via the
    `recommended_compensated_outdoor_temp_c` attribute plus the `active` flag,
    so you can preview what it would do before switching it on.
    """

    _attr_translation_key = "compensated_outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_compensated_outdoor_temperature"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return None
        if not self.coordinator.is_active:
            return result.raw_outdoor_temp_c
        return result.compensated_outdoor_temp_c

    @property
    def extra_state_attributes(self) -> dict:
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return {}
        attrs = asdict(result)
        recommended = attrs.pop("compensated_outdoor_temp_c")
        attrs["recommended_compensated_outdoor_temp_c"] = recommended
        attrs["active"] = self.coordinator.is_active
        return attrs


class IndoorTemperatureSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: the real indoor temperature reading, for easy graphing
    alongside the compensated value without digging into attributes."""

    _attr_translation_key = "indoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.indoor_temp_c if result else None


class OutdoorTemperatureSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: the real (raw, uncompensated) outdoor temperature
    reading, for easy graphing alongside the compensated value."""

    _attr_translation_key = "outdoor_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_outdoor_temperature"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.raw_outdoor_temp_c if result else None


class IndoorTemperatureErrorSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: target minus actual indoor temperature."""

    _attr_translation_key = "indoor_temperature_error"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_indoor_temperature_error"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        if result is None or result.indoor_temp_c is None:
            return None
        return round(result.indoor_target_c - result.indoor_temp_c, 2)


class PriceShiftAppliedSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: how much the comfort target is currently being lowered for price."""

    _attr_translation_key = "price_shift_applied"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_price_shift_applied"

    @property
    def native_value(self) -> float | None:
        result: HeuristicResult | None = self.coordinator.data
        return result.price_shift_applied_c if result else None


class StatusSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic: overall health, with a per-source breakdown.

    "error" means the outdoor sensor (the one hard-required source) is
    currently unavailable or the update cycle is otherwise failing entirely —
    the main sensor's value has gone stale. "degraded" means the update cycle
    is succeeding but a soft-degraded source (indoor sensor, wind forecast,
    cloud/sun forecast, or price) is currently down, so that term is
    contributing nothing this cycle. Wind and cloud/sun are tracked
    separately since not every weather integration provides both. Always
    available, unlike every other entity here, because its entire purpose is
    to report problems — including the case where the coordinator itself is
    failing and everything else would otherwise show unavailable.
    """

    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ok", "degraded", "error"]

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        if not self.coordinator.last_update_success:
            return "error"
        result: HeuristicResult | None = self.coordinator.data
        if result is None:
            return "error"
        if (
            not result.indoor_data_available
            or not result.wind_data_available
            or not result.cloud_data_available
            or (self.coordinator.price_configured and not result.price_data_available)
        ):
            return "degraded"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict:
        result: HeuristicResult | None = self.coordinator.data
        last_error = self.coordinator.last_exception
        attrs: dict = {
            "outdoor_sensor_ok": self.coordinator.last_update_success,
            "last_error": str(last_error) if last_error else None,
            "data_logging_enabled": self.coordinator.data_logging_enabled,
        }
        if self.coordinator.data_log_path:
            attrs["data_log_path"] = self.coordinator.data_log_path
        if result is not None:
            attrs["indoor_sensor_ok"] = result.indoor_data_available
            attrs["wind_forecast_ok"] = result.wind_data_available
            attrs["cloud_sun_forecast_ok"] = result.cloud_data_available
            if self.coordinator.price_configured:
                attrs["price_ok"] = result.price_data_available
        return attrs


class RCThermalTimeConstantSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): estimated thermal time constant tau = R*C.

    Also carries the full RC-estimator result as attributes (reason string,
    parameter estimates, accepted/rejected counts, covariance trace, etc.),
    mirroring how the main sensor exposes the heuristic explanation. Note: R
    and C are not separately identifiable from indoor-temperature dynamics
    alone, so the identifiable time constant is reported instead.
    """

    _attr_translation_key = "rc_thermal_time_constant"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_thermal_time_constant"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        if result is None or not (result.time_constant_h == result.time_constant_h):
            return None
        return round(result.time_constant_h, 2)

    @property
    def extra_state_attributes(self) -> dict:
        result: RCModelResult | None = self.coordinator.rc_result
        if result is None:
            return {}
        attrs = asdict(result)
        attrs.pop("time_constant_h", None)
        return attrs


class RCHeatPumpGainSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): estimated effective heat-pump gain.

    The C-normalised effect on indoor temperature per degC of compensation
    delta the heuristic applies. Dimensionless proxy, not a physical power.

    Reads *unavailable* (None) until the gain dimension has actually been added
    to the model — which only happens once a real compensation delta has excited
    the heat pump (activation switch on AND not suppressed by the summer
    heating-cutoff). This is deliberately distinct from a learned value of 0.0:
    "we have not modelled the pump yet" is not the same as "the pump's modelled
    effect is zero", and conflating them would be misleading. The `gain_modeled`
    flag is also exposed on the RC time-constant sensor's attributes.
    """

    _attr_translation_key = "rc_heat_pump_gain"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_heat_pump_gain"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        if result is None or not result.gain_modeled:
            return None
        return round(result.theta_gain, 4)


class RCSolarGainSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): estimated solar gain coefficient."""

    _attr_translation_key = "rc_solar_gain"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_solar_gain"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        return round(result.theta_solar, 4) if result else None


class RCWindGainSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): estimated wind-sensitivity coefficient.

    Only meaningful when the optional wind term is enabled in options
    (advanced, off by default) — reads a permanently-pinned 0.0 otherwise,
    since the estimator is genuinely 3-dimensional (no wind parameter to
    estimate at all) when the feature is disabled. See rc_model.py's module
    docstring for the interaction-term design and why it's opt-in.
    """

    _attr_translation_key = "rc_wind_gain"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_wind_gain"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        return round(result.theta_wind, 4) if result else None


class RCModelConfidenceSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): estimator maturity / confidence, 0-100%."""

    _attr_translation_key = "rc_model_confidence"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_model_confidence"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        return round(result.confidence * 100.0, 1) if result else None


class RCPredictionErrorSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (shadow model): last-cycle indoor prediction error.

    Actual indoor temperature this cycle minus what the model predicted for it
    last cycle. The headline accuracy metric for the shadow estimator.
    """

    _attr_translation_key = "rc_prediction_error"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_rc_prediction_error"

    @property
    def native_value(self) -> float | None:
        result: RCModelResult | None = self.coordinator.rc_result
        if result is None or result.prediction_error_c is None:
            return None
        return round(result.prediction_error_c, 3)


class MPCRecommendedDeltaSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (MPC, advisory/shadow only): the recommended next-step
    compensation delta from the receding-horizon plan.

    ADVISORY ONLY — this never influences the compensated outdoor temperature;
    the heuristic pipeline gated by switch.<name>_active is still the only thing
    that controls anything. This sensor carries the full MPC result as
    attributes: the plan's reason string, the binding constraint, projected
    cost/savings, the predicted indoor-temperature trajectory, the full
    per-step plan, the trustworthiness gate result, and explicit echoes of the
    RC-model parameters (gain, tau, solar, wind if enabled) the plan was
    actually computed from this cycle — for troubleshooting how a plan arose.
    """

    _attr_translation_key = "mpc_recommended_delta"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mpc_recommended_delta"

    @property
    def native_value(self) -> float | None:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None or result.recommended_delta_c is None:
            return None
        return round(result.recommended_delta_c, 2)

    @property
    def extra_state_attributes(self) -> dict:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None:
            return {}
        attrs = asdict(result)
        attrs.pop("recommended_delta_c", None)  # already the state value
        return attrs


class MPCStatusSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (MPC, advisory only): the planner's status / trustworthiness.

    `ok` = a trustworthy plan; `not_trustworthy` = a plan was computed but the
    underlying RC model isn't mature/plausible enough to rely on it yet;
    `infeasible` = comfort bounds can't be held over the horizon (best-effort
    plan returned); `no_forecast`/`no_data`/`degenerate` = missing inputs.
    """

    _attr_translation_key = "mpc_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "ok",
        "not_trustworthy",
        "infeasible",
        "no_forecast",
        "no_data",
        "degenerate",
    ]

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mpc_status"

    @property
    def native_value(self) -> str | None:
        result: MPCResult | None = self.coordinator.mpc_result
        return result.status if result else None

    @property
    def extra_state_attributes(self) -> dict:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None:
            return {}
        return {
            "trustworthy": result.trustworthy,
            "binding_constraint": result.binding_constraint,
            "forecast_valid_steps": result.forecast_valid_steps,
            "steps_planned": result.steps_planned,
            "horizon_hours": result.horizon_hours,
            "confidence": result.confidence,
            "accepted_samples": result.accepted_samples,
            "reason": result.reason,
        }


class MPCProjectedSavingsSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (MPC, advisory only): projected horizon cost savings vs a
    myopic hold-target baseline, in RELATIVE proxy units (price × degC), not a
    currency figure — the absolute thermal scale is unidentifiable, so this is
    meaningful for comparison/tracking, not as a money amount."""

    _attr_translation_key = "mpc_projected_savings"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mpc_projected_savings"

    @property
    def native_value(self) -> float | None:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None or result.projected_savings is None:
            return None
        return round(result.projected_savings, 3)

    @property
    def extra_state_attributes(self) -> dict:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None:
            return {}
        return {
            "total_cost": result.total_cost,
            "baseline_cost": result.baseline_cost,
            "units": "relative proxy units (price × °C), not currency",
        }


class MPCPlannedNextTempSensor(ClimateOptimizerEntity, SensorEntity):
    """Diagnostic (MPC, advisory only): the indoor temperature the plan
    predicts at the end of the first (applied) step."""

    _attr_translation_key = "mpc_planned_next_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: ClimateOptimizerCoordinator, entry: ClimateOptimizerConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_mpc_planned_next_temperature"

    @property
    def native_value(self) -> float | None:
        result: MPCResult | None = self.coordinator.mpc_result
        if result is None or result.predicted_next_temp_c is None:
            return None
        return round(result.predicted_next_temp_c, 2)
