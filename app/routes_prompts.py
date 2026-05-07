"""Prompt registry CRUD.

A prompt is a (kind, pattern_key, version) triple. Versions are monotonic
per (kind, pattern_key). Status flows: shadow -> active -> retired.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import Caller, caller as require_caller, require_internal_token
from .config import EXTRACTION_WATCHER_AGENT_ID, SYNTHESIS_WATCHER_AGENT_ID
from .graph import session
from .serializer import node_payload


router = APIRouter()


_VALID_KINDS = {"extraction", "synthesis"}
_VALID_STATUSES = {"shadow", "active", "retired"}


def fingerprint_for(*, kind: str, input_label_set: list[str],
                    output_label_set: list[str], output_property_keys: list[str]) -> str:
    payload = json.dumps({
        "kind": kind,
        "in":   sorted(set(input_label_set)),
        "out":  sorted(set(output_label_set)),
        "props": sorted(set(output_property_keys)),
    }, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- read -----------------------------------------------------------------


@router.get("/v1/prompts", dependencies=[Depends(require_internal_token)])
def list_prompts(kind: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if kind:
        if kind not in _VALID_KINDS:
            raise HTTPException(400, f"invalid kind: {kind}")
        where.append("p.kind = $kind")
        params["kind"] = kind
    if status:
        if status not in _VALID_STATUSES:
            raise HTTPException(400, f"invalid status: {status}")
        where.append("p.status = $status")
        params["status"] = status
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    cypher = f"""
    MATCH (p:prompt)
    {clause}
    RETURN p
    ORDER BY p.created_at DESC
    LIMIT $limit
    """
    with session() as s:
        return [node_payload(rec["p"]) for rec in s.run(cypher, **params)]


@router.get("/v1/prompts/{prompt_id}", dependencies=[Depends(require_internal_token)])
def get_prompt(prompt_id: str) -> dict[str, Any]:
    with session() as s:
        rec = s.run("MATCH (p:prompt {prompt_id: $id}) RETURN p", id=prompt_id).single()
    if rec is None:
        raise HTTPException(404, "prompt not found")
    return node_payload(rec["p"])


@router.get("/v1/prompts/by-pattern/{pattern_key}", dependencies=[Depends(require_internal_token)])
def get_prompt_by_pattern(pattern_key: str, status: str = "active") -> dict[str, Any]:
    if status not in _VALID_STATUSES:
        raise HTTPException(400, f"invalid status: {status}")
    cypher = """
    MATCH (p:prompt {pattern_key: $pattern_key, status: $status})
    RETURN p ORDER BY p.version DESC LIMIT 1
    """
    with session() as s:
        rec = s.run(cypher, pattern_key=pattern_key, status=status).single()
    if rec is None:
        raise HTTPException(404, f"no {status} prompt for pattern {pattern_key!r}")
    return node_payload(rec["p"])


# ---- create / version-up --------------------------------------------------


class _CreatePromptIn(BaseModel):
    pattern_key: str
    kind: str                              # 'extraction' | 'synthesis'
    detector_prompt: str
    extractor_prompt: str
    output_schema: str                     # stringified JSON schema
    fingerprint: str
    examples: str = ""                     # stringified JSON
    notes: str = ""
    status: str = "shadow"


_CREATE_PROMPT = """
MATCH (a:agent {auth_user_id: $watcher_agent_id})
CREATE (p:prompt {
    prompt_id:        $prompt_id,
    pattern_key:      $pattern_key,
    version:          $version,
    kind:             $kind,
    status:           $status,
    fingerprint:      $fingerprint,
    detector_prompt:  $detector_prompt,
    extractor_prompt: $extractor_prompt,
    output_schema:    $output_schema,
    examples:         $examples,
    notes:            $notes,
    created_at:       datetime()
})
MERGE (p)-[:authored_by]->(a)
WITH p
OPTIONAL MATCH (prev:prompt {pattern_key: $pattern_key})
    WHERE prev.version = $version - 1
FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
    MERGE (p)-[:supersedes]->(prev)
)
RETURN p
"""


def _next_version(pattern_key: str) -> int:
    cypher = """
    MATCH (p:prompt {pattern_key: $pattern_key})
    RETURN coalesce(max(p.version), 0) + 1 AS next
    """
    with session() as s:
        rec = s.run(cypher, pattern_key=pattern_key).single()
    return int(rec["next"]) if rec else 1


@router.post("/v1/prompts")
def create_prompt(body: _CreatePromptIn, caller: Annotated[Caller, Depends(require_caller)]) -> dict[str, Any]:
    if body.kind not in _VALID_KINDS:
        raise HTTPException(400, f"invalid kind: {body.kind}")
    if body.status not in _VALID_STATUSES:
        raise HTTPException(400, f"invalid status: {body.status}")
    # The watcher agent should create prompts; record by kind for clarity.
    watcher_agent_id = (EXTRACTION_WATCHER_AGENT_ID if body.kind == "extraction"
                        else SYNTHESIS_WATCHER_AGENT_ID)
    prompt_id = str(uuid.uuid4())
    version = _next_version(body.pattern_key)
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            _CREATE_PROMPT,
            prompt_id=prompt_id,
            pattern_key=body.pattern_key,
            version=version,
            kind=body.kind,
            status=body.status,
            fingerprint=body.fingerprint,
            detector_prompt=body.detector_prompt,
            extractor_prompt=body.extractor_prompt,
            output_schema=body.output_schema,
            examples=body.examples,
            notes=body.notes,
            watcher_agent_id=watcher_agent_id,
        ).single())
    return node_payload(rec["p"])


class _PatchPromptIn(BaseModel):
    status: str | None = None


@router.patch("/v1/prompts/{prompt_id}", dependencies=[Depends(require_internal_token)])
def patch_prompt(prompt_id: str, body: _PatchPromptIn) -> dict[str, Any]:
    if body.status is None:
        raise HTTPException(400, "no patchable fields provided (only status is patchable)")
    if body.status not in _VALID_STATUSES:
        raise HTTPException(400, f"invalid status: {body.status}")
    cypher = """
    MATCH (p:prompt {prompt_id: $prompt_id})
    SET p.status = $status
    RETURN p
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher, prompt_id=prompt_id, status=body.status,
        ).single())
    if rec is None:
        raise HTTPException(404, "prompt not found")
    # When promoting a new version to active, retire previous active versions.
    if body.status == "active":
        retire = """
        MATCH (p:prompt {prompt_id: $prompt_id})
        MATCH (other:prompt {pattern_key: p.pattern_key, status: 'active'})
            WHERE other.prompt_id <> $prompt_id
        SET other.status = 'retired'
        """
        with session() as s:
            s.execute_write(lambda tx: tx.run(retire, prompt_id=prompt_id).consume())
    return node_payload(rec["p"])


@router.post("/v1/prompts/{prompt_id}/version-up")
def version_up(prompt_id: str, body: _CreatePromptIn,
               caller: Annotated[Caller, Depends(require_caller)]) -> dict[str, Any]:
    """Create a new version of an existing prompt (same pattern_key)."""
    with session() as s:
        rec = s.run(
            "MATCH (p:prompt {prompt_id: $id}) RETURN p.pattern_key AS k, p.kind AS kind",
            id=prompt_id,
        ).single()
    if rec is None:
        raise HTTPException(404, "prompt not found")
    body_dict = body.model_dump()
    body_dict["pattern_key"] = rec["k"]
    body_dict["kind"] = rec["kind"]
    return create_prompt(_CreatePromptIn(**body_dict), caller)
