"""Health, status, and synchronous-tick endpoints."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_internal_token
from .config import settings
from .graph import session


router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True}


@router.get("/v1/status", dependencies=[Depends(require_internal_token)])
def status_endpoint() -> dict[str, Any]:
    cfg = settings()
    counts: dict[str, int] = {}
    try:
        with session() as s:
            for label in ("agent", "identity", "provider", "input", "extraction", "synthesis", "prompt", "edge_record"):
                rec = s.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()
                counts[label] = int(rec["c"]) if rec else 0
    except Exception as e:  # neo4j down
        return {"neo4j": "down", "error": str(e), "model": cfg.openai_model}
    return {
        "neo4j": "up",
        "model": cfg.openai_model,
        "openai_configured": bool(cfg.openai_api_key),
        "watcher_interval": cfg.watcher_interval,
        "worker_interval":  cfg.worker_interval,
        "synthesis_worker_enabled": cfg.synthesis_worker_enabled,
        "counts": counts,
    }


@router.post("/v1/internal/tick", dependencies=[Depends(require_internal_token)])
def tick(phase: str = "watcher") -> dict[str, Any]:
    """Synchronous tick — used by tests and the dashboard 'process now' button."""
    if phase == "watcher":
        from . import watcher
        return watcher.tick_extraction() | {"phase": "watcher"}
    if phase == "worker":
        from . import worker
        return worker.tick_extraction() | {"phase": "worker"}
    if phase == "synthesis-watcher":
        from . import watcher
        return watcher.tick_synthesis() | {"phase": "synthesis-watcher"}
    if phase == "synthesis-worker":
        from . import worker
        return worker.tick_synthesis() | {"phase": "synthesis-worker"}
    raise HTTPException(400, f"unknown phase: {phase!r}")
