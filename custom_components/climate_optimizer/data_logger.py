"""Local JSONL data logging, for offline model testing/backtesting later.

Unlike heuristic.py/rc_model.py/mpc.py, this is NOT a pure module — it writes
to disk, so it needs `homeassistant` for executor-job scheduling (blocking
file I/O must never run directly on the event loop).

Why this exists: HA's recorder purges history by default (commonly ~10
days), and even its long-term statistics only keep hourly min/mean/max
aggregates — too coarse to re-fit an RLS estimator or backtest an MPC plan
against real history. This appends one full-resolution record per
coordinator cycle to a local file, so a future session can replay real data
through a candidate model change (e.g. comparing linear vs sqrt wind
scaling, or validating the RC model offline) without waiting for new live
data. Opt-in, off by default (see CONF_ENABLE_DATA_LOGGING) — purely local,
nothing is transmitted anywhere.

Scope note: this logs raw physical inputs and computed results per cycle,
plus the multi-hour forecast arrays (price, outdoor temp, wind, solar) MPC
planned against that cycle — needed to faithfully replay/backtest a past
MPC decision later, since forecasts get revised over time and realised
values aren't a substitute for what was actually known at the time.

When an optional power sensor is configured, each record also gets a coarse
per-cycle energy/cost estimate (see coordinator._cycle_energy_and_cost) —
the only real (non-proxy-unit) savings signal in the project. On installs
where that sensor is shared with hot water production it isn't attributable
to space heating alone; see README.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Only needed for type hints; kept out of the runtime import so this
    # module stays loadable (and unit-testable) without homeassistant
    # installed, same as rc_store.py/rc_model.py — safe because
    # `from __future__ import annotations` never evaluates these at runtime.
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_DIR_NAME = "climate_optimizer_data"

# Rotate a log once it crosses this size, so a long-running install doesn't
# accumulate one unbounded file. Gzip on rotation rather than shrinking the
# original in place, since key names (repeated on every JSONL line) compress
# extremely well and this keeps every past line intact and independently
# replayable, just under a .gz extension.
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB


def _rotate(path: Path) -> None:
    """Gzip the current log to a timestamped sibling and remove the
    original, so the next append starts a fresh file. The timestamp is UTC
    and to-the-second, matching this project's other rename-safety
    convention (see rc_store.py)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rotated = path.with_name(f"{path.stem}.{stamp}{path.suffix}.gz")
    with path.open("rb") as src, gzip.open(rotated, "wb") as dst:
        shutil.copyfileobj(src, dst)
    path.unlink()


def _append_line(path: Path, line: str) -> None:
    """Blocking file append — only ever call via the executor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= MAX_LOG_BYTES:
        _rotate(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def log_file_path(hass: HomeAssistant, entry_id: str) -> Path:
    """The JSONL file for one config entry.

    Keyed by entry_id (stable and unique) rather than the entry's title, so
    renaming a zone later never orphans or collides with its history.
    """
    return Path(hass.config.path(DATA_DIR_NAME)) / f"{entry_id}.jsonl"


async def async_log_record(
    hass: HomeAssistant, entry_id: str, record: dict[str, Any]
) -> None:
    """Append one record as a JSON line. Never raises — logs and swallows
    on failure, since a full disk or permissions issue here must not affect
    the real output any more than a bug in the RC/MPC shadow code would."""
    path = log_file_path(hass, entry_id)
    line = json.dumps(record, default=str)
    try:
        await hass.async_add_executor_job(_append_line, path, line)
    except OSError as err:
        _LOGGER.warning("Could not write ClimateOptimizer data log %s: %s", path, err)
