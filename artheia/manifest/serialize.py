"""Serialize a ``SoftwareSpecification`` to a deployable manifest.

A vehicle ``syscomp.py`` file builds up a layered :class:`SoftwareSpecification`
by squashing layers together. That object is the *design* manifest: rich
Python types (frozensets, enums, layers with ``Undefined`` slots). Before
the runtime can consume it we have to:

1. **Simplify** — resolve every layer to its concrete ``_Frozen`` form,
   reject any ``Undefined`` that survived, evaluate ``Defer`` values.
2. **Convert** — turn the frozen dataclasses into plain Python primitives
   (dicts, lists, strings, ints).
3. **Emit** — write a deterministic YAML document.

The result is the *deploy* manifest. ``docs/autosar/manifest.md`` calls
step 1+2+3 collectively the ``SERIALIZATION`` step.

The output schema here is intentionally lean — it carries the vehicle
identity, hardware specification, compute elements with their package
instances, and the platform-wide configs (gateway, hive, tunnel). Legacy
mosaic-specific protobuf payloads from ``rig.yaml`` are *not* reproduced;
those move to ``.art`` package/system definitions (see the artheia MANUAL).
"""

from __future__ import annotations

import dataclasses
import enum
from ipaddress import IPv4Address, IPv6Address
from pathlib import PurePath
from typing import Any

import yaml

from artheia.manifest.core import (
    SoftwareSpecification,
    VehicleInstance,
    _SoftwareSpecification,
)
from artheia.manifest.transform import Default, Layer, Undefined


_SKIP = object()  # sentinel for "drop this key/element entirely"


def _to_primitive(value: Any) -> Any:
    """Recursively turn dataclasses, enums, sets and paths into JSON/YAML-safe values.

    Undefined fields are dropped. Default(x) is unwrapped to x.
    """
    if value is None:
        return None
    if isinstance(value, Undefined):
        # Default subclasses Undefined; unwrap before treating as Undefined.
        if isinstance(value, Default):
            return _to_primitive(value.default)
        return _SKIP
    if isinstance(value, enum.Enum):
        return value.value if isinstance(value.value, (str, int, float, bool)) else value.name
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = _to_primitive(getattr(value, f.name))
            if v is _SKIP:
                continue
            out[f.name] = v
        return out
    if isinstance(value, (set, frozenset)):
        items = [_to_primitive(v) for v in value]
        items = [i for i in items if i is not _SKIP]
        try:
            return sorted(items, key=lambda x: yaml.dump(x, sort_keys=True))
        except Exception:
            return items
    if isinstance(value, (list, tuple)):
        return [_to_primitive(v) for v in value if _to_primitive(v) is not _SKIP]
    if isinstance(value, dict):
        out2: dict[str, Any] = {}
        for k, v in value.items():
            pv = _to_primitive(v)
            if pv is _SKIP:
                continue
            out2[str(k)] = pv
        return out2
    if isinstance(value, (PurePath, IPv4Address, IPv6Address)):
        return str(value)
    # str/int subclasses (NewType, HostCompute(Identity), IPv4Address, …) must
    # be cast back to their base type so PyYAML can represent them.
    if isinstance(value, str) and type(value) is not str:
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and type(value) is not int:
        return int(value)
    if isinstance(value, float) and type(value) is not float:
        return float(value)
    if isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def simplify(spec: SoftwareSpecification) -> _SoftwareSpecification:
    """Strict simplify: resolve all Layer slots and reject any Undefined.

    Use when the model is fully specified. The looser ``to_dict`` / ``to_yaml``
    walk the layered form directly and silently drop Undefined fields, which is
    what the ``generate-manifest`` CLI uses by default.
    """
    if not isinstance(spec, Layer):
        raise TypeError(f"expected a Layer, got {type(spec).__name__}")
    return spec.simplify()


def to_dict(
    software: SoftwareSpecification,
    vehicle: VehicleInstance | None = None,
) -> dict[str, Any]:
    """Turn the design manifest into a plain dict (the deploy manifest).

    Walks the layered Python object directly: Undefined fields are dropped,
    Default(x) wrappers are unwrapped to x. No strict ``simplify()`` step,
    so partially-specified configurations still serialize.
    """
    out: dict[str, Any] = {
        "software": _to_primitive(software),
    }
    if vehicle is not None:
        out["vehicle"] = _to_primitive(vehicle)
    return out


def to_yaml(
    software: SoftwareSpecification,
    vehicle: VehicleInstance | None = None,
) -> str:
    """Convenience: ``to_dict`` plus deterministic YAML dump."""
    return yaml.safe_dump(
        to_dict(software, vehicle=vehicle),
        sort_keys=True,
        default_flow_style=False,
    )
