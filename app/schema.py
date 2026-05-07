"""Schema allowlist + validator.

Loads `schema.yml` once at import time and exposes:

  - `LABELS` / `EDGE_TYPES` — the parsed registry.
  - `validate_node_create(labels, properties)` / `validate_node_patch(...)`
  - `validate_edge_create(type, src_labels, dst_labels)`
  - `label_key(labels)` — turn a list of labels into the dot-key used in
     schema.yml (e.g. ['input', 'note'] -> 'input.note').
  - `category(labels)` — primary category for edge endpoint matching.

The validator intentionally rejects unknown labels and immutable property
mutations. New labels require a `schema.yml` PR.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml


_SCHEMA_PATH = Path(__file__).with_name("schema.yml")


@dataclass(frozen=True)
class LabelDef:
    key: str
    labels: tuple[str, ...]
    required: frozenset[str]
    optional: frozenset[str]
    immutable: frozenset[str]
    open_properties: bool


@dataclass(frozen=True)
class EdgeDef:
    type: str
    from_categories: frozenset[str]
    to_categories: frozenset[str]


@dataclass(frozen=True)
class Schema:
    labels: dict[str, LabelDef] = field(default_factory=dict)
    edges: dict[str, EdgeDef] = field(default_factory=dict)

    def label_def(self, labels: Iterable[str]) -> LabelDef:
        key = label_key(labels)
        if key not in self.labels:
            raise SchemaError(f"unknown label combination: {sorted(set(labels))}")
        return self.labels[key]

    def edge_def(self, type_: str) -> EdgeDef:
        if type_ not in self.edges:
            raise SchemaError(f"unknown edge type: {type_}")
        return self.edges[type_]


class SchemaError(ValueError):
    """Raised when a write violates the schema allowlist."""


def label_key(labels: Iterable[str]) -> str:
    """Turn ['input','note'] into 'input.note' — sorted, deduped."""
    cleaned = sorted({str(l).strip() for l in labels if str(l).strip()})
    return ".".join(cleaned)


def category(labels: Iterable[str]) -> str:
    """Primary category for edge-endpoint matching.

    `input.note`/`input.task` both share category `input`. Single-label
    nodes use the label as the category.
    """
    parts = sorted({str(l).strip() for l in labels if str(l).strip()})
    if not parts:
        raise SchemaError("node has no labels")
    if "input" in parts:
        return "input"
    # For multi-label-but-not-input combinations, prefer the first
    # alphabetical label as category.
    return parts[0]


def _load() -> Schema:
    with _SCHEMA_PATH.open("r") as fh:
        raw = yaml.safe_load(fh)
    labels: dict[str, LabelDef] = {}
    for key, body in (raw.get("labels") or {}).items():
        body = body or {}
        parts = tuple(sorted(str(key).split(".")))
        labels[key] = LabelDef(
            key=key,
            labels=parts,
            required=frozenset(body.get("required") or []),
            optional=frozenset(body.get("optional") or []),
            immutable=frozenset(body.get("immutable") or []),
            open_properties=bool(body.get("open_properties") or False),
        )
    edges: dict[str, EdgeDef] = {}
    for type_, body in (raw.get("edges") or {}).items():
        body = body or {}
        edges[type_] = EdgeDef(
            type=type_,
            from_categories=frozenset(body.get("from") or []),
            to_categories=frozenset(body.get("to") or []),
        )
    return Schema(labels=labels, edges=edges)


SCHEMA: Schema = _load()


def validate_node_create(labels: Iterable[str], properties: dict[str, Any]) -> LabelDef:
    """Validate a CREATE. Returns the matching LabelDef. Raises SchemaError."""
    ld = SCHEMA.label_def(labels)
    keys = set(properties.keys())
    missing = ld.required - keys
    if missing:
        raise SchemaError(f"label {ld.key}: missing required properties: {sorted(missing)}")
    if not ld.open_properties:
        unknown = keys - ld.required - ld.optional
        if unknown:
            raise SchemaError(f"label {ld.key}: unknown properties: {sorted(unknown)}")
    return ld


def validate_node_patch(labels: Iterable[str], set_props: dict[str, Any], unset_props: Iterable[str]) -> LabelDef:
    """Validate a PATCH. Rejects mutations of immutable properties."""
    ld = SCHEMA.label_def(labels)
    set_keys = set(set_props.keys())
    unset_keys = {str(k) for k in unset_props}
    bad = (set_keys | unset_keys) & ld.immutable
    if bad:
        raise SchemaError(f"label {ld.key}: immutable properties cannot be modified: {sorted(bad)}")
    if not ld.open_properties:
        unknown = (set_keys | unset_keys) - ld.required - ld.optional
        if unknown:
            raise SchemaError(f"label {ld.key}: unknown properties: {sorted(unknown)}")
    return ld


def validate_edge_create(type_: str, src_labels: Iterable[str], dst_labels: Iterable[str]) -> EdgeDef:
    ed = SCHEMA.edge_def(type_)
    src_cat = category(src_labels)
    dst_cat = category(dst_labels)
    if src_cat not in ed.from_categories:
        raise SchemaError(
            f"edge {type_}: source category {src_cat!r} not in allowed {sorted(ed.from_categories)}"
        )
    if dst_cat not in ed.to_categories:
        raise SchemaError(
            f"edge {type_}: dest category {dst_cat!r} not in allowed {sorted(ed.to_categories)}"
        )
    return ed
