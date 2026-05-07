"""Watcher: induce extraction/synthesis prompts from observed events.

Polling loop. Every WEAVE_WATCHER_INTERVAL seconds, fetch a batch of
:extraction (or :synthesis) events the watcher hasn't processed yet,
infer the pattern, and either keep / version-up / create a :prompt.

Design: Neo4j is the queue. The watcher's :processed edge is the
idempotency mark. When two watcher replicas run, both succeed at MERGE
but only one writes a new prompt — the (pattern_key, version) UNIQUE
constraint resolves the race; the loser retries with version+1 next tick
(harmless duplication is acceptable for prompt induction).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from openai import OpenAIError

from . import config, llm
from .config import settings, EXTRACTION_WATCHER_AGENT_ID, SYNTHESIS_WATCHER_AGENT_ID
from .graph import session
from .routes_prompts import fingerprint_for
from .serializer import node_payload


log = logging.getLogger(__name__)


# In-memory pattern_key -> last-seen-monotonic to apply the cooldown without
# touching the DB. Watcher is a singleton, so this is fine.
_PATTERN_LAST_SEEN: dict[str, float] = {}


# ---- shared --------------------------------------------------------------


def _is_in_cooldown(pattern_key: str) -> bool:
    cfg = settings()
    last = _PATTERN_LAST_SEEN.get(pattern_key)
    if last is None:
        return False
    return (time.monotonic() - last) < cfg.watcher_pattern_cooldown_sec


def _mark_seen(pattern_key: str) -> None:
    _PATTERN_LAST_SEEN[pattern_key] = time.monotonic()


def _label_set(node_payloads: list[dict[str, Any]]) -> list[str]:
    out: set[str] = set()
    for p in node_payloads:
        for l in (p.get("labels") or []):
            out.add(str(l))
    return sorted(out)


def _property_keys(node_payloads: list[dict[str, Any]]) -> list[str]:
    out: set[str] = set()
    for p in node_payloads:
        for k in p.keys():
            if k in {"id", "labels"}:
                continue
            out.add(str(k))
    return sorted(out)


def _pattern_key(input_labels: list[str], output_labels: list[str]) -> str:
    """Slugify input.label.set -> output.label.set."""
    a = ".".join(input_labels) or "unknown"
    b = ".".join(output_labels) or "empty"
    return f"{a}->{b}"


# ---- extraction watcher -------------------------------------------------


_UNPROCESSED_EXTRACTION = """
MATCH (e:extraction)
WHERE NOT EXISTS {
    MATCH (:agent {auth_user_id: $watcher_id})-[:processed]->(e)
}
WITH e
ORDER BY e.finished_at ASC
LIMIT $limit
OPTIONAL MATCH (e)-[:from]->(src)
OPTIONAL MATCH (e)-[:produced]->(out)
WHERE NOT (out:edge_record)
WITH e, src, collect(DISTINCT out) AS outs
RETURN e, src, outs
"""


_MARK_PROCESSED = """
MATCH (e) WHERE elementId(e) = $eid
MATCH (a:agent {auth_user_id: $watcher_id})
MERGE (a)-[r:processed]->(e)
  ON CREATE SET r.at = datetime()
"""


def _existing_active_prompt(pattern_key: str, kind: str) -> dict[str, Any] | None:
    cypher = """
    MATCH (p:prompt {pattern_key: $pattern_key, kind: $kind, status: 'active'})
    RETURN p ORDER BY p.version DESC LIMIT 1
    """
    with session() as s:
        rec = s.run(cypher, pattern_key=pattern_key, kind=kind).single()
    return node_payload(rec["p"]) if rec else None


def _by_fingerprint(fingerprint: str, kind: str) -> dict[str, Any] | None:
    cypher = """
    MATCH (p:prompt {fingerprint: $fp, kind: $kind, status: 'active'})
    RETURN p LIMIT 1
    """
    with session() as s:
        rec = s.run(cypher, fp=fingerprint, kind=kind).single()
    return node_payload(rec["p"]) if rec else None


def _next_version(pattern_key: str) -> int:
    cypher = """
    MATCH (p:prompt {pattern_key: $pattern_key})
    RETURN coalesce(max(p.version), 0) + 1 AS next
    """
    with session() as s:
        rec = s.run(cypher, pattern_key=pattern_key).single()
    return int(rec["next"]) if rec else 1


def _create_prompt_node(*, pattern_key: str, kind: str, fingerprint: str,
                        detector: str, extractor: str, output_schema: str,
                        notes: str, watcher_id: int) -> str:
    version = _next_version(pattern_key)
    cypher = """
    MATCH (a:agent {auth_user_id: $watcher_id})
    CREATE (p:prompt {
        prompt_id:        randomUUID(),
        pattern_key:      $pattern_key,
        version:          $version,
        kind:             $kind,
        status:           'shadow',
        fingerprint:      $fingerprint,
        detector_prompt:  $detector,
        extractor_prompt: $extractor,
        output_schema:    $output_schema,
        examples:         '',
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
    RETURN p.prompt_id AS prompt_id
    """
    with session() as s:
        rec = s.execute_write(lambda tx: tx.run(
            cypher,
            pattern_key=pattern_key, version=version, kind=kind,
            fingerprint=fingerprint, detector=detector, extractor=extractor,
            output_schema=output_schema, notes=notes, watcher_id=watcher_id,
        ).single())
    return rec["prompt_id"]


def _process_one_extraction(rec: Any, *, watcher_id: int) -> dict[str, Any]:
    e_node = rec["e"]
    src = rec["src"]
    outs = rec["outs"] or []
    if src is None or not outs:
        return {"reason": "incomplete-event"}
    src_p = node_payload(src)
    out_ps = [node_payload(o) for o in outs if o is not None]

    in_labels  = sorted({str(l) for l in (src_p.get("labels") or [])})
    out_labels = _label_set(out_ps)
    out_props  = _property_keys(out_ps)
    pattern_key = _pattern_key(in_labels, out_labels)

    if _is_in_cooldown(pattern_key):
        return {"reason": "cooldown", "pattern_key": pattern_key}

    fp = fingerprint_for(
        kind="extraction",
        input_label_set=in_labels,
        output_label_set=out_labels,
        output_property_keys=out_props,
    )

    existing = _existing_active_prompt(pattern_key, kind="extraction")
    source_repr = json.dumps({"labels": src_p.get("labels"), "properties": {k: v for k, v in src_p.items() if k not in {"id", "labels"}}}, default=str)[:4000]
    output_repr = json.dumps([{"labels": p.get("labels"), "properties": {k: v for k, v in p.items() if k not in {"id", "labels"}}} for p in out_ps], default=str)[:6000]

    cfg = settings()
    if not cfg.openai_api_key:
        return {"reason": "no-openai-key", "pattern_key": pattern_key}

    try:
        if existing:
            decision = llm.decide_keep_or_update(
                existing_detector=existing["detector_prompt"],
                existing_extractor=existing["extractor_prompt"],
                source_repr=source_repr,
                output_repr=output_repr,
            )
            if decision.get("decision") == "update":
                detector = decision.get("revised_detector_prompt") or existing["detector_prompt"]
                extractor = decision.get("revised_extractor_prompt") or existing["extractor_prompt"]
                _create_prompt_node(
                    pattern_key=pattern_key, kind="extraction", fingerprint=fp,
                    detector=detector, extractor=extractor,
                    output_schema=existing.get("output_schema") or "{}",
                    notes=decision.get("rationale") or "watcher: revised",
                    watcher_id=watcher_id,
                )
                _mark_seen(pattern_key)
                return {"reason": "updated", "pattern_key": pattern_key}
            _mark_seen(pattern_key)
            return {"reason": "kept", "pattern_key": pattern_key}
        # No active prompt for this pattern_key — try fingerprint match.
        if (alias := _by_fingerprint(fp, kind="extraction")):
            _mark_seen(pattern_key)
            return {"reason": "alias", "pattern_key": pattern_key, "via": alias["prompt_id"]}
        induced = llm.induce_pattern(source_repr=source_repr, output_repr=output_repr)
        prompt_id = _create_prompt_node(
            pattern_key=pattern_key, kind="extraction", fingerprint=fp,
            detector=induced["detector_prompt"],
            extractor=induced["extractor_prompt"],
            output_schema=induced["output_schema_json"],
            notes=induced.get("rationale") or "",
            watcher_id=watcher_id,
        )
        _mark_seen(pattern_key)
        return {"reason": "new", "pattern_key": pattern_key, "prompt_id": prompt_id}
    except OpenAIError as oe:
        return {"reason": "openai-error", "error": str(oe), "pattern_key": pattern_key}


def tick_extraction() -> dict[str, Any]:
    cfg = settings()
    counters = {"events_seen": 0, "new": 0, "updated": 0, "kept": 0, "alias": 0, "errors": 0, "cooldown": 0, "no_key": 0}
    with session() as s:
        rows = list(s.run(
            _UNPROCESSED_EXTRACTION,
            watcher_id=EXTRACTION_WATCHER_AGENT_ID,
            limit=cfg.watcher_batch,
        ))
    for rec in rows:
        counters["events_seen"] += 1
        result = _process_one_extraction(rec, watcher_id=EXTRACTION_WATCHER_AGENT_ID)
        reason = result.get("reason", "")
        if reason == "new":
            counters["new"] += 1
        elif reason == "updated":
            counters["updated"] += 1
        elif reason == "kept":
            counters["kept"] += 1
        elif reason == "alias":
            counters["alias"] += 1
        elif reason == "cooldown":
            counters["cooldown"] += 1
        elif reason in {"no-openai-key"}:
            counters["no_key"] += 1
        elif reason == "openai-error":
            counters["errors"] += 1
        # Always mark processed — even if we hit a transient OpenAI error,
        # we don't want this event to monopolize ticks. The next extraction
        # of the same pattern will give us another chance.
        with session() as s:
            s.execute_write(lambda tx: tx.run(
                _MARK_PROCESSED,
                eid=rec["e"].element_id,
                watcher_id=EXTRACTION_WATCHER_AGENT_ID,
            ).consume())
    return counters


# ---- synthesis watcher (analogous, narrower) -----------------------------


_UNPROCESSED_SYNTHESIS = """
MATCH (sy:synthesis)
WHERE NOT EXISTS {
    MATCH (:agent {auth_user_id: $watcher_id})-[:processed]->(sy)
}
WITH sy
ORDER BY sy.finished_at ASC
LIMIT $limit
OPTIONAL MATCH (sy)-[:from]->(src)
OPTIONAL MATCH (sy)-[:produced]->(out) WHERE NOT (out:edge_record)
WITH sy, collect(DISTINCT src) AS srcs, collect(DISTINCT out) AS outs
RETURN sy, srcs, outs
"""


def tick_synthesis() -> dict[str, Any]:
    cfg = settings()
    counters = {"events_seen": 0, "new": 0, "updated": 0, "kept": 0, "alias": 0, "errors": 0, "cooldown": 0, "no_key": 0}
    with session() as s:
        rows = list(s.run(
            _UNPROCESSED_SYNTHESIS,
            watcher_id=SYNTHESIS_WATCHER_AGENT_ID,
            limit=cfg.watcher_batch,
        ))
    for rec in rows:
        counters["events_seen"] += 1
        srcs = [node_payload(x) for x in (rec["srcs"] or []) if x is not None]
        outs = [node_payload(x) for x in (rec["outs"] or []) if x is not None]
        if not srcs or not outs:
            continue
        in_labels  = _label_set(srcs)
        out_labels = _label_set(outs)
        out_props  = _property_keys(outs)
        pattern_key = _pattern_key(in_labels, out_labels)
        if _is_in_cooldown(pattern_key):
            counters["cooldown"] += 1
            continue
        if not cfg.openai_api_key:
            counters["no_key"] += 1
            continue
        fp = fingerprint_for(
            kind="synthesis",
            input_label_set=in_labels,
            output_label_set=out_labels,
            output_property_keys=out_props,
        )
        existing = _existing_active_prompt(pattern_key, kind="synthesis")
        source_repr = json.dumps([{"labels": p.get("labels")} for p in srcs])[:4000]
        output_repr = json.dumps([{"labels": p.get("labels"), "properties": {k: v for k, v in p.items() if k not in {"id", "labels"}}} for p in outs], default=str)[:6000]
        try:
            if existing:
                decision = llm.decide_keep_or_update(
                    existing_detector=existing["detector_prompt"],
                    existing_extractor=existing["extractor_prompt"],
                    source_repr=source_repr,
                    output_repr=output_repr,
                )
                if decision.get("decision") == "update":
                    detector = decision.get("revised_detector_prompt") or existing["detector_prompt"]
                    extractor = decision.get("revised_extractor_prompt") or existing["extractor_prompt"]
                    _create_prompt_node(
                        pattern_key=pattern_key, kind="synthesis", fingerprint=fp,
                        detector=detector, extractor=extractor,
                        output_schema=existing.get("output_schema") or "{}",
                        notes=decision.get("rationale") or "",
                        watcher_id=SYNTHESIS_WATCHER_AGENT_ID,
                    )
                    counters["updated"] += 1
                else:
                    counters["kept"] += 1
            else:
                induced = llm.induce_pattern(source_repr=source_repr, output_repr=output_repr)
                _create_prompt_node(
                    pattern_key=pattern_key, kind="synthesis", fingerprint=fp,
                    detector=induced["detector_prompt"],
                    extractor=induced["extractor_prompt"],
                    output_schema=induced["output_schema_json"],
                    notes=induced.get("rationale") or "",
                    watcher_id=SYNTHESIS_WATCHER_AGENT_ID,
                )
                counters["new"] += 1
            _mark_seen(pattern_key)
        except OpenAIError:
            counters["errors"] += 1
        finally:
            with session() as s:
                s.execute_write(lambda tx: tx.run(
                    _MARK_PROCESSED,
                    eid=rec["sy"].element_id,
                    watcher_id=SYNTHESIS_WATCHER_AGENT_ID,
                ).consume())
    return counters


# ---- async runners ------------------------------------------------------


async def run_extraction() -> None:
    cfg = settings()
    log.info("extraction-watcher starting (interval=%.1fs, batch=%d)",
             cfg.watcher_interval, cfg.watcher_batch)
    while True:
        try:
            tick_extraction()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("extraction-watcher tick failed")
        try:
            await asyncio.sleep(cfg.watcher_interval)
        except asyncio.CancelledError:
            log.info("extraction-watcher cancelled")
            return


async def run_synthesis() -> None:
    cfg = settings()
    log.info("synthesis-watcher starting (interval=%.1fs, batch=%d)",
             cfg.watcher_interval, cfg.watcher_batch)
    while True:
        try:
            tick_synthesis()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("synthesis-watcher tick failed")
        try:
            await asyncio.sleep(cfg.watcher_interval)
        except asyncio.CancelledError:
            log.info("synthesis-watcher cancelled")
            return
