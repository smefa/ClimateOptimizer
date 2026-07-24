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
not full multi-hour forecast snapshots — faithfully replaying MPC's exact
historical decisions would additionally need the forecast arrays it saw at
the time, which is a bigger feature deliberately left for later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_DIR_NAME = "climate_optimizer_data"


def _append_line(path: Path, line: str) -> None:
    """Blocking file append — only ever call via the executor."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
