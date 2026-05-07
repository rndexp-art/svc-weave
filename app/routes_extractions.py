"""POST /v1/extractions — the extraction primitive.

Caller passes one source_node_id, a list of new nodes, and a list of new
edges. weave creates an :extraction event + each output node (with
:author to the caller's identity) + each edge (as a real relationship +
its :edge_record shadow). Every output node also gets the legacy
(:extracted_from)->(source) edge for thin-lineage queries.

Edge endpoints can be:
  - "$source" — refers to the extraction's source_node_id
  - "n0", "n1", ... — refers to nodes created in this same call
  - "<element_id>" — refers to an existing node (must already exist)
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import bootstrap, schema, writes
from .auth import Caller, caller as require_caller
from .graph import session
from .schema import SchemaError
from .serializer import node_payload
from .writes import SOURCE_REF


log = logging.getLogger(__name__)

router = APIRouter()


# ---- request / response models -------------------------------------------


class _NodeIn(BaseModel):
    labels: list[str]
    properties: dict[str, Any] = Field(default_factory=dict)
    ref: str                                 # local name within this call ("n0", ...)


class _EdgeIn(BaseModel):
    type: str
    src_ref: str                              # ref | "$source" | element_id
    dst_ref: str
    properties: dict[str, Any] = Field(default_factory=dict)
    edge_ref: str = ""                        # optional; local name for output


class ExtractionIn(BaseModel):
    source_node_id: str
    nodes: list[_NodeIn] = Field(default_factory=list)
    edges: list[_EdgeIn] = Field(default_factory=list)
    prompt_id: str | None = None
    model: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class _NodeOut(BaseModel):
    ref: str
    element_id: str
    labels: list[str]


class _EdgeOut(BaseModel):
    edge_ref: str
    edge_id: str
    src_id: str
    dst_id: str
    type: str


class ExtractionOut(BaseModel):
    extraction_id: str
    nodes: list[_NodeOut]
    edges: list[_EdgeOut]


# ---- ref resolution -------------------------------------------------------


def _resolve_refs(*, source_node_id: str, source_labels: list[str],
                  in_nodes: list[_NodeIn]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build maps from ref -> (placeholder element_id) and ref -> labels.

    The "placeholder" for new-node refs is the ref itself; we substitute
    the real element_id after CREATE. The labels map is what the schema
    edge validator needs; for source/$source it's the looked-up labels.
    """
    refs_to_labels: dict[str, list[str]] = {SOURCE_REF: list(source_labels)}
    seen_refs: set[str] = set()
    for n in in_nodes:
        if not n.ref:
            raise HTTPException(400, "every node needs a non-empty `ref`")
        if n.ref in seen_refs:
            raise HTTPException(400, f"duplicate node ref: {n.ref!r}")
        if n.ref == SOURCE_REF:
            raise HTTPException(400, f"node ref cannot be {SOURCE_REF!r}")
        seen_refs.add(n.ref)
        refs_to_labels[n.ref] = list(n.labels)
    return ({}, refs_to_labels)


def _classify_refs_and_validate_edges(*, edges: list[_EdgeIn], source_node_id: str,
                                      ref_labels: dict[str, list[str]]) -> tuple[set[str], dict[str, list[str]]]:
    """Returns (external_element_ids_referenced, ref_labels_extended_with_externals).
    Validates each edge against the schema given the labels we know."""
    external_ids: set[str] = set()
    for e in edges:
        for r in (e.src_ref, e.dst_ref):
            if r == SOURCE_REF:
                continue
            if r in ref_labels:
                continue
            # treat as element_id of an existing node
            external_ids.add(r)
    # bulk-fetch labels for externals
    ext_labels: dict[str, list[str]] = {}
    if external_ids:
        ext_labels = writes.fetch_labels_for_ids(external_ids)
        missing = external_ids - set(ext_labels.keys())
        if missing:
            raise HTTPException(404, f"unknown node references: {sorted(missing)}")
    full_labels = {**ref_labels, **ext_labels}
    # validate every edge
    for e in edges:
        try:
            schema.validate_edge_create(e.type, full_labels[e.src_ref], full_labels[e.dst_ref])
        except SchemaError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(400, f"edge ref unresolved: {exc}") from exc
    return external_ids, full_labels


# ---- route ----------------------------------------------------------------


@router.post("/v1/extractions", response_model=ExtractionOut)
def create_extraction(body: ExtractionIn, caller: Annotated[Caller, Depends(require_caller)]) -> ExtractionOut:
    # --- 1. Validate every output node against schema ----------------------
    for n in body.nodes:
        try:
            schema.validate_node_create(n.labels, n.properties)
        except SchemaError as exc:
            raise HTTPException(400, f"node ref={n.ref!r}: {exc}") from exc

    # --- 2. Look up the source node + its labels ---------------------------
    src_node = writes.fetch_node(body.source_node_id)
    if src_node is None:
        raise HTTPException(404, f"source_node_id not found: {body.source_node_id}")
    source_labels = list(src_node.get("labels") or [])

    # --- 3. Build ref tables and validate edges ----------------------------
    _, ref_labels = _resolve_refs(
        source_node_id=body.source_node_id,
        source_labels=source_labels,
        in_nodes=body.nodes,
    )
    _, _ = _classify_refs_and_validate_edges(
        edges=body.edges,
        source_node_id=body.source_node_id,
        ref_labels=ref_labels,
    )

    # --- 4. Resolve caller's identity element_id (for :author) -------------
    author_identity_id = bootstrap.resolve_caller_identity_id(
        auth_user_id=caller.auth_user_id,
        provider_name=caller.provider_name,
    )
    if caller.kind == "human" and author_identity_id is None:
        raise HTTPException(
            400,
            "caller has no :identity in graph yet — call POST /v1/agents/{id}/bootstrap first"
        )

    # --- 5. Single transaction: write everything ---------------------------
    extraction_id = str(uuid.uuid4())
    out_nodes: list[_NodeOut] = []
    out_edges: list[_EdgeOut] = []
    with session() as s:
        def _do(tx):
            ref_to_id: dict[str, str] = {SOURCE_REF: body.source_node_id}
            # Create output nodes
            for n in body.nodes:
                created = writes._tx_create_node(
                    tx,
                    labels=n.labels,
                    properties=n.properties,
                    author_identity_element_id=author_identity_id,
                )
                ref_to_id[n.ref] = created.element_id
                out_nodes.append(_NodeOut(
                    ref=n.ref,
                    element_id=created.element_id,
                    labels=list(created.labels),
                ))
            # Create output edges
            output_edge_record_ids: list[str] = []
            for e in body.edges:
                src_id = ref_to_id.get(e.src_ref, e.src_ref)
                dst_id = ref_to_id.get(e.dst_ref, e.dst_ref)
                edge_id = str(uuid.uuid4())
                writes._tx_create_edge(
                    tx, src_id=src_id, dst_id=dst_id, type_=e.type,
                    properties=e.properties, edge_id=edge_id,
                )
                output_edge_record_ids.append(edge_id)
                out_edges.append(_EdgeOut(
                    edge_ref=e.edge_ref or f"e{len(out_edges)}",
                    edge_id=edge_id,
                    src_id=src_id,
                    dst_id=dst_id,
                    type=e.type,
                ))
            # Create the extraction event
            output_node_ids = [n.element_id for n in out_nodes]
            writes._tx_create_extraction_event(
                tx,
                extraction_id=extraction_id,
                agent_auth_user_id=caller.auth_user_id,
                source_id=body.source_node_id,
                prompt_id=body.prompt_id,
                model=body.model,
                meta=body.meta,
                output_node_ids=output_node_ids,
                output_edge_record_ids=output_edge_record_ids,
            )
            return None

        s.execute_write(_do)

    log.info("extraction %s by agent %d: %d nodes, %d edges",
             extraction_id, caller.auth_user_id, len(out_nodes), len(out_edges))
    return ExtractionOut(extraction_id=extraction_id, nodes=out_nodes, edges=out_edges)
