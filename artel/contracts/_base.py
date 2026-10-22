"""Validation helpers shared by the contract dataclasses. Every failure names the offending field."""
from __future__ import annotations

import math
import types
from dataclasses import fields as _fields
from typing import Any, Mapping, Optional, Sequence


class ContractError(ValueError):
    """A payload does not satisfy its contract; the message starts with the dotted field path."""

    def __init__(self, field: str, problem: str):
        super().__init__(f"{field}: {problem}")
        self.field = field
        self.problem = problem


def obj(d: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(d, Mapping):
        raise ContractError(path or "<root>", "expected an object")
    return d


def _get(d: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in d:
        raise ContractError(f"{path}{key}", "required field is missing")
    return d[key]


def string(d, key, path="", nullable=False, nonempty=False) -> Optional[str]:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if not isinstance(v, str):
        raise ContractError(f"{path}{key}", "expected a string")
    if nonempty and not v:
        raise ContractError(f"{path}{key}", "must not be empty")
    return v


def integer(d, key, path="", minimum=0) -> int:
    v = _get(d, key, path)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractError(f"{path}{key}", "expected an integer")
    if v < minimum:
        raise ContractError(f"{path}{key}", f"must be >= {minimum}")
    return v


def number(d, key, path="", minimum=0.0, maximum=None) -> float:
    v = _get(d, key, path)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ContractError(f"{path}{key}", "expected a number")
    if not math.isfinite(v):
        raise ContractError(f"{path}{key}", "must be a finite number")
    if v < minimum or (maximum is not None and v > maximum):
        hi = "" if maximum is None else f" and <= {maximum}"
        raise ContractError(f"{path}{key}", f"must be >= {minimum}{hi}")
    return v


def boolean(d, key, path="") -> bool:
    v = _get(d, key, path)
    if not isinstance(v, bool):
        raise ContractError(f"{path}{key}", "expected a boolean")
    return v


def enum(d, key, allowed: Sequence[str], path="", nullable=False) -> Optional[str]:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if v not in allowed:
        raise ContractError(f"{path}{key}", f"must be one of {sorted(allowed)}, got {v!r}")
    return v


def strings(d, key, path="", maximum=None) -> tuple:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    for i, x in enumerate(v):
        if not isinstance(x, str):
            raise ContractError(f"{path}{key}[{i}]", "expected a string")
    if maximum is not None and len(v) > maximum:
        raise ContractError(f"{path}{key}", f"at most {maximum} items allowed, got {len(v)}")
    return tuple(v)


def nested(d, key, cls, path="", nullable=False):
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    return cls.from_dict(v, f"{path}{key}.")


def nested_list(d, key, cls, path="") -> tuple:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    return tuple(cls.from_dict(x, f"{path}{key}[{i}].") for i, x in enumerate(v))


def mapping(d, key, path="", nullable=False) -> Optional[dict]:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if not isinstance(v, Mapping):
        raise ContractError(f"{path}{key}", "expected an object")
    return freeze(v)


def mapping_list(d, key, path="", item_types: Optional[Mapping[str, tuple]] = None) -> tuple:
    """A list of objects; item_types maps each required key to the tuple of accepted Python types."""
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    out = []
    for i, x in enumerate(v):
        if not isinstance(x, Mapping):
            raise ContractError(f"{path}{key}[{i}]", "expected an object")
        for k, types_ in (item_types or {}).items():
            if k not in x:
                raise ContractError(f"{path}{key}[{i}].{k}", "required field is missing")
            val = x[k]
            if not isinstance(val, types_) or (isinstance(val, bool) and bool not in types_):
                names = " or ".join(t.__name__ if t is not Mapping else "object" for t in types_)
                raise ContractError(f"{path}{key}[{i}].{k}", f"expected {names}")
        out.append(freeze(x))
    return tuple(out)


def freeze(v: Any) -> Any:
    """Deep, recursive immutability: mappings become read-only proxies over a copy, lists become tuples."""
    if isinstance(v, Mapping):
        return types.MappingProxyType({k: freeze(x) for k, x in v.items()})
    if isinstance(v, (list, tuple)):
        return tuple(freeze(x) for x in v)
    return v


def thaw(v: Any) -> Any:
    """Inverse of freeze: plain dict/list all the way down (a fresh copy, safe to mutate and to JSON-encode)."""
    if isinstance(v, Mapping):
        return {k: thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [thaw(x) for x in v]
    return v


def reject_unknown(d: Mapping[str, Any], cls, path: str) -> None:
    known = {"in" if f.name == "in_" else f.name for f in _fields(cls)}
    for k in d:
        if k not in known:
            raise ContractError(f"{path}{k}", "unknown field")


def check_path(path: str) -> str:
    return path if not path or path.endswith(".") else path + "."


def to_plain(v: Any) -> Any:
    """Recursively turn contract objects, tuples and mappings into JSON-compatible values."""
    if hasattr(v, "to_dict"):
        return v.to_dict()
    if isinstance(v, (list, tuple)):
        return [to_plain(x) for x in v]
    if isinstance(v, Mapping):
        return {k: to_plain(x) for k, x in v.items()}
    return v


def string_groups(d, key, path="") -> tuple:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    return tuple(strings({"g": g}, "g", f"{path}{key}[{i}].") for i, g in enumerate(v))
