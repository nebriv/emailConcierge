from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Iterable
from types import ModuleType

from email_concierge.config import settings
from email_concierge.extractors.base import Extractor
from email_concierge.log import get_logger

log = get_logger(__name__)


def discover_plugins(
    package: ModuleType | None = None,
    disabled: Iterable[str] | None = None,
) -> list[Extractor]:
    """Walk a plugin package, import each submodule, and instantiate any
    class implementing the Extractor protocol.

    A broken plugin (import error, construction error) is logged and skipped —
    never fatal. Disabled names (by plugin `name` attribute) are excluded.

    Returns instances sorted by (stage, priority). `priority` defaults to 0
    when the plugin class does not declare it.
    """
    if package is None:
        from email_concierge.extractors import plugins as package  # lazy import

    disabled_set = set(disabled) if disabled is not None else set(settings().disabled_plugins_list)

    found: list[Extractor] = []
    for mod_info in pkgutil.iter_modules(package.__path__):
        if mod_info.name.startswith("_"):
            continue
        full_name = f"{package.__name__}.{mod_info.name}"
        try:
            module = importlib.import_module(full_name)
        except Exception:
            log.exception("plugin_import_failed", module=full_name)
            continue

        for cls_name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue  # re-exports, not plugins defined here
            if cls_name.startswith("_") or cls_name.startswith("Base"):
                continue
            if not _looks_like_extractor(cls):
                continue
            try:
                instance = cls()
            except Exception:
                log.exception("plugin_construct_failed", module=full_name, cls=cls_name)
                continue
            if not isinstance(instance, Extractor):
                continue
            if getattr(instance, "name", None) in disabled_set:
                log.info("plugin_disabled", name=instance.name)
                continue
            found.append(instance)
            log.debug(
                "plugin_discovered",
                name=getattr(instance, "name", cls_name),
                stage=getattr(instance, "stage", None),
                priority=getattr(instance, "priority", 0),
            )

    found.sort(key=lambda ex: (getattr(ex, "stage", 99), getattr(ex, "priority", 0)))
    return found


def _looks_like_extractor(cls: type) -> bool:
    """Cheap shape check before calling cls(). Avoids instantiating every
    dataclass / helper class that happens to live in a plugin module.
    """
    return (
        hasattr(cls, "name")
        and hasattr(cls, "stage")
        and callable(getattr(cls, "can_handle", None))
        and callable(getattr(cls, "extract", None))
    )
