"""Load the provena store app without colliding with intelligence's `app` package."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEL_ROOT = _REPO_ROOT / "intelligence"


def _snapshot_app_modules() -> dict[str, Any]:
    return {name: module for name, module in sys.modules.items() if name == "app" or name.startswith("app.")}


def _clear_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]


def _restore_modules(saved: dict[str, Any]) -> None:
    _clear_app_modules()
    sys.modules.update(saved)


def _prioritize_path(preferred: str, *, hide: str | None = None) -> list[str]:
    """Reorder sys.path so `preferred` wins over the colliding `app` package."""
    paths = [p for p in sys.path if p != hide] if hide else list(sys.path)
    if preferred in paths:
        paths.remove(preferred)
    return [preferred, *paths]


@dataclass
class LoadedApp:
    """Pinned app package kept in sys.modules until restore()."""

    config: Any
    main: Any
    _saved_modules: dict[str, Any]
    _saved_path: list[str]

    def restore(self) -> None:
        sys.path[:] = self._saved_path
        _restore_modules(self._saved_modules)


def load_store_modules(*, pin: bool = True) -> LoadedApp:
    """Return pinned (store_config, store_main) from repo root."""
    saved_modules = _snapshot_app_modules()
    saved_path = list(sys.path)
    _clear_app_modules()
    root = str(_REPO_ROOT)
    intel_root = str(_INTEL_ROOT)
    sys.path[:] = _prioritize_path(root, hide=intel_root)
    store_config = importlib.import_module("app.config")
    store_main = importlib.import_module("app.main")
    if not hasattr(store_config, "get_settings"):
        raise ImportError("loaded app.config is not the provena store module")
    loaded = LoadedApp(store_config, store_main, saved_modules, saved_path)
    if not pin:
        loaded.restore()
    return loaded


def load_intel_modules(*, pin: bool = True) -> LoadedApp:
    """Return pinned (intel_config, intel_main) from intelligence/."""
    saved_modules = _snapshot_app_modules()
    saved_path = list(sys.path)
    _clear_app_modules()
    root = str(_REPO_ROOT)
    intel_root = str(_INTEL_ROOT)
    sys.path[:] = _prioritize_path(intel_root, hide=root)
    intel_config = importlib.import_module("app.config")
    intel_main = importlib.import_module("app.main")
    if not hasattr(intel_config, "settings"):
        raise ImportError("loaded app.config is not the intelligence module")
    loaded = LoadedApp(intel_config, intel_main, saved_modules, saved_path)
    if not pin:
        loaded.restore()
    return loaded