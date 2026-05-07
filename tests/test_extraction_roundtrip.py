"""End-to-end test: POST /v1/extractions writes the event + outputs.

Marked `e2e` — needs neo4j running. Skips automatically when env vars
aren't set.

Run from inside the weave container, OR set NEO4J_PASSWORD +
WEAVE_INTERNAL_TOKEN and point NEO4J_URI at a running neo4j:

    NEO4J_URI=bolt://localhost:7687 \\
    NEO4J_PASSWORD=changeme \\
    WEAVE_INTERNAL_TOKEN=test \\
    pytest -q -m e2e tests/test_extraction_roundtrip.py
"""
from __future__ import annotations

import os
import uuid

import pytest


pytestmark = pytest.mark.e2e


def _have_neo4j() -> bool:
    return bool(os.environ.get("NEO4J_PASSWORD") and os.environ.get("WEAVE_INTERNAL_TOKEN"))


@pytest.fixture(scope="module")
def client():
    if not _have_neo4j():
        pytest.skip("NEO4J_PASSWORD / WEAVE_INTERNAL_TOKEN not set")
    from fastapi.testclient import TestClient
    from app import bootstrap, graph
    from app.main import app

    graph.init()
    graph.ensure_schema()
    bootstrap.bootstrap_service_agents()
    with TestClient(app) as c:
        yield c
    graph.close()


def _headers():
    return {
        "X-Internal-Token": os.environ["WEAVE_INTERNAL_TOKEN"],
        "X-Weave-Caller":   "service:weave-gateway",
    }


def test_extraction_roundtrip(client):
    # Create a source note via direct generic-node POST so we don't need
    # external state. Use a UUID for external_id to avoid collisions across
    # test runs.
    ext = str(uuid.uuid4())
    resp = client.post("/v1/nodes", headers=_headers(), json={
        "labels": ["input", "note"],
        "properties": {
            "note_id": ext,
            "provider_name": "weave",
            "external_chat_id": "test",
            "external_id": ext,
            "content": "Buy milk",
            "created_at": "2026-05-07T00:00:00Z",
        },
    })
    assert resp.status_code == 200, resp.text
    src = resp.json()
    src_id = src["id"]

    # Extract: produce one :input:task with a content field, plus a chained
    # edge from a new note to $source.
    out_id = str(uuid.uuid4())
    resp = client.post("/v1/extractions", headers=_headers(), json={
        "source_node_id": src_id,
        "nodes": [
            {
                "labels": ["input", "task"],
                "properties": {
                    "task_id": out_id,
                    "provider_name": "weave",
                    "external_chat_id": "test",
                    "external_id": out_id,
                    "title": "Buy milk",
                    "description": "from note",
                    "status": "todo",
                    "created_at": "2026-05-07T00:00:00Z",
                    "updated_at": "2026-05-07T00:00:00Z",
                },
                "ref": "n0",
            }
        ],
        "edges": [],
        "meta": {"test": True},
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"][0]["element_id"]
    extraction_id = body["extraction_id"]

    # Verify: extraction event exists, has :produced -> task and :from -> note.
    from app.graph import session
    with session() as s:
        rec = s.run(
            "MATCH (e:extraction {extraction_id: $id})-[:produced]->(t:input:task) "
            "MATCH (e)-[:from]->(src:input:note) "
            "RETURN t.task_id AS task_id, src.note_id AS note_id",
            id=extraction_id,
        ).single()
    assert rec is not None
    assert rec["task_id"] == out_id
    assert rec["note_id"] == ext

    # Legacy :extracted_from edge also present.
    with session() as s:
        rec = s.run(
            "MATCH (t:input:task {task_id: $id})-[:extracted_from]->(:input:note) "
            "RETURN count(*) AS c",
            id=out_id,
        ).single()
    assert rec["c"] >= 1
