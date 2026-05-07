# AGENTS.md — weave service

The graph gateway for rndexp.art. Owns the Neo4j connection pool. **The only service that talks to Neo4j directly.** Everything else (kiln, telegram-bot, explorer, dashboard) calls weave's HTTP API.

This is a submodule of the [rndexpart gateway](https://github.com/rndexp-art/rndexpart). Read the gateway's AGENTS.md for the overall architecture and tooling.

## What weave introduces

- **Schema vocabulary registry** — `app/schema.yml` is the single source of truth for which labels and edge types weave will write. Adding a label is a PR. This is the cost of the gatekeeping; it's intentional.
- **Extraction primitive** — `POST /v1/extractions` takes a single source node and writes a set of new nodes/edges authored by the caller, each linked back via `:extracted_from`.
- **Synthesis primitive** — `POST /v1/syntheses` takes multiple source nodes/edges and writes a set of new nodes/edges, each linked back via `:synthesized_from`.
- **Extraction event** — every extraction creates a `(:extraction)` node with `:by` to the agent, `:from` to the source, `:produced` to outputs, optional `:used_prompt`. Synthesis is symmetric.
- **Edge records** — relationships produced by extraction/synthesis are reified as `(:edge_record)` nodes (with their own UNIQUE `edge_id`) so synthesis can reference them as inputs. Existing graph edges (`:source`, `:provider`, etc.) stay unreified.
- **Prompt registry** — `(:prompt)` nodes are versioned per `pattern_key`. The watcher creates them; an operator promotes from `'shadow'` to `'active'`; the worker runs `'active'` ones.

## Service shape

- Internal port: **8007**, no public Caddy site.
- All endpoints under `/v1/*`. Authenticated by `X-Internal-Token: $WEAVE_INTERNAL_TOKEN`.
- Caller identity is passed as `X-Weave-Caller: human:<auth_user_id>` or `X-Weave-Caller: service:<name>`. Used to resolve the acting agent.
- Background tasks (watcher + worker) run as asyncio tasks in the same process, started by FastAPI lifespan. Single-replica.

## Schema additions on top of the existing kiln/explorer/telegram-bot vocabulary

Existing labels weave inherits: `:provider`, `:agent`, `:identity`, `:source`, `:input:note`, `:input:task`. Existing edges: `:provider`, `:author`, `:source`, `:owner`, `:readable`, `:writeable`, `:agent`, `:chained`, `:overrides`, `:extracted_from`, `:mirrors`, `:processed`.

New labels:
- `:extraction { extraction_id, started_at, finished_at, prompt_id, model, meta }`
- `:synthesis  { synthesis_id, ... }`
- `:edge_record { edge_id, type, src_node_id, dst_node_id, properties_json, created_at }`
- `:prompt { prompt_id, pattern_key, version, kind, status, fingerprint, detector_prompt, extractor_prompt, output_schema, examples, notes, created_at }`

New relationships: `:by`, `:from`, `:produced`, `:used_prompt`, `:supersedes`, `:authored_by` (only on `:prompt`), `:synthesized_from`.

Authorship of nodes is via the existing chain `(node)-[:author]->(:identity)-[:agent]->(:agent)`. Service-written nodes get a service identity; weave creates one per service-agent at bootstrap.

## Service-agent sentinels

`auth_user_id` integers `1001-1099` are reserved for weave-internal service agents:

| auth_user_id | agent | provider |
|---|---|---|
| 1001 | weave-gateway | weave |
| 1002 | extraction-watcher | extraction-watcher |
| 1003 | synthesis-watcher | synthesis-watcher |
| 1004 | extraction-worker | extraction-worker |
| 1005 | synthesis-worker | synthesis-worker |

This range is reserved in the gateway's `config/services.yml` under `service_agents:`.

## Migration path for existing services

Each service drops its `neo4j` dependency and calls weave instead. Order:
1. **explorer** — small surface (subgraph read + create note); easiest to flip.
2. **kiln** — refactor `app/graph.py` into an httpx client; task creation flows as `POST /v1/extractions`.
3. **telegram-bot** — replace `app/neo4j_writer.py` with a single call to `POST /v1/integrations/telegram/messages`.

## Deploys

1. Push to `main` for development; merge to `production` to deploy.
2. `.github/workflows/deploy.yml` fires on push to `production`, calling `repository_dispatch` against the gateway with `event_type=service-updated`, `client_payload.service=weave`.
3. The gateway bumps the submodule pin, SSHes to the VPS, re-renders, and runs `docker compose up -d`.
