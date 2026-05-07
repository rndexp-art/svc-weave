"""Bootstrap weave's own service-agent + provider + identity nodes.

Idempotent. Run on startup. Mirrors the kiln pattern (kiln/app/graph.py
bootstrap()) but extended: in addition to the (:agent)-[:provider] edge,
each service-agent gets an (:identity) node so it can be the target of
the existing (:input)-[:author]->(:identity) edge.
"""
from __future__ import annotations

import logging

from . import config
from .graph import session


log = logging.getLogger(__name__)


# Idempotent. The identity acts as the "who composed this" target for
# nodes weave-internal agents create. We use external_id = name of the
# service-agent (singleton per provider).
_BOOTSTRAP_SERVICE_AGENT = """
MERGE (p:provider {name: $provider_name})
  ON CREATE SET p.created_at = datetime(), p.kind = $provider_kind

MERGE (a:agent {auth_user_id: $auth_user_id})
  ON CREATE SET a.created_at = datetime(),
                a.kind = 'service',
                a.name = $agent_name
SET a.kind = 'service', a.name = $agent_name

MERGE (a)-[:provider]->(p)

MERGE (i:identity {provider_name: $provider_name, external_id: $identity_external_id})
  ON CREATE SET i.created_at = datetime(),
                i.kind = 'service',
                i.name = $agent_name
SET i.name = $agent_name

MERGE (i)-[:provider]->(p)
MERGE (i)-[:agent]->(a)
"""


_SERVICE_AGENTS = [
    # (auth_user_id, agent_name, provider_name, provider_kind, identity_external_id)
    (config.WEAVE_GATEWAY_AGENT_ID,         "weave-gateway",        config.WEAVE_PROVIDER_NAME,              "gateway", "weave-gateway"),
    (config.EXTRACTION_WATCHER_AGENT_ID,    "extraction-watcher",   config.EXTRACTION_WATCHER_PROVIDER_NAME, "watcher", "extraction-watcher"),
    (config.SYNTHESIS_WATCHER_AGENT_ID,     "synthesis-watcher",    config.SYNTHESIS_WATCHER_PROVIDER_NAME,  "watcher", "synthesis-watcher"),
    (config.EXTRACTION_WORKER_AGENT_ID,     "extraction-worker",    config.EXTRACTION_WORKER_PROVIDER_NAME,  "worker",  "extraction-worker"),
    (config.SYNTHESIS_WORKER_AGENT_ID,      "synthesis-worker",     config.SYNTHESIS_WORKER_PROVIDER_NAME,   "worker",  "synthesis-worker"),
]


def bootstrap_service_agents() -> None:
    """Create weave's five service-agent + provider + identity rows."""
    with session() as s:
        for uid, name, prov, prov_kind, ext_id in _SERVICE_AGENTS:
            s.execute_write(lambda tx, _u=uid, _n=name, _p=prov, _pk=prov_kind, _ei=ext_id: tx.run(
                _BOOTSTRAP_SERVICE_AGENT,
                auth_user_id=_u,
                agent_name=_n,
                provider_name=_p,
                provider_kind=_pk,
                identity_external_id=_ei,
            ).consume())
    log.info("bootstrapped %d weave service-agents", len(_SERVICE_AGENTS))


# Generic bootstrap for human users (replaces explorer/kiln per-service
# bootstrap helpers). Idempotent.
_BOOTSTRAP_HUMAN_AGENT = """
MERGE (auth:provider {name: 'auth'})
  ON CREATE SET auth.created_at = datetime(), auth.kind = 'auth'

MERGE (a:agent {auth_user_id: $auth_user_id})
  ON CREATE SET a.created_at = datetime(), a.kind = 'human'
SET a.name = coalesce($name, a.name),
    a.email = coalesce($email, a.email)

MERGE (i:identity {provider_name: 'auth', external_id: $auth_external_id})
  ON CREATE SET i.created_at = datetime(), i.kind = 'human',
                i.email = $email,
                i.name = $name
MERGE (i)-[:provider]->(auth)
MERGE (i)-[:agent]->(a)
RETURN a
"""


def bootstrap_human_agent(*, auth_user_id: int, email: str | None, name: str | None) -> None:
    with session() as s:
        s.execute_write(lambda tx: tx.run(
            _BOOTSTRAP_HUMAN_AGENT,
            auth_user_id=auth_user_id,
            auth_external_id=str(auth_user_id),
            email=(email or None),
            name=(name or None),
        ).consume())


def resolve_caller_identity_id(*, auth_user_id: int, provider_name: str) -> str | None:
    """Return the element_id of the (:identity) for this caller, or None.

    For human callers: identity is the auth-provider identity.
    For service callers: identity is the service's singleton identity.
    """
    cypher = """
    MATCH (i:identity {provider_name: $provider_name})
    MATCH (i)-[:agent]->(:agent {auth_user_id: $auth_user_id})
    RETURN i LIMIT 1
    """
    with session() as s:
        rec = s.run(cypher, auth_user_id=auth_user_id, provider_name=provider_name).single()
    return rec["i"].element_id if rec else None
