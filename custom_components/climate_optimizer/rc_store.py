"""(De)serialization of the RC shadow-model estimator state for persistence.

This module is the *format* half of persisting `RCModelState` across Home
Assistant restarts. It is deliberately PURE — no `homeassistant.*` imports —
for the same reason `rc_model.py` is: so the serialization contract can be
unit-tested in complete isolation (loaded by file path, exactly like
`test_rc_model.py` does) without Home Assistant installed. CI only installs
`pytest`, so anything importing `homeassistant` at module load would break the
test suite.

The actual disk I/O — constructing `homeassistant.helpers.storage.Store`,
`async_load`/`async_delay_save`/`async_save` — lives in `coordinator.py`,
which already depends on Home Assistant. That module calls `serialize_state()`
to produce the JSON-safe payload and `deserialize_state()` to validate and
reconstruct it, falling back to a fresh `rc_model.initial_state()` whenever
this module returns `None`.

Compatibility gates handled here (all cause `deserialize_state()` to return
`None` so the caller cold-starts cleanly rather than crashing on bad data):
  * `schema_version` mismatch  — this module's own serialized-shape version,
    bumped whenever the payload layout changes incompatibly;
  * `model_version` mismatch   — the RC model's algorithm version
    (`rc_model.MODEL_VERSION`); a different estimator formulation must not be
    fed old parameter/covariance vectors;
  * dimensionality mismatch    — the estimator has one of eight shapes
    ([env] (+solar) (+wind) (+gain)) depending on `enable_solar`, `enable_wind`
    and whether the lazy gain dimension has been added (see rc_model's
    docstring). Loading a state into an estimator of the wrong shape would give
    the RLS matrices the wrong size and either crash or silently corrupt the
    fit. Several shapes share a length, so length alone is ambiguous:
    `enable_solar`/`enable_wind` (current config) are passed in explicitly and
    `has_gain` is stored in the payload, and ALL THREE are checked so
    [env, solar, wind] can never be mistaken for [env, solar, gain];
  * any structural corruption  — wrong types, wrong matrix shape, non-finite
    values, missing keys.
"""

from __future__ import annotations

import logging
import math
from typing import Any

# Package-relative import at runtime; the absolute fallback is only taken when
# this file is loaded standalone by file path in the test suite (which has no
# package context and pre-registers `rc_model` in sys.modules), mirroring the
# loader that test_rc_model.py already uses.
try:  # pragma: no cover - trivial import shim
    from .rc_model import MODEL_VERSION, RCModelState
except ImportError:  # pragma: no cover - test path-load fallback
    from rc_model import MODEL_VERSION, RCModelState  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# Store schema version passed to homeassistant Store(...). Kept separate from
# STATE_SCHEMA_VERSION below: this is the on-disk container version HA itself
# understands (and would route through a Store migration hook), whereas
# STATE_SCHEMA_VERSION versions the *shape of our payload* and is validated by
# hand in deserialize_state so we can discard-and-cold-start rather than
# needing a migration for a purely advisory shadow model.
STORAGE_VERSION = 1

# Bump this whenever the serialized layout of RCModelState changes in a way
# that older stored data could not be safely loaded into new code.
#   v2: added the lazy heat-pump gain dimension. The parameter order changed
#       (gain, when present, is now appended LAST rather than sitting at a fixed
#       index) and a `has_gain` flag was added, so v1 payloads cannot be mapped
#       onto the new layout and are discarded -> clean cold start.
#   v3: solar became optional (it is a user-facing input that can be switched
#       off), so it is no longer implied to be present. The layout flags are now
#       stored explicitly rather than reconstructed from the parameter count —
#       see _layout_flags_match for the aliasing bug that forced this.
STATE_SCHEMA_VERSION = 3

STORAGE_KEY_PREFIX = "climate_optimizer_rc_state"


def store_key(entry_id: str) -> str:
    """Storage key for one config entry's RC state.

    Keyed by `entry_id` (stable and unique) rather than the entry title, the
    same rename-safety convention `data_logger.py` uses for its per-entry log.
    """
    return f"{STORAGE_KEY_PREFIX}_{entry_id}"


def serialize_state(
    state: RCModelState, *, enable_solar: bool, enable_wind: bool
) -> dict[str, Any]:
    """Convert an `RCModelState` into a JSON-safe dict for the Store.

    Tuples become lists (JSON has no tuple); everything else is already a
    primitive. `n_params` is recorded so `deserialize_state()` can enforce the
    dimensionality invariant without reconstructing anything first, and the two
    config layout flags are recorded alongside `has_gain` so the layout the
    state was FIT UNDER is known exactly rather than inferred — see
    `_layout_flags_match`.

    `enable_solar`/`enable_wind` are passed in rather than read off the state
    because they are config, not state: `RCModelState` deliberately carries only
    what the estimator learned, and the layout flags live in `RCModelConfig`.
    """
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "model_version": MODEL_VERSION,
        "n_params": len(state.theta),
        "has_gain": state.has_gain,
        "enable_solar": enable_solar,
        "enable_wind": enable_wind,
        "theta": list(state.theta),
        "p_matrix": [list(row) for row in state.p_matrix],
        "accepted_samples": state.accepted_samples,
        "rejected_samples": state.rejected_samples,
        "clip_events": state.clip_events,
        "resid_var": state.resid_var,
        "have_prev": state.have_prev,
        "prev_indoor_temp_c": state.prev_indoor_temp_c,
        "prev_outdoor_temp_c": state.prev_outdoor_temp_c,
        "prev_u_c": state.prev_u_c,
        "prev_solar": state.prev_solar,
        "prev_wind_speed_ms": state.prev_wind_speed_ms,
        "prev_predicted_next_indoor_c": state.prev_predicted_next_indoor_c,
    }


def _finite_or_none(value: Any) -> bool:
    """A prev_* slot is valid if it is None or a finite real number."""
    if value is None:
        return True
    return isinstance(value, (int, float)) and math.isfinite(value)


def _layout_flags_match(
    data: dict[str, Any], enable_solar: bool, enable_wind: bool
) -> bool:
    """Whether the stored state was fit under today's layout configuration.

    Checked instead of (not merely alongside) a parameter-count comparison,
    because counting is genuinely unsound once both optional dimensions exist.
    Consider a state fit as [env, wind, gain] (solar off, wind on, gain added,
    n=3) being loaded under a config of solar on / wind off: the expected count
    is also 1+1+0+1 = 3, so a count check passes and the estimator silently
    reinterprets the learned wind parameter as solar — a corrupt fit that never
    raises. Comparing the flags themselves cannot alias this way.
    """
    stored_solar = data.get("enable_solar")
    stored_wind = data.get("enable_wind")
    if not isinstance(stored_solar, bool) or not isinstance(stored_wind, bool):
        return False
    return stored_solar == enable_solar and stored_wind == enable_wind


def deserialize_state(
    data: Any, *, enable_solar: bool, enable_wind: bool
) -> RCModelState | None:
    """Reconstruct an `RCModelState` from stored data, or `None` if unusable.

    Returns `None` (never raises) on any incompatibility or corruption so the
    caller can cleanly fall back to `rc_model.initial_state()`.
    `enable_solar`/`enable_wind` are the CURRENTLY configured settings; a
    persisted state fit under a different layout is rejected rather than loaded
    (see module docstring).
    """
    if not isinstance(data, dict):
        _LOGGER.debug("RC state discarded: not a dict (%r)", type(data))
        return None

    if data.get("schema_version") != STATE_SCHEMA_VERSION:
        _LOGGER.debug(
            "RC state discarded: schema_version %r != %r",
            data.get("schema_version"),
            STATE_SCHEMA_VERSION,
        )
        return None

    if data.get("model_version") != MODEL_VERSION:
        _LOGGER.debug(
            "RC state discarded: model_version %r != %r",
            data.get("model_version"),
            MODEL_VERSION,
        )
        return None

    # `has_gain` is intrinsic to the saved state (a learned runtime property,
    # not a config toggle); the two layout flags are config and must match
    # today's exactly. Both gates are applied: the flag comparison is the sound
    # one, and the count check that follows it is a cheap structural
    # cross-validation that the payload is self-consistent.
    has_gain = data.get("has_gain")
    if not isinstance(has_gain, bool):
        _LOGGER.debug("RC state discarded: has_gain missing or not a bool (%r)", has_gain)
        return None
    if not _layout_flags_match(data, enable_solar, enable_wind):
        _LOGGER.debug(
            "RC state discarded: fit under enable_solar=%r/enable_wind=%r, "
            "now configured enable_solar=%s/enable_wind=%s",
            data.get("enable_solar"),
            data.get("enable_wind"),
            enable_solar,
            enable_wind,
        )
        return None
    expected_n = (
        1
        + (1 if enable_solar else 0)
        + (1 if enable_wind else 0)
        + (1 if has_gain else 0)
    )
    if data.get("n_params") != expected_n:
        _LOGGER.debug(
            "RC state discarded: n_params %r != expected %d "
            "(enable_solar=%s, enable_wind=%s, has_gain=%s)",
            data.get("n_params"),
            expected_n,
            enable_solar,
            enable_wind,
            has_gain,
        )
        return None

    try:
        theta = tuple(float(x) for x in data["theta"])
        p_rows = data["p_matrix"]
        if not isinstance(p_rows, (list, tuple)):
            raise TypeError("p_matrix is not a list")
        p_matrix = tuple(tuple(float(x) for x in row) for row in p_rows)
        accepted_samples = int(data["accepted_samples"])
        rejected_samples = int(data["rejected_samples"])
        clip_events = int(data["clip_events"])
        resid_var = float(data["resid_var"])
        have_prev = bool(data["have_prev"])
        prev_indoor_temp_c = data["prev_indoor_temp_c"]
        prev_outdoor_temp_c = data["prev_outdoor_temp_c"]
        prev_u_c = data["prev_u_c"]
        prev_solar = data["prev_solar"]
        prev_wind_speed_ms = data["prev_wind_speed_ms"]
        prev_predicted_next_indoor_c = data["prev_predicted_next_indoor_c"]
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("RC state discarded: corrupt payload (%s)", err)
        return None

    # Shape checks: theta and P must be exactly the expected dimensionality.
    if len(theta) != expected_n:
        _LOGGER.debug("RC state discarded: theta length %d", len(theta))
        return None
    if len(p_matrix) != expected_n or any(len(row) != expected_n for row in p_matrix):
        _LOGGER.debug("RC state discarded: p_matrix not %dx%d", expected_n, expected_n)
        return None

    # Finiteness: reject NaN/inf anywhere so a corrupted fit can never be
    # resurrected into the live estimator.
    if not all(math.isfinite(x) for x in theta):
        _LOGGER.debug("RC state discarded: non-finite theta")
        return None
    if not all(math.isfinite(x) for row in p_matrix for x in row):
        _LOGGER.debug("RC state discarded: non-finite p_matrix")
        return None
    if not math.isfinite(resid_var):
        _LOGGER.debug("RC state discarded: non-finite resid_var")
        return None
    if not all(
        _finite_or_none(v)
        for v in (
            prev_indoor_temp_c,
            prev_outdoor_temp_c,
            prev_u_c,
            prev_solar,
            prev_wind_speed_ms,
            prev_predicted_next_indoor_c,
        )
    ):
        _LOGGER.debug("RC state discarded: non-finite prev_* slot")
        return None

    return RCModelState(
        theta=theta,
        p_matrix=p_matrix,
        has_gain=has_gain,
        accepted_samples=accepted_samples,
        rejected_samples=rejected_samples,
        clip_events=clip_events,
        resid_var=resid_var,
        have_prev=have_prev,
        prev_indoor_temp_c=prev_indoor_temp_c,
        prev_outdoor_temp_c=prev_outdoor_temp_c,
        prev_u_c=prev_u_c,
        prev_solar=prev_solar,
        prev_wind_speed_ms=prev_wind_speed_ms,
        prev_predicted_next_indoor_c=prev_predicted_next_indoor_c,
    )
