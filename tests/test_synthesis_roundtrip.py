"""End-to-end synthesis test. See test_extraction_roundtrip.py for setup."""
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


def _new_note(client, content: str) -> str:
    ext = str(uuid.uuid4())
    resp = client.post("/v1/nodes", headers=_headers(), json={
        "labels": ["input", "note"],
        "properties": {
            "note_id": ext,
            "provider_name": "weave",
            "external_chat_id": "test",
            "external_id": ext,
            "content": content,
            "created_at": "2026-05-07T00:00:00Z",
        },
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def test_synthesis_with_two_inputs(client):
    a = _new_note(client, "Note A: project kickoff Monday")
    b = _new_note(client, "Note B: budget approved")

    out_id = str(uuid.uuid4())
    resp = client.post("/v1/syntheses", headers=_headers(), json={
        "source_node_ids": [a, b],
        "source_edge_ids": [],
        "nodes": [
            {
                "labels": ["input", "task"],
                "properties": {
                    "task_id": out_id,
                    "provider_name": "weave",
                    "external_chat_id": "test",
                    "external_id": out_id,
                    "title": "Project kickoff under approved budget",
                    "description": "synthesized from notes A+B",
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
    synthesis_id = resp.json()["synthesis_id"]

    from app.graph import session
    with session() as s:
        rec = s.run(
            "MATCH (sy:synthesis {synthesis_id: $id})-[:from]->(src:input:note) "
            "RETURN count(DISTINCT src) AS c",
            id=synthesis_id,
        ).single()
    assert rec["c"] == 2
