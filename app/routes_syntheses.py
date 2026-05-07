"""POST /v1/syntheses — the synthesis primitive.

Like extraction, but takes multiple inputs (nodes and/or edge_records).
Each output node gets (:synthesized_from)->(input) for every input.
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


log = logging.getLogger(__name__)

router = APIRouter()


class _NodeIn(BaseModel):
    labels: list[str]
    properties: dict[str, Any] = Field(default_factory=dict)
    ref: str


class _EdgeIn(BaseModel):
    type: str
    src_ref: str
    dst_ref: str
    properties: dict[str, Any] = Field(default_factory=dict)
    edge_ref: str = ""


class SynthesisIn(BaseModel):
    source_node_ids: list[str] = Field(default_factory=list)
    source_edge_ids: list[str] = Field(default_factory=list)   # edge_id values
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


class SynthesisOut(BaseModel):
    synthesis_id: str
    nodes: list[_NodeOut]
    edges: list[_EdgeOut]


def _resolve_edge_record_ids(edge_ids: list[str]) -> dict[str, str]:
    """edge_id -> edge_record element_id."""
    if not edge_ids:
        return {}
    cypher = """
    MATCH (er:edge_record) WHERE er.edge_id IN $ids
    RETURN er.edge_id AS eid, elementId(er) AS element_id
    """
    out: dict[str, str] = {}
    with session() as s:
        for rec in s.run(cypher, ids=edge_ids):
            out[rec["eid"]] = rec["element_id"]
    return out


@router.post("/v1/syntheses", response_model=SynthesisOut)
def create_synthesis(body: SynthesisIn, caller: Annotated[Caller, Depends(require_caller)]) -> SynthesisOut:
    if not body.source_node_ids and not body.source_edge_ids:
        raise HTTPException(400, "synthesis requires at least one source_node_id or source_edge_id")

    # --- validate output nodes ---
    for n in body.nodes:
        try:
            schema.validate_node_create(n.labels, n.properties)
        except SchemaError as exc:
            raise HTTPException(400, f"node ref={n.ref!r}: {exc}") from exc

    # --- look up input nodes + edge_records ---
    node_labels = writes.fetch_labels_for_ids(body.source_node_ids)
    missing_nodes = set(body.source_node_ids) - set(node_labels.keys())
    if missing_nodes:
        raise HTTPException(404, f"unknown source_node_ids: {sorted(missing_nodes)}")

    edge_record_ids = _resolve_edge_record_ids(body.source_edge_ids)
    missing_edges = set(body.source_edge_ids) - set(edge_record_ids.keys())
    if missing_edges:
        raise HTTPException(404, f"unknown source_edge_ids: {sorted(missing_edges)}")

    # --- ref labels: each output node's labels + any external refs in edges ---
    ref_labels: dict[str, list[str]] = {n.ref: list(n.labels) for n in body.nodes}
    external_ids: set[str] = set()
    for e in body.edges:
        for r in (e.src_ref, e.dst_ref):
            if r in ref_labels:
                continue
            external_ids.add(r)
    ext_labels = writes.fetch_labels_for_ids(external_ids) if external_ids else {}
    missing_ext = external_ids - set(ext_labels.keys())
    if missing_ext:
        raise HTTPException(404, f"unknown edge references: {sorted(missing_ext)}")
    full_labels = {**ref_labels, **ext_labels}
    for e in body.edges:
        try:
            schema.validate_edge_create(e.type, full_labels[e.src_ref], full_labels[e.dst_ref])
        except SchemaError as exc:
            raise HTTPException(400, str(exc)) from exc

    # --- caller identity ---
    author_identity_id = bootstrap.resolve_caller_identity_id(
        auth_user_id=caller.auth_user_id,
        provider_name=caller.provider_name,
    )
    if caller.kind == "human" and author_identity_id is None:
        raise HTTPException(400,
            "caller has no :identity in graph yet — call POST /v1/agents/{id}/bootstrap first")

    # --- one-tx write ---
    synthesis_id = str(uuid.uuid4())
    out_nodes: list[_NodeOut] = []
    out_edges: list[_EdgeOut] = []
    with session() as s:
        def _do(tx):
            ref_to_id: dict[str, str] = {}
            for n in body.nodes:
                created = writes._tx_create_node(
                    tx, labels=n.labels, properties=n.properties,
                    author_identity_element_id=author_identity_id,
                )
                ref_to_id[n.ref] = created.element_id
                out_nodes.append(_NodeOut(
                    ref=n.ref, element_id=created.element_id, labels=list(created.labels),
                ))
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
                    edge_id=edge_id, src_id=src_id, dst_id=dst_id, type=e.type,
                ))
            output_node_ids = [n.element_id for n in out_nodes]
            writes._tx_create_synthesis_event(
                tx,
                synthesis_id=synthesis_id,
                agent_auth_user_id=caller.auth_user_id,
                source_node_ids=body.source_node_ids,
                source_edge_ids=body.source_edge_ids,
                prompt_id=body.prompt_id,
                model=body.model,
                meta=body.meta,
                output_node_ids=output_node_ids,
                output_edge_record_ids=output_edge_record_ids,
            )
            return None
        s.execute_write(_do)

    log.info("synthesis %s by agent %d: %d nodes, %d edges, %d input nodes, %d input edges",
             synthesis_id, caller.auth_user_id, len(out_nodes), len(out_edges),
             len(body.source_node_ids), len(body.source_edge_ids))
    return SynthesisOut(synthesis_id=synthesis_id, nodes=out_nodes, edges=out_edges)
