"""Runtime configuration for the weave service. Env-var-only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# Service-agent sentinel allocation for weave. Reserved range 1001-1099.
WEAVE_GATEWAY_AGENT_ID         = 1001
EXTRACTION_WATCHER_AGENT_ID    = 1002
SYNTHESIS_WATCHER_AGENT_ID     = 1003
EXTRACTION_WORKER_AGENT_ID     = 1004
SYNTHESIS_WORKER_AGENT_ID      = 1005

WEAVE_PROVIDER_NAME              = "weave"
EXTRACTION_WATCHER_PROVIDER_NAME = "extraction-watcher"
SYNTHESIS_WATCHER_PROVIDER_NAME  = "synthesis-watcher"
EXTRACTION_WORKER_PROVIDER_NAME  = "extraction-worker"
SYNTHESIS_WORKER_PROVIDER_NAME   = "synthesis-worker"


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str = field(default_factory=lambda: _env("NEO4J_URI", "bolt://neo4j:7687"))
    neo4j_user: str = field(default_factory=lambda: _env("NEO4J_USER", "neo4j"))
    neo4j_password: str = field(default_factory=lambda: _env("NEO4J_PASSWORD"))

    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY").strip().strip('"').strip("'"))
    openai_model: str = field(default_factory=lambda: _env("WEAVE_OPENAI_MODEL", "gpt-4o-mini"))

    internal_token: str = field(default_factory=lambda: _env("WEAVE_INTERNAL_TOKEN"))

    watcher_interval: float = field(default_factory=lambda: _env_float("WEAVE_WATCHER_INTERVAL", 60.0))
    worker_interval:  float = field(default_factory=lambda: _env_float("WEAVE_WORKER_INTERVAL", 30.0))
    watcher_batch:    int   = field(default_factory=lambda: _env_int("WEAVE_WATCHER_BATCH", 10))
    worker_batch:     int   = field(default_factory=lambda: _env_int("WEAVE_WORKER_BATCH", 25))
    watcher_pattern_cooldown_sec: int = field(default_factory=lambda: _env_int("WEAVE_WATCHER_PATTERN_COOLDOWN_SEC", 3600))

    synthesis_worker_enabled: bool = field(default_factory=lambda: _env_bool("WEAVE_SYNTHESIS_WORKER_ENABLED", False))


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        s = Settings()
        if not s.neo4j_password:
            raise RuntimeError("NEO4J_PASSWORD is required.")
        if not s.internal_token:
            raise RuntimeError(
                "WEAVE_INTERNAL_TOKEN is required. Generate with: "
                "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
            )
        _settings = s
    return _settings
