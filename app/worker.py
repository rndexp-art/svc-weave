"""Worker: apply active prompts to candidate inputs.

Polling loop. Every WEAVE_WORKER_INTERVAL seconds:
  1. List `:prompt {kind:'extraction', status:'active'}` nodes.
  2. For each prompt, find candidate inputs the worker has been granted
     access to (via `:source -[:readable]-> worker_agent`) that haven't
     been processed by THIS prompt yet.
  3. Run detector → extractor; on hit, write a new :extraction via the
     in-process route function.
  4. Mark the (prompt -> input) :processed edge regardless of result.

The synthesis worker is gated behind WEAVE_SYNTHESIS_WORKER_ENABLED;
synthesis input-clustering is open-ended so we ship empty until a
concrete pattern lands.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from openai import OpenAIError

from . import llm
from .config import (
    settings,
    EXTRACTION_WORKER_AGENT_ID,
    SYNTHESIS_WORKER_AGENT_ID,
)
from .graph import session
from .serializer import node_payload


log = logging.getLogger(__name__)


_ACTIVE_EXTRACTION_PROMPTS = """
MATCH (p:prompt {kind: 'extraction', status: 'active'})
RETURN p
ORDER BY p.created_at ASC
"""


# Candidates: input nodes attached to a source the worker can read, not
# yet processed by this prompt. Pattern_key shape "<input.labels>-> ..."
# tells us the source labels — for v1 we hardcode :input matching the
# pattern_key prefix and let the schema layer reject mismatches.
_PROMPT_CANDIDATES = """
MATCH (p:prompt {prompt_id: $prompt_id})
MATCH (n:input)-[:source]->(s:source)-[:readable]->(:agent {auth_user_id: $worker_id})
WHERE NOT EXISTS {
    MATCH (p)-[:processed]->(n)
}
RETURN n
LIMIT $limit
"""


_MARK_PROMPT_PROCESSED = """
MATCH (p:prompt {prompt_id: $prompt_id})
MATCH (n) WHERE elementId(n) = $eid
MERGE (p)-[r:processed]->(n)
  ON CREATE SET r.at = datetime(),
                r.result = $result
"""


def _node_text(payload: dict[str, Any]) -> str:
    """Best-effort textual representation for the LLM. Prefer common
    content fields; fall back to JSON-dumping the properties."""
    for k in ("content", "text", "body", "title"):
        v = payload.get(k)
        if isinstance(v, str) and v.strip():
            return v
    props = {k: v for k, v in payload.items() if k not in {"id", "labels"}}
    return json.dumps(props, default=str)[:8000]


def _run_extraction_via_route(*, source_node_id: str, prompt_id: str,
                              parsed_output: dict[str, Any], model: str) -> dict[str, Any]:
    """Call the extraction route function directly, in-process. We pull the
    function lazily to avoid a circular import at module load."""
    from .routes_extractions import (
        ExtractionIn, _NodeIn, _EdgeIn, create_extraction,
    )
    from .auth import Caller
    nodes_raw = parsed_output.get("nodes") or []
    edges_raw = parsed_output.get("edges") or []
    nodes = [_NodeIn(
        labels=list(n.get("labels") or []),
        properties=dict(n.get("properties") or {}),
        ref=str(n.get("ref") or f"n{i}"),
    ) for i, n in enumerate(nodes_raw)]
    edges = [_EdgeIn(
        type=str(e.get("type") or ""),
        src_ref=str(e.get("src_ref") or "$source"),
        dst_ref=str(e.get("dst_ref") or ""),
        properties=dict(e.get("properties") or {}),
        edge_ref=str(e.get("edge_ref") or f"e{i}"),
    ) for i, e in enumerate(edges_raw)]
    body = ExtractionIn(
        source_node_id=source_node_id,
        nodes=nodes,
        edges=edges,
        prompt_id=prompt_id,
        model=model,
        meta={"source": "extraction-worker"},
    )
    caller = Caller(
        kind="service",
        auth_user_id=EXTRACTION_WORKER_AGENT_ID,
        provider_name="extraction-worker",
        raw="service:extraction-worker",
    )
    out = create_extraction(body, caller)
    return out.model_dump()


def tick_extraction() -> dict[str, Any]:
    cfg = settings()
    counters = {"prompts": 0, "candidates": 0, "hits": 0, "misses": 0, "errors": 0, "no_key": 0}
    if not cfg.openai_api_key:
        counters["no_key"] = 1
        return counters
    with session() as s:
        prompts = [node_payload(rec["p"]) for rec in s.run(_ACTIVE_EXTRACTION_PROMPTS)]
    counters["prompts"] = len(prompts)
    if not prompts:
        return counters
    for prompt in prompts:
        prompt_id = prompt["prompt_id"]
        try:
            output_schema = json.loads(prompt.get("output_schema") or "{}")
        except json.JSONDecodeError:
            log.warning("prompt %s has malformed output_schema; skipping", prompt_id)
            continue
        # Wrap into the strict-JSON envelope the LLM helpers want.
        envelope_schema = output_schema if isinstance(output_schema, dict) and "schema" in output_schema else {
            "name": "extraction_output",
            "strict": True,
            "schema": output_schema if isinstance(output_schema, dict) else {"type": "object"},
        }
        with session() as s:
            candidates = [node_payload(rec["n"]) for rec in s.run(
                _PROMPT_CANDIDATES,
                prompt_id=prompt_id,
                worker_id=EXTRACTION_WORKER_AGENT_ID,
                limit=cfg.worker_batch,
            )]
        counters["candidates"] += len(candidates)
        for cand in candidates:
            text = _node_text(cand)
            element_id = cand["id"]
            try:
                present = llm.detect(prompt=prompt["detector_prompt"], content=text)
            except OpenAIError as e:
                log.warning("detector failed on %s: %s", element_id, e)
                counters["errors"] += 1
                _mark(prompt_id, element_id, "error")
                continue
            if not present:
                counters["misses"] += 1
                _mark(prompt_id, element_id, "miss")
                continue
            try:
                parsed = llm.extract(
                    prompt=prompt["extractor_prompt"],
                    output_schema=envelope_schema,
                    content=text,
                )
            except OpenAIError as e:
                log.warning("extractor failed on %s: %s", element_id, e)
                counters["errors"] += 1
                _mark(prompt_id, element_id, "error")
                continue
            try:
                _run_extraction_via_route(
                    source_node_id=element_id,
                    prompt_id=prompt_id,
                    parsed_output=parsed,
                    model=cfg.openai_model,
                )
                counters["hits"] += 1
                _mark(prompt_id, element_id, "hit")
            except Exception as e:
                log.exception("extraction-write failed on %s: %s", element_id, e)
                counters["errors"] += 1
                _mark(prompt_id, element_id, "error")
    return counters


def _mark(prompt_id: str, eid: str, result: str) -> None:
    with session() as s:
        s.execute_write(lambda tx: tx.run(
            _MARK_PROMPT_PROCESSED, prompt_id=prompt_id, eid=eid, result=result,
        ).consume())


def tick_synthesis() -> dict[str, Any]:
    cfg = settings()
    if not cfg.synthesis_worker_enabled:
        return {"enabled": False}
    # Synthesis input-clustering is open-ended; ship empty until a concrete
    # candidate-discovery pattern lands. The watcher still records prompts;
    # this tick is the placeholder so the lifecycle wiring works.
    return {"enabled": True, "candidates": 0}


async def run_extraction() -> None:
    cfg = settings()
    log.info("extraction-worker starting (interval=%.1fs, batch=%d)",
             cfg.worker_interval, cfg.worker_batch)
    while True:
        try:
            tick_extraction()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("extraction-worker tick failed")
        try:
            await asyncio.sleep(cfg.worker_interval)
        except asyncio.CancelledError:
            log.info("extraction-worker cancelled")
            return


async def run_synthesis() -> None:
    cfg = settings()
    log.info("synthesis-worker starting (interval=%.1fs, batch=%d, enabled=%s)",
             cfg.worker_interval, cfg.worker_batch, cfg.synthesis_worker_enabled)
    while True:
        try:
            tick_synthesis()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("synthesis-worker tick failed")
        try:
            await asyncio.sleep(cfg.worker_interval)
        except asyncio.CancelledError:
            log.info("synthesis-worker cancelled")
            return
