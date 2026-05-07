"""Generic node read/write + lifecycle PATCH (note/task archive/trash/etc).

The lifecycle PATCH endpoints are special-case sugar over the generic
PATCH for the highest-frequency mutations (mirroring the kiln helpers
in services/kiln/app/graph.py:546-620).
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import writes
from .auth import Caller, caller as require_caller, require_internal_token
from .graph import session
from .schema import SchemaError
from .serializer import node_payload


router = APIRouter()


# ---- read -----------------------------------------------------------------


@router.get("/v1/nodes/{element_id:path}", dependencies=[Depends(require_internal_token)])
def get_node(element_id: str) -> dict[str, Any]:
    n = writes.fetch_node(element_id)
    if n is None:
        raise HTTPException(404, "node not found")
    return n


@router.get("/v1/nodes", dependencies=[Depends(require_internal_token)])
def list_nodes(label: str = "", limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    if label:
        cypher = f"MATCH (n:{label}) RETURN n ORDER BY coalesce(n.created_at, datetime({{epochMillis:0}})) DESC LIMIT $limit"
    else:
        cypher = "MATCH (n) RETURN n LIMIT $limit"
    with session() as s:
        return [node_payload(rec["n"]) for rec in s.run(cypher, limit=limit)]


# ---- create ---------------------------------------------------------------


class _CreateNodeIn(BaseModel):
    labels: list[str]
    properties: dict[str, Any] = Field(default_factory=dict)


@router.post("/v1/nodes")
def create_node(body: _CreateNodeIn, caller: Annotated[Caller, Depends(require_caller)]) -> dict[str, Any]:
    from . import bootstrap as bs
    author_id = bs.resolve_caller_identity_id(
        auth_user_id=caller.auth_user_id, provider_name=caller.provider_name,
    )
    try:
        return writes.create_node(
            labels=body.labels, properties=body.properties,
            author_identity_element_id=author_id,
        )
    except SchemaError as e:
        raise HTTPException(400, str(e)) from e


# ---- patch ----------------------------------------------------------------


class _PatchNodeIn(BaseModel):
    set: dict[str, Any] = Field(default_factory=dict)
    unset: list[str] = Field(default_factory=list)


@router.patch("/v1/nodes/{element_id:path}", dependencies=[Depends(require_internal_token)])
def patch_node(element_id: str, body: _PatchNodeIn) -> dict[str, Any]:
    try:
        out = writes.patch_node(element_id=element_id, set_props=body.set, unset_props=body.unset)
    except SchemaError as e:
        raise HTTPException(400, str(e)) from e
    if out is None:
        raise HTTPException(404, "node not found")
    return out


# ---- lifecycle: notes -----------------------------------------------------


_NOTE_LIFECYCLE_FIELDS = {"archived_at", "trashed_at"}
_TASK_LIFECYCLE_FIELDS = {"completed_at", "archived_at", "trashed_at"}


class _SetBoolIn(BaseModel):
    set: bool


@router.patch("/v1/notes/{note_id}/{field}", dependencies=[Depends(require_internal_token)])
def patch_note_lifecycle(note_id: str, field: str, body: _SetBoolIn,
                         auth_user_id: int) -> dict[str, Any]:
    if field not in _NOTE_LIFECYCLE_FIELDS:
        raise HTTPException(400, f"unknown lifecycle field: {field}")
    cypher = """
    MATCH (n:input:note {note_id: $note_id})-[:source]->(s:source)-[:owner]->(:agent {auth_user_id: $auth_user_id})
    SET n[$field] = CASE WHEN $set THEN datetime() ELSE null END,
        n.updated_at = datetime()
    RETURN n
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher, note_id=note_id, auth_user_id=auth_user_id, field=field, set=body.set,
        ).single())
    if rec is None:
        raise HTTPException(404, "note not found or not owned by user")
    return node_payload(rec["n"])


# ---- lifecycle: tasks -----------------------------------------------------


@router.patch("/v1/tasks/{task_id}/{field}", dependencies=[Depends(require_internal_token)])
def patch_task_lifecycle(task_id: str, field: str, body: _SetBoolIn,
                         auth_user_id: int) -> dict[str, Any]:
    if field not in _TASK_LIFECYCLE_FIELDS:
        raise HTTPException(400, f"unknown lifecycle field: {field}")
    cypher = """
    MATCH (t:input:task {task_id: $task_id})-[:source]->(:source)-[:owner]->(:agent {auth_user_id: $auth_user_id})
    SET t[$field] = CASE WHEN $set THEN datetime() ELSE null END,
        t.updated_at = datetime()
    RETURN t
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher, task_id=task_id, auth_user_id=auth_user_id, field=field, set=body.set,
        ).single())
    if rec is None:
        raise HTTPException(404, "task not found or not owned by user")
    return node_payload(rec["t"])


class _StatusIn(BaseModel):
    status: str


_VALID_TASK_STATUSES = {"todo", "doing", "done", "blocked"}


@router.patch("/v1/tasks/{task_id}/status", dependencies=[Depends(require_internal_token)])
def patch_task_status(task_id: str, body: _StatusIn, auth_user_id: int) -> dict[str, Any]:
    status = (body.status or "").lower()
    if status not in _VALID_TASK_STATUSES:
        raise HTTPException(400, f"invalid status: {status}")
    cypher = """
    MATCH (t:input:task {task_id: $task_id})-[:source]->(:source)-[:owner]->(:agent {auth_user_id: $auth_user_id})
    SET t.status = $status,
        t.updated_at = datetime(),
        t.completed_at = CASE WHEN $status = 'done' AND coalesce(t.completed_at, null) IS NULL
                              THEN datetime() ELSE t.completed_at END
    RETURN t
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher, task_id=task_id, auth_user_id=auth_user_id, status=status,
        ).single())
    if rec is None:
        raise HTTPException(404, "task not found or not owned by user")
    return node_payload(rec["t"])
