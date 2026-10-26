"""Describe `contracts` as language-neutral JSON: every data type with its fields, every enum with its values, every
interface with its methods, and the constants. A port of the core or of a module to another language implements
exactly this (docs/spec/contracts.json); tests/test_spec.py fails when the file and the code drift apart.

    venv/bin/python tools/describe_contracts.py            # write docs/spec/contracts.json
    venv/bin/python tools/describe_contracts.py --check    # exit 1 if it is out of date
"""

from __future__ import annotations

import dataclasses
import enum
import inspect
import json
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "spec" / "contracts.json"
sys.path.insert(0, str(ROOT))

import contracts  # noqa: E402  (after the path)


def type_name(annotation) -> str:
    """A type as a short neutral name: str, int, float, bool, list[T], map[K, V], T | None, a contract type's name."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return "none" if annotation is None else "any"
    if isinstance(annotation, str):
        return annotation
    origin, arguments = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        return " | ".join(type_name(item) for item in arguments)
    if origin in (list, tuple, typing.Sequence, set, frozenset) or str(origin) in ("collections.abc.Sequence",):
        inner = [item for item in arguments if item is not Ellipsis]
        return f"list[{type_name(inner[0]) if inner else 'any'}]"
    if origin in (dict, typing.Mapping) or str(origin) in ("collections.abc.Mapping",):
        return f"map[{type_name(arguments[0])}, {type_name(arguments[1])}]" if arguments else "map[str, any]"
    if origin is typing.Literal:
        return " | ".join(json.dumps(item) for item in arguments)
    if origin is not None and str(origin).startswith("collections.abc.Callable"):
        return "callable"
    if annotation is typing.Any:
        return "any"
    name = getattr(annotation, "__name__", None) or str(annotation)
    return {"str": "str", "int": "int", "float": "float", "bool": "bool", "bytes": "bytes", "Path": "path",
            "NoneType": "none"}.get(name, name)


def hints(item) -> dict:
    """Annotations resolved in the item's own module (raw text where they cannot be)."""
    try:
        return typing.get_type_hints(item)
    except Exception:
        return dict(getattr(item, "__annotations__", {}))


def default(field) -> object:
    if field.default is not dataclasses.MISSING:
        value = field.default
        return value.value if isinstance(value, enum.Enum) else value if isinstance(
            value, (str, int, float, bool, type(None))) else repr(value)
    if field.default_factory is not dataclasses.MISSING:
        factory = field.default_factory
        if factory in (list, dict, tuple, set, frozenset):
            return repr(factory())  # empty
        return "generated"  # (a fresh value each time, such as a random ID)
    return None


def doc(item) -> str:
    return " ".join((inspect.getdoc(item) or "").split())


def describe() -> dict:
    data, enums, interfaces, constants = {}, {}, {}, {}
    for name in sorted(contracts.__all__):
        item = getattr(contracts, name)
        if isinstance(item, type) and issubclass(item, enum.Enum):
            enums[name] = {"doc": doc(item), "values": [member.value for member in item]}
        elif dataclasses.is_dataclass(item):
            types = hints(item)
            data[name] = {"doc": doc(item), "fields": [
                {"name": field.name, "type": type_name(types.get(field.name, field.type)),
                 **({"default": default(field)} if default(field) is not None or
                    field.default is None else {})}
                for field in dataclasses.fields(item)]}
        elif isinstance(item, type) and (getattr(item, "_is_protocol", False) or inspect.isabstract(item)
                                         or issubclass(item, Exception)):
            if issubclass(item, Exception):
                data[name] = {"doc": doc(item), "error": True, "fields": [
                    {"name": "kind", "type": "str"}, {"name": "message", "type": "str"}]}
                continue
            types = hints(item)
            methods = []
            for method_name, method in inspect.getmembers(item, inspect.isfunction):
                if method_name.startswith("_"):
                    continue
                signature = inspect.signature(method)
                method_types = hints(method)
                methods.append({"name": method_name, "async": inspect.iscoroutinefunction(method),
                                "parameters": [{"name": parameter.name,
                                                "type": type_name(method_types.get(parameter.name, parameter.annotation)),
                                                "keyword_only": parameter.kind is parameter.KEYWORD_ONLY,
                                                **({"default": parameter.default if isinstance(
                                                    parameter.default, (str, int, float, bool, type(None)))
                                                    else repr(parameter.default)}
                                                   if parameter.default is not inspect.Parameter.empty else {})}
                                               for parameter in list(signature.parameters.values())[1:]],
                                "returns": type_name(method_types.get("return", signature.return_annotation)),
                                "doc": doc(method)})
            attributes = [{"name": attribute, "type": type_name(kind)} for attribute, kind in types.items()
                          if not attribute.startswith("_")]
            interfaces[name] = {"doc": doc(item), "attributes": attributes, "methods": methods}
        elif isinstance(item, (str, int, float, tuple)):
            constants[name] = list(item) if isinstance(item, tuple) else item
        elif isinstance(item, dict):
            constants[name] = {key: value for key, value in item.items()}
    return {"version": contracts.CONTRACTS_VERSION, "constants": constants, "enums": enums, "types": data,
            "interfaces": interfaces}


def render() -> str:
    return json.dumps(describe(), indent=1, ensure_ascii=False, default=str) + "\n"


if __name__ == "__main__":
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        sys.exit(0 if current == text else 1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
