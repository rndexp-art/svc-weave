"""neo4j -> dict serialization helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _serialize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    if hasattr(value, "iso_format"):  # neo4j.time.DateTime
        return value.iso_format()
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def node_payload(node) -> dict[str, Any]:
    if node is None:
        return {}
    return {
        "id": node.element_id,
        "labels": list(node.labels),
        **{k: _serialize(v) for k, v in node.items()},
    }


def relationship_payload(rel) -> dict[str, Any]:
    if rel is None:
        return {}
    return {
        "id": rel.element_id,
        "type": rel.type,
        "src_id": rel.start_node.element_id if rel.start_node is not None else None,
        "dst_id": rel.end_node.element_id if rel.end_node is not None else None,
        **{k: _serialize(v) for k, v in rel.items()},
    }
