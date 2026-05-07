"""Shared low-level graph writes used by extraction + synthesis + generic CRUD.

Centralized so the schema-validation layer is the only path to the graph.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from . import schema
from .graph import session
from .serializer import node_payload, relationship_payload


SOURCE_REF = "$source"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _serialize_props(properties: dict[str, Any]) -> dict[str, Any]:
    """Coerce values neo4j won't accept into ones it will."""
    out: dict[str, Any] = {}
    for k, v in (properties or {}).items():
        if v is None or isinstance(v, (bool, int, float, str)):
            out[k] = v
        elif isinstance(v, (list, tuple)):
            out[k] = [str(x) if not isinstance(x, (str, int, float, bool)) else x for x in v]
        elif isinstance(v, dict):
            out[k] = json.dumps(v, separators=(",", ":"), ensure_ascii=False, default=str)
        elif isinstance(v, datetime):
            out[k] = v.astimezone(timezone.utc).isoformat()
        else:
            out[k] = str(v)
    return out


def fetch_labels_for_ids(element_ids: Iterable[str]) -> dict[str, list[str]]:
    """Bulk lookup labels for existing nodes. Used to validate edges that
    reference an existing node by element_id."""
    ids = sorted({str(eid) for eid in element_ids if eid})
    if not ids:
        return {}
    cypher = """
    MATCH (n) WHERE elementId(n) IN $ids
    RETURN elementId(n) AS id, labels(n) AS labels
    """
    out: dict[str, list[str]] = {}
    with session() as s:
        for rec in s.run(cypher, ids=ids):
            out[rec["id"]] = list(rec["labels"])
    return out


def fetch_node(element_id: str) -> dict[str, Any] | None:
    cypher = "MATCH (n) WHERE elementId(n) = $id RETURN n"
    with session() as s:
        rec = s.run(cypher, id=element_id).single()
    return node_payload(rec["n"]) if rec else None


# ---------------------------------------------------------------------------
# Tx-scoped helpers. Every write goes through one of these so the labeling
# and authorship invariants are enforced in one place.
# ---------------------------------------------------------------------------

def _tx_create_node(tx, *, labels: list[str], properties: dict[str, Any],
                    author_identity_element_id: str | None) -> Any:
    labels_str = ":".join(sorted({str(l).strip() for l in labels if str(l).strip()}))
    if not labels_str:
        raise ValueError("node create needs at least one label")
    cypher = f"""
    CREATE (n:{labels_str})
    SET n = $props
    WITH n
    OPTIONAL MATCH (i:identity) WHERE elementId(i) = $author_id AND $author_id IS NOT NULL
    FOREACH (_ IN CASE WHEN i IS NOT NULL THEN [1] ELSE [] END |
        MERGE (n)-[:author]->(i)
    )
    RETURN n
    """
    rec = tx.run(cypher, props=_serialize_props(properties), author_id=author_identity_element_id).single()
    return rec["n"]


def _tx_create_edge(tx, *, src_id: str, dst_id: str, type_: str,
                    properties: dict[str, Any], edge_id: str) -> tuple[Any, Any]:
    """Create a real relationship + its :edge_record shadow node. Returns
    (relationship_id, edge_record_node)."""
    props = _serialize_props(dict(properties or {}))
    props["edge_id"] = edge_id
    # Cypher relationship type interpolation (whitelisted by schema validator
    # at the API layer; this function is internal).
    cypher = f"""
    MATCH (s) WHERE elementId(s) = $src_id
    MATCH (d) WHERE elementId(d) = $dst_id
    CREATE (s)-[r:{type_}]->(d)
    SET r = $props
    CREATE (er:edge_record {{
        edge_id: $edge_id,
        type: $type_str,
        src_node_id: $src_id,
        dst_node_id: $dst_id,
        properties_json: $props_json,
        created_at: datetime()
    }})
    RETURN r, er
    """
    rec = tx.run(
        cypher,
        src_id=src_id, dst_id=dst_id,
        props=props,
        edge_id=edge_id,
        type_str=type_,
        props_json=json.dumps({k: v for k, v in props.items() if k != "edge_id"},
                              separators=(",", ":"), ensure_ascii=False, default=str),
    ).single()
    return rec["r"], rec["er"]


def _tx_create_extraction_event(tx, *, extraction_id: str, agent_auth_user_id: int,
                                source_id: str, prompt_id: str | None,
                                model: str | None, meta: dict[str, Any],
                                output_node_ids: list[str], output_edge_record_ids: list[str]) -> Any:
    cypher = """
    MATCH (a:agent {auth_user_id: $agent_auth_user_id})
    MATCH (src) WHERE elementId(src) = $source_id
    CREATE (e:extraction {
        extraction_id: $extraction_id,
        started_at:    datetime($now),
        finished_at:   datetime(),
        output_node_count: $node_count,
        output_edge_count: $edge_count,
        prompt_id:     $prompt_id,
        model:         $model,
        meta:          $meta_json
    })
    MERGE (e)-[:by]->(a)
    MERGE (e)-[:from]->(src)
    WITH e, src
    OPTIONAL MATCH (p:prompt {prompt_id: $prompt_id}) WHERE $prompt_id IS NOT NULL
    FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
        MERGE (e)-[:used_prompt]->(p)
    )
    WITH e, src
    UNWIND $output_node_ids AS oid
    MATCH (o) WHERE elementId(o) = oid
    MERGE (e)-[:produced]->(o)
    MERGE (o)-[:extracted_from]->(src)
    WITH e, src
    UNWIND $output_edge_record_ids AS erid
    MATCH (er:edge_record {edge_id: erid})
    MERGE (e)-[:produced]->(er)
    RETURN e
    """
    rec = tx.run(
        cypher,
        extraction_id=extraction_id,
        agent_auth_user_id=agent_auth_user_id,
        source_id=source_id,
        prompt_id=prompt_id,
        model=model,
        meta_json=json.dumps(meta or {}, separators=(",", ":"), ensure_ascii=False, default=str),
        node_count=len(output_node_ids),
        edge_count=len(output_edge_record_ids),
        output_node_ids=output_node_ids,
        output_edge_record_ids=output_edge_record_ids,
        now=_now_iso(),
    ).single()
    return rec["e"]


def _tx_create_synthesis_event(tx, *, synthesis_id: str, agent_auth_user_id: int,
                               source_node_ids: list[str], source_edge_ids: list[str],
                               prompt_id: str | None, model: str | None,
                               meta: dict[str, Any],
                               output_node_ids: list[str], output_edge_record_ids: list[str]) -> Any:
    cypher = """
    MATCH (a:agent {auth_user_id: $agent_auth_user_id})
    CREATE (sy:synthesis {
        synthesis_id: $synthesis_id,
        started_at:   datetime($now),
        finished_at:  datetime(),
        output_node_count: $node_count,
        output_edge_count: $edge_count,
        prompt_id:    $prompt_id,
        model:        $model,
        meta:         $meta_json
    })
    MERGE (sy)-[:by]->(a)
    WITH sy
    UNWIND $source_node_ids AS sid
    MATCH (s) WHERE elementId(s) = sid
    MERGE (sy)-[:from]->(s)
    WITH sy
    UNWIND $source_edge_ids AS eid
    MATCH (er:edge_record {edge_id: eid})
    MERGE (sy)-[:from]->(er)
    WITH sy
    OPTIONAL MATCH (p:prompt {prompt_id: $prompt_id}) WHERE $prompt_id IS NOT NULL
    FOREACH (_ IN CASE WHEN p IS NOT NULL THEN [1] ELSE [] END |
        MERGE (sy)-[:used_prompt]->(p)
    )
    WITH sy
    UNWIND $output_node_ids AS oid
    MATCH (o) WHERE elementId(o) = oid
    MERGE (sy)-[:produced]->(o)
    WITH sy
    UNWIND $source_node_ids + $source_edge_ids AS srcid
    OPTIONAL MATCH (sn) WHERE elementId(sn) = srcid
    OPTIONAL MATCH (er2:edge_record {edge_id: srcid})
    WITH sy, coalesce(sn, er2) AS src_target
    UNWIND $output_node_ids AS oid2
    MATCH (o2) WHERE elementId(o2) = oid2
    MERGE (o2)-[:synthesized_from]->(src_target)
    WITH sy
    UNWIND $output_edge_record_ids AS erid
    MATCH (er3:edge_record {edge_id: erid})
    MERGE (sy)-[:produced]->(er3)
    RETURN sy
    """
    rec = tx.run(
        cypher,
        synthesis_id=synthesis_id,
        agent_auth_user_id=agent_auth_user_id,
        source_node_ids=source_node_ids,
        source_edge_ids=source_edge_ids,
        prompt_id=prompt_id,
        model=model,
        meta_json=json.dumps(meta or {}, separators=(",", ":"), ensure_ascii=False, default=str),
        node_count=len(output_node_ids),
        edge_count=len(output_edge_record_ids),
        output_node_ids=output_node_ids,
        output_edge_record_ids=output_edge_record_ids,
        now=_now_iso(),
    ).single()
    return rec["sy"]


# ---------------------------------------------------------------------------
# Top-level write helpers
# ---------------------------------------------------------------------------

def create_node(*, labels: list[str], properties: dict[str, Any],
                author_identity_element_id: str | None) -> dict[str, Any]:
    schema.validate_node_create(labels, properties)
    with session() as s:
        node = s.execute_write(lambda tx: _tx_create_node(
            tx, labels=labels, properties=properties,
            author_identity_element_id=author_identity_element_id,
        ))
    return node_payload(node)


def patch_node(*, element_id: str, set_props: dict[str, Any], unset_props: list[str]) -> dict[str, Any] | None:
    cypher_fetch = "MATCH (n) WHERE elementId(n) = $id RETURN labels(n) AS labels"
    with session() as s:
        rec = s.run(cypher_fetch, id=element_id).single()
        if rec is None:
            return None
        schema.validate_node_patch(rec["labels"], set_props, unset_props)
        sets = ", ".join(f"n.{k} = ${'p_' + k}" for k in set_props)
        unsets = ", ".join(f"n.{k} = null" for k in unset_props)
        clauses = [c for c in [sets, unsets] if c]
        if not clauses:
            return node_payload(s.run("MATCH (n) WHERE elementId(n) = $id RETURN n", id=element_id).single()["n"])
        cypher = f"""
        MATCH (n) WHERE elementId(n) = $id
        SET {', '.join(clauses)}
        RETURN n
        """
        params = {"id": element_id, **{f"p_{k}": v for k, v in _serialize_props(set_props).items()}}
        rec = s.execute_write(lambda tx: tx.run(cypher, **params).single())
    return node_payload(rec["n"]) if rec else None


def create_edge_standalone(*, src_id: str, dst_id: str, type_: str,
                           properties: dict[str, Any]) -> dict[str, Any]:
    """Create a single edge (and its :edge_record). For use by POST /v1/edges
    outside of an extraction/synthesis."""
    labels = fetch_labels_for_ids([src_id, dst_id])
    if src_id not in labels:
        raise ValueError(f"source node not found: {src_id}")
    if dst_id not in labels:
        raise ValueError(f"dest node not found: {dst_id}")
    schema.validate_edge_create(type_, labels[src_id], labels[dst_id])
    edge_id = str(uuid.uuid4())
    with session() as s:
        rel, er = s.execute_write(lambda tx: _tx_create_edge(
            tx, src_id=src_id, dst_id=dst_id, type_=type_,
            properties=properties, edge_id=edge_id,
        ))
    return {
        "edge_id": edge_id,
        "relationship": relationship_payload(rel),
        "edge_record": node_payload(er),
    }
