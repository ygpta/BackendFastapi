# Support Agent API

Production-oriented Python 3.13 / FastAPI service with Pydantic v2, async
SQLAlchemy, PostgreSQL, Alembic, Docker, and persistent LangGraph chat.
Existing ticket endpoints and learning material are retained.

**Learning this codebase?** Start with [the hands-on learning path](docs/learning-path.md).
It maps the current code to tests, debugging steps, exercises and questions to answer.
Search for `Learn:` in the source to find the study notes at important boundaries.
For a quick Hinglish Pydantic refresher, open the
[Pydantic revision sheet](docs/pydantic-revision-hinglish.html).
Database seekhne ke liye [SQLAlchemy aur Alembic Hinglish guide](docs/sqlalchemy-alembic-revision-hinglish.html) dekho.

**The tenant agent platform is implemented.** See
[the platform guide](docs/agent-platform.md) for login/roles, quotas, idempotent
durable jobs, approvals, SSE, retrieval, evaluations, tracing, alerts and load tests.
Production now requires JWT authentication; API-key business access is legacy-only.

This is a deployable foundation, not a claim that infrastructure is production
ready without configuration. Read [the operations guide](docs/production.md)
before exposing it to traffic.

## Run Locally

Requires Docker Compose v2.24.4+. No environment file or model credentials are
needed for tickets.

```bash
docker compose up --build -d
docker compose logs -f migrate api
curl http://127.0.0.1:8000/health/ready
```

API docs: http://127.0.0.1:8000/docs. Compose waits for PostgreSQL, runs a single
migration job, then starts the API. Data stays in a named volume.
`docker compose down` preserves it; `docker compose down -v` destroys it.
Default credentials and unauthenticated development mode are local only.
Set `API_PORT` or `POSTGRES_PORT` to avoid occupied host ports.

For host development with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --locked
docker compose up -d db
uv run alembic upgrade head
uv run uvicorn backendfastapi.main:app --reload --no-access-log
```

Settings come from environment variables and optionally a local `.env` file.
Never commit real credentials. [pyproject.toml](pyproject.toml) defines dependencies;
[uv.lock](uv.lock) locks exact versions. These are the only dependency manifests;
there is no manually duplicated requirements list. Install with `uv sync --locked`
or, without uv, `pip install .` (`pip` alone does not enforce the uv lockfile).

## Enable Chat

Set these variables in your shell, secret manager, or local environment file:

```dotenv
CHAT_ENABLED=true
OPENAI_API_KEY=<your-provider-key>
OPENAI_MODEL=gpt-4.1-mini
API_KEY=<random-service-key-at-least-32-characters>
```

Generate a key locally with `openssl rand -hex 32`. Do not send secrets through
chat or commit them. Recreate the API with `docker compose up -d api`.

OpenAI is the default chat provider. To use xAI/Grok, configure
`LLM_PROVIDER=xai`, `LLM_API_KEY`, and `LLM_MODEL` (for example, a Grok model enabled
for your account). For Ollama, set `LLM_PROVIDER=ollama`, `LLM_MODEL` to a model
already pulled on the Ollama server, and optionally `LLM_BASE_URL` to the server's
OpenAI-compatible `/v1` endpoint. From Docker, `localhost` means the application
container; use a reachable host or Compose service address instead. OpenAI
embeddings remain a separate setting for semantic retrieval: configure
`OPENAI_API_KEY` and `OPENAI_EMBEDDING_MODEL`, or retrieval uses lexical search.

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \
  -d '{"message":"Hello, can you help with a login issue?"}'
```

The response contains `conversation_id` and `message`. Send that ID with the
next message to continue. PostgreSQL history survives API restarts and works
across workers. The graph is `START -> respond -> END`, with a server-controlled
system prompt and bounded recent history. It does not run tools or background jobs.

```json
{"conversation_id":"<returned-uuid>","message":"What did I just ask?"}
```

| Route | Purpose |
| --- | --- |
| `GET /health/live` | Process liveness |
| `GET /health/ready` | Database connectivity |
| `GET /metrics` | Authenticated Prometheus HTTP/process/agent metrics |
| `GET /api/v1/dashboard/summary` | Dashboard ticket and chat activity counts |
| `POST /api/v1/tickets/preview` | Validate ticket input |
| `POST /api/v1/tickets` | Persist a ticket |
| `GET /api/v1/tickets` | Filter/search tickets with cursor pagination |
| `GET /api/v1/tickets/{id}` | Retrieve a ticket |
| `PATCH /api/v1/tickets/{id}` | Update status using an expected version |
| `POST /api/v1/chat` | Create or continue a conversation |
| `GET /api/v1/chat/{id}/messages` | Recent messages, oldest first |
| `DELETE /api/v1/chat/{id}` | Delete conversation and messages |
| `POST /api/v1/auth/login` | Tenant user login |
| `POST /api/v1/runs` | Idempotent, quota-reserved agent job |
| `POST /api/v1/runs/batch` | Enqueue up to 10 durable jobs per request |
| `POST /api/v1/webhooks/runs` | JWT-authenticated webhook-compatible run submission |
| `GET /api/v1/runs/{id}/events` | Replayable SSE token/state events |
| `POST /api/v1/runs/{id}/cancel` | Cancel a durable run |
| `POST /api/v1/runs/{id}/approval` | Separate-admin approval |
| `GET /api/v1/knowledge/search` | Tenant-scoped hybrid semantic and lexical search |
| `POST /api/v1/knowledge/documents/{id}/embed` | Admin re-embeds an existing document |
| `GET /api/v1/usage` | Monthly spending/reservations |
| `GET /api/v1/audit` | Administrator audit records |

History accepts `limit` (1-100) and `before_id`; pass the first returned message
ID to fetch the preceding page. Disabled chat returns `503`. Concurrent turns
may return `409`; fetch history before deciding to resubmit. Provider errors
return `502`, saturation `503`, and timeouts `504`. Do not automatically retry
a message whose response was lost: this API does not implement idempotency keys.

That warning applies to legacy inline `/chat`. Authenticated mode disables it:
use `/runs` with a required `Idempotency-Key`. Ticket creation also supports optional
keys. Enable Compose's `agent` profile for the worker and checkpoint setup job.

The dashboard and ticket-management screen contracts, examples, and prioritized
remaining features are in [the feature roadmap](docs/features-and-roadmap.md).
These are backend APIs, not rendered frontend screens. Requests are bounded to
32 KiB by default (`MAX_REQUEST_BODY_BYTES`); oversized bodies return `413`.

## Structure

```text
src/backendfastapi/
  main.py                  # App factory, lifespan, middleware, error boundary
  api/deps.py              # Request-scoped database session
  api/v1/endpoints/        # HTTP adapters: health, tickets, dashboard, chat
  core/                   # Settings, authentication, logging, metrics, body limits
  schemas/                # Pydantic input/output contracts
  services/               # Use cases and transaction coordination
  repositories/           # SQL queries and atomic persistence
  agents/                 # LangGraph definitions, no HTTP/database coupling
  db/
    base.py               # Shared SQLAlchemy metadata
    session.py            # Async connection pool and session factory
    tenancy.py            # Tenant and immutable-write enforcement hooks
    models/
      identity.py         # Tenants, users, login sessions
      support.py          # Tickets, conversations, messages
      controls.py         # Audit, rate, budget and idempotency records
      agents.py           # Runs, tools, knowledge, prompts and evaluations
      mixins.py           # Shared tenant column
  worker.py               # Durable worker entry point and execution lifecycle
  manage.py               # Operator-only administrator provisioning
migrations/               # Alembic schema changes
monitoring/               # Prometheus, alert routing and collector configuration
tests/                    # API/workflow and PostgreSQL integration tests
  helpers.py              # Shared HTTP test setup, not imported from test modules
  load/                   # k6 scenarios
docs/                     # Current guides plus clearly labeled learning material
.github/workflows/        # Lint, tests, migrations, audit, container build
```

Read the code in this order: app factory, API dependencies, endpoint, schema,
service, repository, then model. Services coordinate use cases and transactions;
repositories perform persistence. Shared audit writes belong to
[repositories/audit.py](src/backendfastapi/repositories/audit.py), not a service
imported by repositories. The worker calls services and graph definitions directly,
without going through HTTP endpoints.

Model classes remain available from `backendfastapi.db.models`; the package imports
all domains so Alembic always sees the complete schema. Keep migration revisions,
package initializers, the lockfile and deployment configurations: they are required
even when a normal HTTP request does not import or execute them.

Chat releases its read transaction before calling the model, then atomically
stores a user/assistant pair using optimistic concurrency. Graphs are process-local;
conversation state is not. Application-owned history is not LangGraph durable
checkpointing or resumable execution.

## Verify

```bash
uv run ruff check .
uv run ruff format --check src tests
uv run pytest --cov=backendfastapi --cov-report=term-missing
uv run pip-audit --local
```

Default tests use a fake model and SQLite. PostgreSQL integration tests real
migrations, cascades, and concurrent writers. Supply a **disposable** database;
the test runs schema downgrades:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://app:password@localhost:5432/disposable_test \
  uv run pytest -m postgres -q
```

See [production operations](docs/production.md) for configuration and deployment.
The original [learning path](docs/learning-path.md) and numbered lessons describe
earlier milestones; this README and the operations guide describe the current app.
