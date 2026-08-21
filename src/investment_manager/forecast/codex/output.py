from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import ValidationError


def strict_output_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return the strict JSON Schema subset accepted by Codex."""

    normalized = deepcopy(schema)
    validation_keywords = (
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "minItems",
        "maxItems",
    )

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            for keyword in validation_keywords:
                node.pop(keyword, None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized


def safe_validation_diagnostics(
    error: ValidationError,
    schema: Mapping[str, Any],
) -> dict[str, int | str]:
    """Describe schema failures without retaining model-generated content."""

    errors = error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )
    allowed_fields = _schema_property_names(schema)
    error_types = sorted({str(item.get("type", "unknown")) for item in errors})
    locations = sorted(
        {
            ".".join(
                "#"
                if isinstance(segment, int)
                else segment
                if isinstance(segment, str) and segment in allowed_fields
                else "*"
                for segment in item.get("loc", ())
            )
            or "ROOT"
            for item in errors
        }
    )
    return {
        "schema_error_count": len(errors),
        "schema_error_types": _bounded_labels(error_types),
        "schema_error_locations": _bounded_labels(locations),
    }


def _schema_property_names(schema: Mapping[str, Any]) -> frozenset[str]:
    names: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, Mapping):
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                names.update(str(name) for name in properties)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return frozenset(names)


def _bounded_labels(labels: list[str]) -> str:
    return ",".join(labels[:8])[:512] or "NONE"
