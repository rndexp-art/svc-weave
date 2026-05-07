# weave service

The graph gateway for rndexp.art. Single HTTP entry point into Neo4j. Submodule of [rndexp-art/rndexpart](https://github.com/rndexp-art/rndexpart).

Internal port: **8007**. No public hostname.

See [AGENTS.md](AGENTS.md) for the architecture, schema additions, and migration plan.

## Files
- `app/` — FastAPI service.
- `app/schema.yml` — label/edge allowlist (single source of truth for what weave will write).
- `compose.fragment.yml` — included by the gateway's compose when this service is enabled.
- `caddy.fragment` — empty; weave is internal-only.

## Local dev

This service runs as part of the gateway. From the gateway repo root:

```sh
tools/rndexp service enable weave --env local
tools/rndexp up
```

Then `curl -H "X-Internal-Token: $WEAVE_INTERNAL_TOKEN" http://localhost:8007/healthz` (or from another container, `http://weave:8007/healthz`).

## Deploy

```sh
# from this submodule's directory
git push origin main:production
```
