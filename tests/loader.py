"""Load the integration's pure modules by file path.

`learner.py`, `lag.py`, `learner_store.py` and `heuristic.py` have zero Home
Assistant dependency by design, and CI never installs `homeassistant`. Importing
them via `custom_components.truetemp` would pull it in through the
package's `__init__.py`, so they are loaded directly by path instead and
registered under their bare module names — which is also what lets
`learner_store.py`'s `from .lag import ...` fall back to `from lag import ...`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_COMPONENT = (
    Path(__file__).parent.parent / "custom_components" / "truetemp"
)


def load(name: str):
    """Load (and cache) one pure module under its bare name."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _COMPONENT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so sibling fallback imports resolve.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
