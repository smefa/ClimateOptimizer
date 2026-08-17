"""Local JSONL data logging, for offline model testing/backtesting later.

Unlike heuristic.py/learner.py/lag.py, this is NOT a pure module — it writes
to disk, so it needs `homeassistant` for executor-job scheduling (blocking
file I/O must never run directly on the event loop).

Why this exists: HA's recorder purges history by default (commonly ~10
days), and even its long-term statistics only keep hourly min/mean/max
aggregates — too coarse to judge a controller or replay a candidate change.
This appends one full-resolution record per coordinator cycle to a local
file, so a future session can drive a modified controller through real
history instead of waiting months for new live data per attempt. This is
exactly how the previous thermal model was evaluated — and disproved — so
it is the mechanism by which the current one gets held to the same standard.
Opt-in, off by default (see CONF_ENABLE_DATA_LOGGING) — purely local,
nothing is transmitted anywhere.

Scope note: each record carries the raw physical inputs, the learner's
internal state (which moves every cycle and is not reconstructible from
stored config), and the price forecast the decision actually used. That last
one matters because forecasts get revised: the realised prices are not a
substitute for what was known at decision time.
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
    # installed, same as learner_store.py/learner.py — safe because
    # `from __future__ import annotations` never evaluates these at runtime.
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DATA_DIR_NAME = "truetemp_data"

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
    convention (see learner_store.py)."""
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
    the real output. Best-effort, exactly like the output push."""
    path = log_file_path(hass, entry_id)
    line = json.dumps(record, default=str)
    try:
        await hass.async_add_executor_job(_append_line, path, line)
    except OSError as err:
        _LOGGER.warning("Could not write TrueTemp data log %s: %s", path, err)
