"""Validation helpers shared by the contract dataclasses. Every failure names the offending field."""
from __future__ import annotations

import math
import types
from collections.abc import Mapping, Sequence
from dataclasses import fields as _fields
from typing import Any, Literal, cast, overload


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


@overload
def string(d: Any, key: str, path: str = ..., nullable: Literal[False] = ..., nonempty: bool = ...) -> str: ...
@overload
def string(d: Any, key: str, path: str = ..., *, nullable: bool, nonempty: bool = ...) -> str | None: ...
def string(d: Any, key: str, path: str = "", nullable: bool = False, nonempty: bool = False) -> str | None:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if not isinstance(v, str):
        raise ContractError(f"{path}{key}", "expected a string")
    if nonempty and not v:
        raise ContractError(f"{path}{key}", "must not be empty")
    return v


def integer(d: Any, key: str, path: str = "", minimum: int = 0) -> int:
    v = _get(d, key, path)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ContractError(f"{path}{key}", "expected an integer")
    if v < minimum:
        raise ContractError(f"{path}{key}", f"must be >= {minimum}")
    return int(v)


def number(d: Any, key: str, path: str = "", minimum: float = 0.0, maximum: float | None = None) -> float:
    v = _get(d, key, path)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ContractError(f"{path}{key}", "expected a number")
    if not math.isfinite(v):
        raise ContractError(f"{path}{key}", "must be a finite number")
    if v < minimum or (maximum is not None and v > maximum):
        hi = "" if maximum is None else f" and <= {maximum}"
        raise ContractError(f"{path}{key}", f"must be >= {minimum}{hi}")
    return cast(float, v)


def boolean(d: Any, key: str, path: str = "") -> bool:
    v = _get(d, key, path)
    if not isinstance(v, bool):
        raise ContractError(f"{path}{key}", "expected a boolean")
    return v


@overload
def enum(d: Any, key: str, allowed: Sequence[str], path: str = ..., nullable: Literal[False] = ...) -> str: ...
@overload
def enum(d: Any, key: str, allowed: Sequence[str], path: str = ..., *, nullable: bool) -> str | None: ...
def enum(d: Any, key: str, allowed: Sequence[str], path: str = "", nullable: bool = False) -> str | None:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if v not in allowed:
        raise ContractError(f"{path}{key}", f"must be one of {sorted(allowed)}, got {v!r}")
    return cast(str, v)


def strings(d: Any, key: str, path: str = "", maximum: int | None = None) -> tuple[str, ...]:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    for i, x in enumerate(v):
        if not isinstance(x, str):
            raise ContractError(f"{path}{key}[{i}]", "expected a string")
    if maximum is not None and len(v) > maximum:
        raise ContractError(f"{path}{key}", f"at most {maximum} items allowed, got {len(v)}")
    return tuple(v)


def nested(d: Any, key: str, cls: Any, path: str = "", nullable: bool = False) -> Any:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    return cls.from_dict(v, f"{path}{key}.")


def nested_list(d: Any, key: str, cls: Any, path: str = "") -> tuple[Any, ...]:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    return tuple(cls.from_dict(x, f"{path}{key}[{i}].") for i, x in enumerate(v))


def mapping(d: Any, key: str, path: str = "", nullable: bool = False) -> Any:
    v = _get(d, key, path)
    if v is None and nullable:
        return None
    if not isinstance(v, Mapping):
        raise ContractError(f"{path}{key}", "expected an object")
    return freeze(v)


def _check_item_field(item: Mapping[str, Any], where: str, key: str, accepted: tuple[type, ...]) -> None:
    if key not in item:
        raise ContractError(f"{where}.{key}", "required field is missing")
    val = item[key]
    if not isinstance(val, accepted) or (isinstance(val, bool) and bool not in accepted):
        names = " or ".join(t.__name__ if t is not Mapping else "object" for t in accepted)
        raise ContractError(f"{where}.{key}", f"expected {names}")


def mapping_list(
    d: Any, key: str, path: str = "", item_types: Mapping[str, tuple[type, ...]] | None = None
) -> tuple[Any, ...]:
    """A list of objects; item_types maps each required key to the tuple of accepted Python types."""
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    out = []
    for i, x in enumerate(v):
        where = f"{path}{key}[{i}]"
        if not isinstance(x, Mapping):
            raise ContractError(where, "expected an object")
        for k, types_ in (item_types or {}).items():
            _check_item_field(x, where, k, types_)
        out.append(freeze(x))
    return tuple(out)


def freeze(v: Any, path: str = "") -> Any:
    """Deep, recursive immutability over JSON values only: mappings with str keys become read-only proxies over a
    copy, lists become tuples, floats must be finite; anything else (sets, bytes, objects) is rejected."""
    if isinstance(v, Mapping):
        out = {}
        for k, x in v.items():
            if not isinstance(k, str):
                raise ContractError(path or "<root>", f"non-string key {k!r}")
            out[k] = freeze(x, f"{path}{k}.")
        return types.MappingProxyType(out)
    if isinstance(v, (list, tuple)):
        return tuple(freeze(x, f"{path}{i}.") for i, x in enumerate(v))
    if isinstance(v, float) and not math.isfinite(v):
        raise ContractError(path.rstrip(".") or "<root>", "must be finite")
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    raise ContractError(path.rstrip(".") or "<root>", f"not a JSON value: {type(v).__name__}")


def thaw(v: Any) -> Any:
    """Inverse of freeze: plain dict/list all the way down (a fresh copy, safe to mutate and to JSON-encode)."""
    if isinstance(v, Mapping):
        return {k: thaw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [thaw(x) for x in v]
    return v


def reject_unknown(d: Mapping[str, Any], cls: Any, path: str) -> None:
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


def string_groups(d: Any, key: str, path: str = "") -> tuple[tuple[str, ...], ...]:
    v = _get(d, key, path)
    if not isinstance(v, (list, tuple)):
        raise ContractError(f"{path}{key}", "expected a list")
    return tuple(strings({"g": g}, "g", f"{path}{key}[{i}].") for i, g in enumerate(v))
