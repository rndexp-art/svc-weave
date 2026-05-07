"""Neo4j driver lifecycle + schema constraints.

weave is the only service that opens a neo4j driver in the rndexp.art
stack. Other services call its HTTP API.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

from neo4j import Driver, GraphDatabase

from .config import settings


log = logging.getLogger(__name__)


_driver: Driver | None = None


def init() -> None:
    global _driver
    if _driver is not None:
        return
    cfg = settings()
    _driver = GraphDatabase.driver(
        cfg.neo4j_uri,
        auth=(cfg.neo4j_user, cfg.neo4j_password),
        max_connection_pool_size=16,
    )
    _driver.verify_connectivity()


def close() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None


def driver() -> Driver:
    if _driver is None:
        raise RuntimeError("graph.init() not called")
    return _driver


@contextmanager
def session():
    with driver().session() as s:
        yield s


# Existing constraints (carried over from the kiln/telegram-bot/explorer
# era) plus weave's additions. Re-running CREATE CONSTRAINT IF NOT EXISTS
# is a no-op, so it's safe to keep these here even after the other
# services drop their own ensure_schema calls.
_CONSTRAINTS = [
    # carried over
    "CREATE CONSTRAINT note_id IF NOT EXISTS "
    "FOR (n:note) REQUIRE n.note_id IS UNIQUE",
    "CREATE CONSTRAINT note_external_pk IF NOT EXISTS "
    "FOR (n:note) REQUIRE (n.provider_name, n.external_chat_id, n.external_id) IS UNIQUE",
    "CREATE CONSTRAINT provider_name IF NOT EXISTS "
    "FOR (p:provider) REQUIRE p.name IS UNIQUE",
    "CREATE CONSTRAINT agent_auth_user_id IF NOT EXISTS "
    "FOR (a:agent) REQUIRE a.auth_user_id IS UNIQUE",
    "CREATE CONSTRAINT source_pk IF NOT EXISTS "
    "FOR (s:source) REQUIRE (s.provider_name, s.external_id) IS UNIQUE",
    "CREATE CONSTRAINT identity_pk IF NOT EXISTS "
    "FOR (i:identity) REQUIRE (i.provider_name, i.external_id) IS UNIQUE",
    "CREATE CONSTRAINT task_id IF NOT EXISTS "
    "FOR (t:task) REQUIRE t.task_id IS UNIQUE",
    "CREATE CONSTRAINT task_external_pk IF NOT EXISTS "
    "FOR (t:task) REQUIRE (t.provider_name, t.external_chat_id, t.external_id) IS UNIQUE",
    # weave additions
    "CREATE CONSTRAINT extraction_id IF NOT EXISTS "
    "FOR (e:extraction) REQUIRE e.extraction_id IS UNIQUE",
    "CREATE CONSTRAINT synthesis_id IF NOT EXISTS "
    "FOR (s:synthesis) REQUIRE s.synthesis_id IS UNIQUE",
    "CREATE CONSTRAINT prompt_id IF NOT EXISTS "
    "FOR (p:prompt) REQUIRE p.prompt_id IS UNIQUE",
    "CREATE CONSTRAINT prompt_pattern_version IF NOT EXISTS "
    "FOR (p:prompt) REQUIRE (p.pattern_key, p.version) IS UNIQUE",
    "CREATE CONSTRAINT edge_record_id IF NOT EXISTS "
    "FOR (er:edge_record) REQUIRE er.edge_id IS UNIQUE",
    "CREATE INDEX prompt_status IF NOT EXISTS "
    "FOR (p:prompt) ON (p.status, p.kind)",
    "CREATE INDEX prompt_fingerprint IF NOT EXISTS "
    "FOR (p:prompt) ON (p.fingerprint)",
]


def ensure_schema() -> None:
    with session() as s:
        for c in _CONSTRAINTS:
            s.run(c)
