"""JSON Schema 2020-12 subset used by the Agent Yankers documents.

The files under ``schemas/shows/agent_yankers/`` are the contract. This checker
covers the keywords those files use (type, const, enum, properties, required,
additionalProperties, items, string and number bounds, pattern, uniqueItems,
and ``format: date-time``). A schema that adds another keyword fails here, so
a constraint cannot land in the file and be ignored.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from gen_audio.shows.agent_yankers.errors import AgentYankersError, SchemaError

SCHEMA_FILES = {
    "premise": "premise.v1.schema.json",
    "marks": "marks.v1.schema.json",
    "session": "session.v1.schema.json",
    "voiceover": "voiceover.v1.schema.json",
    "highlights": "highlights.v1.schema.json",
    "episode": "episode.v1.schema.json",
}

_ALLOWED = {
    "$schema",
    "$id",
    "title",
    "description",
    "type",
    "const",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
    "uniqueItems",
    "pattern",
    "format",
}
_TYPES = {"object", "array", "string", "boolean", "integer", "number", "null"}
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")


def repo_root() -> Path:
    """Checkout root. This file lives in ``src/gen_audio/shows/agent_yankers``."""
    root = Path(__file__).resolve().parents[4]
    if not (root / "pyproject.toml").is_file():
        raise AgentYankersError(
            "Agent Yankers schemas are resolved from a gen-audio checkout",
            code="missing_schemas",
        )
    return root


def schema_dir() -> Path:
    return repo_root() / "schemas" / "shows" / "agent_yankers"


def example_dir() -> Path:
    return repo_root() / "examples" / "agent_yankers"


def load_schema(name: str) -> dict:
    """Load one shipped schema and reject keywords this checker does not apply."""
    try:
        filename = SCHEMA_FILES[name]
    except KeyError:
        known = ", ".join(sorted(SCHEMA_FILES))
        raise AgentYankersError(f"unknown schema {name!r}; known: {known}", code="schema") from None
    path = schema_dir() / filename
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentYankersError(f"could not read {path.name}: {exc}", code="schema") from exc
    if not isinstance(document, dict):
        raise SchemaError(name, "schema root must be an object")
    _check_schema_document(document, name)
    return document


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaError(str(path), f"invalid JSON: {exc.msg}") from exc


def validate_instance(instance: Any, schema: dict, path: str = "$") -> None:
    """Raise ``SchemaError`` when ``instance`` fails ``schema``."""
    types = schema.get("type")
    if types is not None:
        allowed = (types,) if isinstance(types, str) else tuple(types)
        if not any(_is_type(instance, kind) for kind in allowed):
            raise SchemaError(path, f"expected {' or '.join(allowed)}")
    if "const" in schema and instance != schema["const"]:
        raise SchemaError(path, f"must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(path, "is not one of the allowed values")
    if instance is None:
        return
    if isinstance(instance, str):
        _check_string(instance, schema, path)
    elif isinstance(instance, (int, float)) and not isinstance(instance, bool):
        _check_number(instance, schema, path)
    elif isinstance(instance, list):
        _check_array(instance, schema, path)
    elif isinstance(instance, dict):
        _check_object(instance, schema, path)


def dump_json(document: dict) -> str:
    """Stable UTF-8 JSON with a trailing newline. No ASCII escapes."""
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _check_schema_document(schema: dict, path: str) -> None:
    extra = sorted(set(schema) - _ALLOWED)
    if extra:
        raise SchemaError(path, "unsupported keywords: " + ", ".join(extra))
    types = schema.get("type")
    if types is not None:
        listed = (types,) if isinstance(types, str) else tuple(types)
        unknown = [kind for kind in listed if kind not in _TYPES]
        if unknown:
            raise SchemaError(path, "unknown type " + ", ".join(unknown))
    if "format" in schema and schema["format"] != "date-time":
        raise SchemaError(path, "only format date-time is checked")
    if "required" in schema:
        props = schema.get("properties", {})
        for key in schema["required"]:
            if key not in props:
                raise SchemaError(path, f"required field {key} is not a property")
    for key, sub in schema.get("properties", {}).items():
        if not isinstance(sub, dict):
            raise SchemaError(f"{path}.{key}", "property schema must be an object")
        _check_schema_document(sub, f"{path}.{key}")
    if "items" in schema:
        if not isinstance(schema["items"], dict):
            raise SchemaError(path + "[]", "items schema must be an object")
        _check_schema_document(schema["items"], path + "[]")
    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        _check_schema_document(additional, path + ".*")


def _is_type(instance: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(instance, dict)
    if kind == "array":
        return isinstance(instance, list)
    if kind == "string":
        return isinstance(instance, str)
    if kind == "boolean":
        return isinstance(instance, bool)
    if kind == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if kind == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if kind == "null":
        return instance is None
    raise SchemaError("$", f"unknown type {kind}")


def _check_string(instance: str, schema: dict, path: str) -> None:
    if "minLength" in schema and len(instance) < schema["minLength"]:
        raise SchemaError(path, f"shorter than {schema['minLength']}")
    if "maxLength" in schema and len(instance) > schema["maxLength"]:
        raise SchemaError(path, f"longer than {schema['maxLength']}")
    pattern = schema.get("pattern")
    if pattern is not None and re.search(pattern, instance) is None:
        raise SchemaError(path, "does not match the required pattern")
    if schema.get("format") == "date-time" and not _date_time(instance):
        raise SchemaError(path, "must be a UTC date-time like 2026-03-01T15:00:00Z")


def _date_time(value: str) -> bool:
    if _DATE_TIME.fullmatch(value) is None:
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _check_number(instance: int | float, schema: dict, path: str) -> None:
    if isinstance(instance, float) and instance != instance:
        raise SchemaError(path, "must be finite")
    if "minimum" in schema and instance < schema["minimum"]:
        raise SchemaError(path, f"must be >= {schema['minimum']}")
    if "maximum" in schema and instance > schema["maximum"]:
        raise SchemaError(path, f"must be <= {schema['maximum']}")
    if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
        raise SchemaError(path, f"must be > {schema['exclusiveMinimum']}")
    if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
        raise SchemaError(path, f"must be < {schema['exclusiveMaximum']}")


def _check_array(instance: list, schema: dict, path: str) -> None:
    if "minItems" in schema and len(instance) < schema["minItems"]:
        raise SchemaError(path, f"needs at least {schema['minItems']} items")
    if "maxItems" in schema and len(instance) > schema["maxItems"]:
        raise SchemaError(path, f"has more than {schema['maxItems']} items")
    if schema.get("uniqueItems") is True and not _unique(instance):
        raise SchemaError(path, "items must be unique")
    item_schema = schema.get("items")
    if item_schema is None:
        return
    for index, item in enumerate(instance):
        validate_instance(item, item_schema, f"{path}[{index}]")


def _unique(instance: list) -> bool:
    try:
        return len(instance) == len(set(instance))
    except TypeError:
        seen: list[Any] = []
        for item in instance:
            if item in seen:
                return False
            seen.append(item)
        return True


def _check_object(instance: dict, schema: dict, path: str) -> None:
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in instance:
            raise SchemaError(path, f"missing required field {key}")
    additional = schema.get("additionalProperties", True)
    for key, value in instance.items():
        child = f"{path}.{key}"
        if key in props:
            validate_instance(value, props[key], child)
        elif additional is False:
            raise SchemaError(child, "is not a field in this schema")
        elif isinstance(additional, dict):
            validate_instance(value, additional, child)
