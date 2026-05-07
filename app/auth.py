"""Internal-token guard + caller resolution.

Every endpoint requires `X-Internal-Token: $WEAVE_INTERNAL_TOKEN`. Callers
identify themselves with `X-Weave-Caller`:

  - `human:<auth_user_id>` for end-user-driven calls (dashboard, explorer,
     telegram WebApp acting on behalf of a user).
  - `service:<service_name>` for service-to-service calls (kiln worker,
     telegram-bot ingest). These resolve to one of the reserved sentinels
     in config.py.

The dependency `caller()` returns a `Caller` object with the resolved
`auth_user_id` already validated.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from . import config
from .config import settings


def require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = settings().internal_token
    if not x_internal_token or not hmac.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad internal token")


# Map of service-name -> (auth_user_id, provider_name) for service callers.
# Other services (kiln, explorer, telegram-bot) act on behalf of HUMANS, so
# they should pass `human:<id>` instead. These entries are for weave's own
# in-process callers (watcher, worker) that need an agent identity.
_SERVICE_CALLERS: dict[str, tuple[int, str]] = {
    "weave-gateway":       (config.WEAVE_GATEWAY_AGENT_ID,         config.WEAVE_PROVIDER_NAME),
    "extraction-watcher":  (config.EXTRACTION_WATCHER_AGENT_ID,    config.EXTRACTION_WATCHER_PROVIDER_NAME),
    "synthesis-watcher":   (config.SYNTHESIS_WATCHER_AGENT_ID,     config.SYNTHESIS_WATCHER_PROVIDER_NAME),
    "extraction-worker":   (config.EXTRACTION_WORKER_AGENT_ID,     config.EXTRACTION_WORKER_PROVIDER_NAME),
    "synthesis-worker":    (config.SYNTHESIS_WORKER_AGENT_ID,      config.SYNTHESIS_WORKER_PROVIDER_NAME),
}


@dataclass(frozen=True)
class Caller:
    kind: str               # 'human' | 'service'
    auth_user_id: int
    provider_name: str      # provider this caller will write under
    raw: str                # original X-Weave-Caller header value


def parse_caller(value: str) -> Caller:
    if not value or ":" not in value:
        raise HTTPException(400, "X-Weave-Caller header missing or malformed")
    kind, _, ident = value.partition(":")
    kind = kind.strip().lower()
    ident = ident.strip()
    if not ident:
        raise HTTPException(400, "X-Weave-Caller has no identifier")
    if kind == "human":
        try:
            uid = int(ident)
        except ValueError as e:
            raise HTTPException(400, "human caller id must be integer") from e
        if uid <= 0:
            raise HTTPException(400, "human caller id must be positive")
        return Caller(kind="human", auth_user_id=uid, provider_name="auth", raw=value)
    if kind == "service":
        if ident not in _SERVICE_CALLERS:
            raise HTTPException(400, f"unknown service caller: {ident!r}")
        uid, provider = _SERVICE_CALLERS[ident]
        return Caller(kind="service", auth_user_id=uid, provider_name=provider, raw=value)
    raise HTTPException(400, f"unknown caller kind: {kind!r}")


def caller(
    x_weave_caller: Annotated[str | None, Header()] = None,
    _token: Annotated[None, Depends(require_internal_token)] = None,
) -> Caller:
    if not x_weave_caller:
        raise HTTPException(400, "X-Weave-Caller header is required")
    return parse_caller(x_weave_caller)
