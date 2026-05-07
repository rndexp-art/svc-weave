"""Generic edge read/write."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import writes
from .auth import Caller, caller as require_caller, require_internal_token
from .graph import session
from .schema import SchemaError
from .serializer import node_payload, relationship_payload


router = APIRouter()


@router.get("/v1/edges/{edge_id}", dependencies=[Depends(require_internal_token)])
def get_edge(edge_id: str) -> dict[str, Any]:
    cypher = """
    MATCH (er:edge_record {edge_id: $edge_id})
    OPTIONAL MATCH (s)-[r]->(d) WHERE r.edge_id = $edge_id
    RETURN er, r, s, d
    """
    with session() as s:
        rec = s.run(cypher, edge_id=edge_id).single()
    if rec is None:
        raise HTTPException(404, "edge not found")
    return {
        "edge_record": node_payload(rec["er"]),
        "relationship": relationship_payload(rec["r"]) if rec["r"] is not None else None,
    }


@router.get("/v1/edges", dependencies=[Depends(require_internal_token)])
def list_edges(type: str = "", src: str = "", dst: str = "", limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if type:
        where.append("er.type = $type")
        params["type"] = type
    if src:
        where.append("er.src_node_id = $src")
        params["src"] = src
    if dst:
        where.append("er.dst_node_id = $dst")
        params["dst"] = dst
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cypher = f"""
    MATCH (er:edge_record)
    {clause}
    RETURN er
    ORDER BY er.created_at DESC
    LIMIT $limit
    """
    with session() as s:
        return [node_payload(rec["er"]) for rec in s.run(cypher, **params)]


class _CreateEdgeIn(BaseModel):
    type: str
    src: str
    dst: str
    properties: dict[str, Any] = Field(default_factory=dict)


@router.post("/v1/edges")
def create_edge(body: _CreateEdgeIn, _caller: Annotated[Caller, Depends(require_caller)]) -> dict[str, Any]:
    try:
        return writes.create_edge_standalone(
            src_id=body.src, dst_id=body.dst, type_=body.type, properties=body.properties,
        )
    except SchemaError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
