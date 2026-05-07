"""Bootstrap, source access, mirror, processed, granted-sources,
pending-notes, list-sources-for-user, user-scoped reads.

These are the endpoints that replace per-service helpers in
services/kiln/app/graph.py and services/explorer/app/graph.py.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import bootstrap as bs, config
from .auth import require_internal_token
from .graph import session
from .serializer import node_payload


router = APIRouter()


# ---- human bootstrap -------------------------------------------------------


class _BootstrapHumanIn(BaseModel):
    email: str | None = None
    name: str | None = None


@router.post("/v1/agents/{auth_user_id}/bootstrap", dependencies=[Depends(require_internal_token)])
def bootstrap_human(auth_user_id: int, body: _BootstrapHumanIn) -> dict[str, Any]:
    bs.bootstrap_human_agent(auth_user_id=auth_user_id, email=body.email, name=body.name)
    cypher = "MATCH (a:agent {auth_user_id: $id}) RETURN a"
    with session() as s:
        rec = s.run(cypher, id=auth_user_id).single()
    if rec is None:
        raise HTTPException(500, "bootstrap did not create agent")
    return node_payload(rec["a"])


# ---- source access (grant / revoke / mirror) ------------------------------


class _GrantIn(BaseModel):
    to_agent_auth_user_id: int
    capability: str = "readable"           # readable | writeable | owner


_VALID_CAPS = {"readable", "writeable", "owner"}


@router.post("/v1/sources/{source_id:path}/grant", dependencies=[Depends(require_internal_token)])
def grant(source_id: str, body: _GrantIn) -> dict[str, Any]:
    if body.capability not in _VALID_CAPS:
        raise HTTPException(400, f"invalid capability: {body.capability}")
    cap = body.capability
    cypher = f"""
    MATCH (s:source) WHERE elementId(s) = $source_id
    MATCH (a:agent {{auth_user_id: $agent_id}})
    MERGE (s)-[:{cap}]->(a)
    RETURN s
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher, source_id=source_id, agent_id=body.to_agent_auth_user_id,
        ).single())
    if rec is None:
        raise HTTPException(404, "source or agent not found")
    return {"granted": True, "source_id": source_id, "capability": cap,
            "to_agent_auth_user_id": body.to_agent_auth_user_id}


class _RevokeIn(BaseModel):
    from_agent_auth_user_id: int
    capability: str = "readable"


@router.post("/v1/sources/{source_id:path}/revoke", dependencies=[Depends(require_internal_token)])
def revoke(source_id: str, body: _RevokeIn) -> dict[str, Any]:
    if body.capability not in _VALID_CAPS:
        raise HTTPException(400, f"invalid capability: {body.capability}")
    cap = body.capability
    cypher = f"""
    MATCH (s:source)-[r:{cap}]->(a:agent {{auth_user_id: $agent_id}})
        WHERE elementId(s) = $source_id
    DELETE r
    """
    with session() as s:
        s.execute_write(lambda tx: tx.run(
            cypher, source_id=source_id, agent_id=body.from_agent_auth_user_id,
        ).consume())
    return {"granted": False, "source_id": source_id, "capability": cap,
            "from_agent_auth_user_id": body.from_agent_auth_user_id}


_ENSURE_MIRROR = """
MATCH (orig:source) WHERE elementId(orig) = $orig_id
OPTIONAL MATCH (orig)-[:owner]->(owner:agent)
MERGE (kp:provider {name: $provider_name})
  ON CREATE SET kp.created_at = datetime(), kp.kind = $provider_kind
MERGE (mirror:source {provider_name: $provider_name, external_id: $orig_id})
  ON CREATE SET mirror.created_at = datetime(),
                mirror.kind = $mirror_kind,
                mirror.title = coalesce(orig.title, orig.provider_name + ' mirror')
SET mirror.mirrors_provider_name = orig.provider_name,
    mirror.mirrors_external_id   = orig.external_id
MERGE (mirror)-[:provider]->(kp)
MERGE (mirror)-[:mirrors]->(orig)
WITH mirror, owner
FOREACH (_ IN CASE WHEN owner IS NOT NULL THEN [1] ELSE [] END |
    MERGE (mirror)-[:owner]->(owner)
    MERGE (mirror)-[:readable]->(owner)
    MERGE (mirror)-[:writeable]->(owner)
)
RETURN mirror
"""


@router.post("/v1/sources/{source_id:path}/mirror", dependencies=[Depends(require_internal_token)])
def ensure_mirror(source_id: str, for_provider: str) -> dict[str, Any]:
    """Ensure a per-provider mirror source exists for source_id. Returns the mirror."""
    if not for_provider:
        raise HTTPException(400, "for_provider is required")
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            _ENSURE_MIRROR,
            orig_id=source_id,
            provider_name=for_provider,
            provider_kind=for_provider,
            mirror_kind=f"{for_provider}_mirror",
        ).single())
    if rec is None:
        raise HTTPException(404, "source not found")
    return node_payload(rec["mirror"])


# ---- granted sources / pending notes / processed --------------------------


@router.get("/v1/agents/{auth_user_id}/granted-sources", dependencies=[Depends(require_internal_token)])
def granted_sources(auth_user_id: int, exclude_provider: str = "") -> list[dict[str, Any]]:
    cypher = """
    MATCH (s:source)-[:readable]->(:agent {auth_user_id: $auth_user_id})
    WHERE $exclude_provider = '' OR s.provider_name <> $exclude_provider
    RETURN s
    """
    with session() as s:
        return [node_payload(rec["s"]) for rec in s.run(
            cypher, auth_user_id=auth_user_id, exclude_provider=exclude_provider,
        )]


_PENDING_NOTES = """
MATCH (s:source) WHERE elementId(s) = $source_id
MATCH (n:input:note)-[:source]->(s)
WHERE NOT EXISTS {
    MATCH (:agent {auth_user_id: $agent_id})-[:processed]->(n)
}
WITH n, s
OPTIONAL MATCH (n)-[:author]->(i:identity)
RETURN n, i
ORDER BY coalesce(n.created_at, n.sent_at, datetime({epochMillis:0})) ASC
LIMIT $limit
"""


@router.get("/v1/sources/{source_id:path}/pending-notes", dependencies=[Depends(require_internal_token)])
def pending_notes(source_id: str, for_agent_id: int, limit: int = 25) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    out: list[dict[str, Any]] = []
    with session() as s:
        for rec in s.run(_PENDING_NOTES, source_id=source_id, agent_id=for_agent_id, limit=limit):
            row = node_payload(rec["n"])
            row["author"] = node_payload(rec["i"]) if rec["i"] is not None else None
            out.append(row)
    return out


class _ProcessedIn(BaseModel):
    target_element_ids: list[str] = Field(default_factory=list)


@router.post("/v1/agents/{auth_user_id}/processed", dependencies=[Depends(require_internal_token)])
def mark_processed(auth_user_id: int, body: _ProcessedIn) -> dict[str, Any]:
    """Idempotently :processed-mark a list of nodes from this agent."""
    cypher = """
    UNWIND $ids AS eid
    MATCH (n) WHERE elementId(n) = eid
    MATCH (a:agent {auth_user_id: $auth_user_id})
    MERGE (a)-[r:processed]->(n)
        ON CREATE SET r.at = datetime()
    """
    ids = [eid for eid in body.target_element_ids if eid]
    if not ids:
        return {"marked": 0}
    with session() as s:
        s.execute_write(lambda tx: tx.run(cypher, ids=ids, auth_user_id=auth_user_id).consume())
    return {"marked": len(ids)}


# ---- user-scoped reads ----------------------------------------------------


_LIST_USER_SOURCES = """
MATCH (a:agent {auth_user_id: $auth_user_id})
MATCH (s:source)-[:owner]->(a)
WHERE $exclude_provider = '' OR s.provider_name <> $exclude_provider
OPTIONAL MATCH (s)-[r:readable]->(:agent {auth_user_id: $for_grant_check})
RETURN s, (r IS NOT NULL) AS granted
ORDER BY granted DESC, coalesce(s.title, s.provider_name) ASC
"""


@router.get("/v1/users/{auth_user_id}/sources", dependencies=[Depends(require_internal_token)])
def user_sources(auth_user_id: int, exclude_provider: str = "kiln",
                 grant_check_for: int = 0) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with session() as s:
        for rec in s.run(
            _LIST_USER_SOURCES,
            auth_user_id=auth_user_id,
            exclude_provider=exclude_provider,
            for_grant_check=grant_check_for,
        ):
            row = node_payload(rec["s"])
            row["granted"] = bool(rec["granted"])
            out.append(row)
    return out


_USER_NOTES = """
MATCH (a:agent {auth_user_id: $auth_user_id})
MATCH (n:input:note)-[:source]->(s:source)-[:owner]->(a)
WHERE NOT (s.provider_name = $exclude_provider)
  AND ($include_trashed OR coalesce(n.trashed_at, null) IS NULL)
  AND ($include_archived OR coalesce(n.archived_at, null) IS NULL)
WITH n, s
OPTIONAL MATCH (n)-[:author]->(i:identity)
RETURN n, s, i,
       [(n)<-[:extracted_from]-(t:input:task) | t.task_id] AS task_ids
ORDER BY coalesce(n.created_at, n.sent_at, datetime({epochMillis:0})) DESC
LIMIT $limit
"""


@router.get("/v1/users/{auth_user_id}/notes", dependencies=[Depends(require_internal_token)])
def user_notes(auth_user_id: int, limit: int = 100,
               include_trashed: bool = False, include_archived: bool = False,
               exclude_provider: str = "kiln") -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    out: list[dict[str, Any]] = []
    with session() as s:
        for rec in s.run(
            _USER_NOTES, auth_user_id=auth_user_id, limit=limit,
            include_trashed=include_trashed, include_archived=include_archived,
            exclude_provider=exclude_provider,
        ):
            row = node_payload(rec["n"])
            row["source"] = node_payload(rec["s"])
            row["author"] = node_payload(rec["i"]) if rec["i"] is not None else None
            row["task_ids"] = list(rec["task_ids"] or [])
            out.append(row)
    return out


_USER_TASKS = """
MATCH (a:agent {auth_user_id: $auth_user_id})
MATCH (t:input:task)-[:source]->(mirror:source)-[:owner]->(a)
WHERE mirror.provider_name = $kiln_provider
  AND ($status = '' OR t.status = $status)
  AND ($include_completed OR coalesce(t.completed_at, null) IS NULL)
  AND ($include_archived OR coalesce(t.archived_at, null) IS NULL)
  AND ($include_trashed OR coalesce(t.trashed_at, null) IS NULL)
OPTIONAL MATCH (t)-[:extracted_from]->(n:input:note)
OPTIONAL MATCH (t)-[:author]->(i:identity)
RETURN t, n, i, mirror
ORDER BY coalesce(t.created_at, datetime({epochMillis:0})) DESC
LIMIT $limit
"""


@router.get("/v1/users/{auth_user_id}/tasks", dependencies=[Depends(require_internal_token)])
def user_tasks(auth_user_id: int, limit: int = 200, status: str = "",
               include_completed: bool = True,
               include_archived: bool = False,
               include_trashed: bool = False,
               kiln_provider: str = "kiln") -> list[dict[str, Any]]:
    limit = max(1, min(1000, int(limit)))
    out: list[dict[str, Any]] = []
    with session() as s:
        for rec in s.run(
            _USER_TASKS, auth_user_id=auth_user_id, limit=limit, status=status,
            include_completed=include_completed, include_archived=include_archived,
            include_trashed=include_trashed, kiln_provider=kiln_provider,
        ):
            row = node_payload(rec["t"])
            row["note"] = node_payload(rec["n"]) if rec["n"] is not None else None
            row["author"] = node_payload(rec["i"]) if rec["i"] is not None else None
            row["mirror_source"] = node_payload(rec["mirror"])
            out.append(row)
    return out


# Subgraph for explorer — agent + identities + sources + providers + recent notes.
_SUBGRAPH = """
MATCH (a:agent {auth_user_id: $auth_user_id})
OPTIONAL MATCH (i:identity)-[:agent]->(a)
OPTIONAL MATCH (i)-[:provider]->(ip:provider)
OPTIONAL MATCH (s:source)-[:owner]->(a)
OPTIONAL MATCH (s)-[:provider]->(sp:provider)
WITH a,
     collect(DISTINCT i)  AS identities,
     collect(DISTINCT ip) AS identity_providers,
     collect(DISTINCT s)  AS sources,
     collect(DISTINCT sp) AS source_providers
RETURN a, identities, identity_providers, sources, source_providers
"""


@router.get("/v1/users/{auth_user_id}/subgraph", dependencies=[Depends(require_internal_token)])
def user_subgraph(auth_user_id: int) -> dict[str, Any]:
    with session() as s:
        rec = s.run(_SUBGRAPH, auth_user_id=auth_user_id).single()
    if rec is None:
        raise HTTPException(404, "agent not found")
    return {
        "agent": node_payload(rec["a"]),
        "identities": [node_payload(n) for n in rec["identities"] if n is not None],
        "identity_providers": [node_payload(n) for n in rec["identity_providers"] if n is not None],
        "sources": [node_payload(n) for n in rec["sources"] if n is not None],
        "source_providers": [node_payload(n) for n in rec["source_providers"] if n is not None],
    }
