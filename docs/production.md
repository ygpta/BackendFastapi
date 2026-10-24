# Production Operations

**Current platform:** [Tenant Agent Platform](agent-platform.md) documents login,
roles/isolation, durable runs and shared controls. Production requires JWT auth,
not just a service key. Legacy chat limitations below apply only to auth-disabled
development; `/runs` adds checkpoints, idempotency, SSE, approvals and quotas.

## Security Boundary

Auth-disabled local business routes accept `X-API-Key`. Key comparisons are
constant time. Production refuses to start without a key of at least 32 characters
and a non-default PostgreSQL password. Development may deliberately omit a key.
Health probes are public. `/metrics` uses the same API-key dependency as the API.
Interactive docs and OpenAPI are disabled in production.

The legacy API-key path is **single trusted-service authentication**.
Everyone with the shared key can access the same tickets and conversations.
Ownership is scoped to that service identity; key rotation keeps it stable.
For multi-tenant use, replace the auth dependency with verified OIDC/JWT identities
and enforce tenant ownership on every query. Never embed the key in a public UI.

Chat text is stored as plaintext in the database and sent to OpenAI. Approve the
provider data policy, enable encryption at rest and in transit, restrict DB access,
set retention/deletion policies, and obtain required consent. The request logger
does not record bodies, keys, query strings, or provider error bodies. Upstream
tracing must also redact secrets. Do not enable LangSmith tracing for private data
without approval.

## Configuration

| Variable | Default / requirement |
| --- | --- |
| `ENVIRONMENT` | `development`; production enables strict validation |
| `DATABASE_URL` | `postgresql+asyncpg://...`; percent-encode credentials |
| `API_KEY` | Optional locally; production minimum 32 characters |
| `ALLOWED_HOSTS` | JSON array; production Compose requires an explicit value |
| `CORS_ORIGINS` | JSON array; defaults to no cross-origin browser access |
| `CHAT_ENABLED` | `false`; enabling requires credentials for the selected chat provider |
| `OPENAI_MODEL` | `gpt-4.1-mini`; choose an approved accessible chat model |
| `LLM_PROVIDER` | `openai`; alternatively `xai` or `ollama` |
| `LLM_API_KEY` | Required for xAI/Grok; Ollama does not require a provider key |
| `LLM_MODEL` | Explicit model name required for xAI/Grok and Ollama |
| `LLM_BASE_URL` | Optional OpenAI-compatible provider base URL override |
| `CHAT_TIMEOUT_SECONDS` | `30`; overall workflow deadline, max 120 |
| `CHAT_MAX_HISTORY_MESSAGES` | `20`; max 100; dangling assistant turn omitted |
| `CHAT_MAX_OUTPUT_TOKENS` | `1024`; max 4096 |
| `DATABASE_POOL_SIZE` | `5` per worker |
| `DATABASE_MAX_OVERFLOW` | `10` per worker |
| `DATABASE_TIMEOUT_SECONDS` | `10`; pool, connection, command/statement timeout |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small`; semantic retrieval uses 1536 dimensions |
| `WORKER_CONCURRENCY` | `4`; maximum simultaneous durable runs per worker process |
| `PROVIDER_MAX_CONCURRENCY` | `8`; simultaneous chat/embedding calls per process |
| `LOG_LEVEL` | `INFO`; JSON application/access events |
| `MAX_REQUEST_BODY_BYTES` | `32768`; allowed range 1 KiB-10 MiB |

Settings are validated and cached per process. Production rejects wildcard
hosts/origins. Restart workers to apply changes. Do not log settings or URLs.
Use an orchestrator secret manager; Docker administrators can inspect Compose
environment variables.

### Environment File Selection

Host par `APP_ENV_FILE=.env.development` ya `APP_ENV_FILE=.env.production` set
karne se shared `get_settings()` loader sirf selected file read karta hai. Absolute
path bhi de sakte ho; relative path working directory se resolve hota hai. Explicit
file missing ho to startup fail hota hai. Variable unset ho to existing `.env`
behavior rehta hai; `APP_ENV_FILE=''` se dotenv reading band hoti hai. File name
mode infer nahi karta: `ENVIRONMENT=production` separately required hai. Process
environment selected file ko override karta hai. Selector ko shell/deployment mein
set karo, selected file ke andar nahi; file choose karne se pehle yeh chahiye.

Production mein preferred approach secret manager/orchestrator se environment
inject karna hai, dotenv file image mein bake karna nahi. API, worker, migrations
aur admin command sab shared loader use karte hain; unko consistent settings do.
Settings imports ke waqt bhi load hoti hain, isliye startup se pehle values set
karo. Cache clear karne se already-created database engine rebuild nahi hota;
changes ke liye processes restart/recreate karo.

Compose ka `--env-file` host-side interpolation ke liye hai, Python ke
`APP_ENV_FILE` ka replacement path nahi. Bundled app services `APP_ENV_FILE: ""`
use karti hain aur Compose ke `environment` mapping ki injected values read karti
hain. Host env file automatically container mein mount nahi hoti. Production
override sab app services mein mode/auth restrictions enforce karta hai:

```bash
docker compose --env-file .env.production -f compose.yaml -f compose.production.yaml up --build -d
```

Yeh file-based operator option hai; preferred secrets injection wale deployment
mein existing command bina `--env-file` use karo aur values trusted environment se
do. Sirf Compose mapping mein listed variables forward hote hain: naye settings
ke liye mapping ya orchestrator configuration bhi update karo. `.gitignore` aur
`.dockerignore` already `.env.*` ko exclude karte hain; real files commit, logs ya
image mein mat daalo. Deployment secrets ke liye least-privilege access, rotation
aur audit controls bhi chahiye.

## Logging and Metrics

JSON access events contain request ID, method, canonical route template, status,
and elapsed milliseconds. Bodies, credentials, raw query strings, and raw
tracebacks are not captured. SQL parameters are hidden. This deliberately trades
full local-variable diagnostics for safer shared logs; add approved, redacted
error reporting rather than enabling unrestricted payload capture.

Scrape `GET /metrics` with `X-API-Key` when authentication is enabled. It includes
HTTP counts/latency/in-progress requests, process memory/CPU/GC, generation
outcomes/latency, and provider-reported input/output tokens. Route templates keep
resource IDs out of metric labels. Unknown routes and probes are excluded from
HTTP histograms; pre-routing body/host rejections are visible in logs, not those
route-specific counters. Metrics never contain prompts or generated text.

Generation success means valid provider text, not a committed database turn.
Cancelled generations may include overall timeouts. Token counters cover only
returned usage metadata, not provider-internal retries, interrupted calls, or
missing reports. They reset on process restart and are not a billing ledger or
budget enforcement. Scrape one worker per container and aggregate replicas in
Prometheus. No Prometheus server, alert rules, dashboards, or distributed trace
exporter is provisioned by this repository.

The ASGI body guard buffers at most the configured limit before JSON parsing and
checks chunked bodies even without a declared size. It is intended for these
small JSON routes, not streaming uploads. Keep gateway body limits and slow-client
timeouts as well. A `413` response retains its request ID.

## Deployment

The locked multi-stage image runs as a non-root UID without development tools.
Compose runs the API read-only, drops capabilities, restricts CPU/memory/PIDs,
and allows a small temporary filesystem. Pin approved base-image digests in your
release pipeline and rebuild regularly for OS fixes.

With secrets supplied by your deployment environment:

```bash
export ALLOWED_HOSTS='["api.example.com","127.0.0.1"]'
export ENVIRONMENT=production
export AUTH_ENABLED=true
# Provide an independent JWT_SECRET of at least 48 characters through secrets.
# Supply API_KEY, POSTGRES_PASSWORD, and DATABASE_URL from a secret manager.
# For bundled PostgreSQL, DATABASE_URL points to db:5432 and credentials match.
docker compose -f compose.yaml -f compose.production.yaml up --build -d
```

For xAI/Grok or Ollama, set `LLM_PROVIDER`, `LLM_MODEL`, and the applicable
`LLM_API_KEY`/`LLM_BASE_URL`. Ollama must be reachable from both API and worker
containers. Semantic retrieval still uses the configured OpenAI embedding model;
without `OPENAI_API_KEY`, document search falls back to PostgreSQL full-text search.

Requires Compose v2.24.4+ for `!reset`. Include `127.0.0.1` in allowed hosts for
container probes. The override removes the DB host port; the API binds to host
loopback. Place a trusted gateway in front. For managed PostgreSQL, deploy just
the API and migration job with your orchestrator, separate least-privilege runtime
and migration credentials, verified TLS, backups, and point-in-time recovery.

The gateway **must** supply HTTPS, body limits (for example 32 KiB for these JSON
routes), distributed rate limits/quotas, and upstream timeouts longer than the chat
deadline. Set model spending alerts. Uvicorn concurrency limits are not a rate
limiter. The image ignores forwarded headers; enable them only with explicitly
trusted proxy IPs in your deployment command.

The worker runs up to `WORKER_CONCURRENCY` jobs concurrently. The process-local
`PROVIDER_MAX_CONCURRENCY` semaphore separately caps simultaneous chat and embedding
requests; it does not enforce a fleet-wide provider quota. Scale replicas and
budget DB connections as
`replicas * workers * (pool_size + max_overflow)`, plus migration/admin connections.
Tune concurrency and memory under realistic load. SIGTERM permits 40 seconds for
in-flight requests; Compose allows 45 seconds. Increase both for longer chat
deadlines. Model HTTP clients and database pools close during lifespan shutdown.

## Migrations

Never run `metadata.create_all()` at startup. Run one migration job before rolling
out replicas, not Alembic in every worker. Compose's `migrate` service provides
this separation for one deployment. Serialize deployments externally; concurrent
migration jobs are not supported.

```bash
uv run alembic revision --autogenerate -m "describe schema change"
# Review generated SQL, locking behavior, indexes, and downgrade behavior.
uv run alembic upgrade head
uv run alembic check
uv run alembic current
```

Use expand/contract changes for zero downtime, test on a restored snapshot, and
verify backups. Dropping chat tables destroys data; prefer forward fixes and
never downgrade automatically on a failed rollout. Readiness checks connectivity,
not schema revision or provider health. Gate rollout on migration success.

## Chat Guarantees

Input is a bounded user message and optional conversation UUID. Clients cannot
inject fake persisted assistant turns. Only recent bounded history reaches the
model; older messages remain available through cursor pagination. Responses are
untrusted plain text. Frontends rendering Markdown must sanitize HTML and links.
The workflow has no tools or side-effect permissions.

The model runs outside a DB transaction. Both messages commit together after
generation. A version check rejects overlapping writers with `409`; the losing
answer is discarded but the provider may still bill the call. Serialize turns
per conversation. Deletion cascades to messages.

There are no idempotency keys, streaming responses, cross-provider failover,
durable graph checkpoints, or background workers. A crash can leave a model call
billed without a saved response. Add an idempotent job/outbox design before using
chat for side effects or retryable long-running work.

## Release Checklist

- Run lint, fake-model tests, and disposable PostgreSQL migration/concurrency tests.
- Audit locked dependencies and scan the image in your release platform.
- Configure rotation, TLS, private networking, backups, and restore tests.
- Enforce gateway quotas, body limits, timeouts, and provider spending alerts.
- Send JSON logs to a collector; correlate using `X-Request-ID`.
- Monitor readiness, errors, latency, DB pool pressure, and provider failures.
- Configure Prometheus scraping, distributed tracing, and alerts in your platform.
- Load-test and approve retention, identity, and model data-handling policies.
- Smoke-test real provider access in staging using non-sensitive data.

CI does not replace infrastructure scanning, real-model staging validation, or
workload-specific SLOs.

Use `--profile agent` for durable workers and checkpoint setup. Configure model
pricing, budgets, retention, monitoring secret mounts and a real alert receiver
as described in the platform guide.