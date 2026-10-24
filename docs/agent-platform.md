# Tenant Agent Platform

## Scope

Implemented: Argon2/JWT login, rotating revocable sessions, admin/operator/viewer
roles, tenant-scoped ORM sessions, shared rate/spend controls, idempotent jobs and
ticket creation, audits, leased workers, official LangGraph PostgreSQL checkpoints,
SSE/cancellation, approved allowlisted tools, pgvector semantic and lexical RAG,
tenant-scoped embedding caching, bounded batch run submission, JWT-authenticated
run webhooks, prompt versions, feedback, deterministic evaluations, OTLP tracing,
alert rules and a k6 scenario.

These are bounded implementations, not guarantees of arbitrary-agent safety,
exactly-once external actions, semantic answer quality or invoice accuracy.
Automated tests use fake models and do not make paid provider requests.

## Local Setup

Generate secrets directly in your shell, not in source control or chat:

```bash
export AUTH_ENABLED=true
export REGISTRATION_ENABLED=true
export JWT_SECRET="$(openssl rand -hex 48)"
docker compose --profile agent up --build -d
```

For real generation set `CHAT_ENABLED=true`, `OPENAI_API_KEY` and an approved
`OPENAI_MODEL`, then recreate API/worker. Semantic search uses
`OPENAI_EMBEDDING_MODEL` (default `text-embedding-3-small`, 1536 dimensions).
Default prices match the default model;
set both price variables when changing it. There is no fake-provider production mode.

Register locally with `POST /api/v1/auth/register`:

```json
{"tenant_name":"Example","email":"admin@example.com","password":"a-unique-long-password"}
```

The first account is always admin of a new tenant. Tokens include the tenant ID.
Send `Authorization: Bearer <access_token>` on business APIs. Store tokens securely,
never in URLs. Prefer in-memory access tokens and a reviewed backend-for-frontend
for browser refresh-token storage, not localStorage.

Production requires `AUTH_ENABLED=true`, an independent 48+ character `JWT_SECRET`,
a metrics `API_KEY`, explicit hosts and non-default DB credentials. Production
self-registration is disabled. Bootstrap an admin interactively:

```bash
docker compose exec api python -m backendfastapi.manage \
  --tenant-name Example --email admin@example.com
```

The command prompts for the password directly in the terminal. `--tenant-id` adds
an admin to an existing tenant. Legacy tickets migrate to `development`; existing
conversations/messages retain their owner as tenant ID. Deliberately provision into
`development` or `service` only when adopting that legacy data.

## Identity and Roles

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/auth/login` | Tenant ID, email, password; returns tokens |
| `POST /api/v1/auth/refresh` | One-time hashed refresh-token rotation |
| `GET /api/v1/auth/me` | Current account and database role |
| `POST /api/v1/auth/logout` | Revoke session and its access tokens |
| `POST /api/v1/auth/users` | Admin creates a tenant account |
| `PATCH /api/v1/auth/users/{id}` | Different admin changes role/active state |

Roles and account status are checked on every authenticated request. Viewers only
read; operators manage tickets, knowledge, runs and feedback; admins additionally
manage users, prompts, grants, budgets, approvals, audits and evaluations. Own-account
disable/demotion is rejected. Tenant IDs come from verified tokens, not headers or
request bodies. Cross-tenant resources return `404`.

Business sessions enforce tenant criteria on ORM reads and bulk updates/deletes,
and reject cross-tenant flushes. This is application-layer isolation, not database
RLS. Raw SQL and privileged auth/worker sessions must stay internal reviewed code.

Login/refresh/registration use shared IP fixed-window limits; business calls use
shared per-user limits (default 120/minute). PostgreSQL atomic upserts coordinate
replicas. Database failure never falls back to process memory. Rate-limit `429`
responses include `Retry-After`. Fixed windows permit boundary bursts; a trusted
gateway still supplies TLS, abuse/connection limits and slow-client timeouts.

## Runs and Streaming

```http
POST /api/v1/runs
Authorization: Bearer <token>
Idempotency-Key: unique-client-generated-key
Content-Type: application/json

{"message":"How do I reset login?","use_rag":true}
```

Returns `202` with a run ID. Same tenant/key and request replays that run; different
input returns `409`. Key, budget reservation and job commit atomically. Ticket
creation honors optional idempotency keys; status updates use expected versions.
Other configuration endpoints do not universally implement idempotency keys.
Keys have no automatic expiry; define retention without invalidating legitimate
retries or deleting active-run keys.

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/runs/batch` | Up to 10 separately idempotent durable jobs |
| `POST /api/v1/webhooks/runs` | JWT-authenticated webhook-compatible run submission |
| `GET /api/v1/runs` | Recent tenant runs |
| `GET /api/v1/runs/{id}` | State, output, safe error classification |
| `GET /api/v1/runs/{id}/events` | Persisted SSE state/token batches |
| `POST /api/v1/runs/{id}/cancel` | Idempotent cancellation request |
| `POST /api/v1/runs/{id}/approval` | Versioned separate-admin approval/denial |

Reconnect with `Last-Event-ID`. `follow=false` reads the current backlog and exits.
Streams close after five minutes for reauthentication and recheck session revocation.
Use fetch-based SSE for bearer headers. Disconnect only unsubscribes; `/cancel`
requests cancellation. Partial tokens may be replayed and are not committed turns;
only `succeeded` means the conversation pair committed.

Workers claim with `FOR UPDATE SKIP LOCKED`, renew leases and fence writes using
claim tokens. Retrieval, generation and tool nodes use `AsyncPostgresSaver` with
tenant-prefixed thread IDs. A one-shot setup job versions checkpoint tables;
Alembic excludes those package-owned tables.

Expired leases resume from the last checkpoint. Generation interrupted before its
checkpoint can repeat and be billed again. Recovery conservatively charges the
prior attempt's full reservation and requires budget for another; attempts are
bounded. Conversation messages commit with final state. Concurrent turns can end
with `conversation_conflict`: serialize a conversation's runs in clients.
Cancellation cannot undo provider bills or an already committed tool action;
final output includes any durable tool receipt.

Authenticated chat must use `/runs`; inline `/chat` rejects authenticated mode so
it cannot bypass reservations. Inline chat remains legacy local-development only.

## Spending

`GET /api/v1/usage` reports the UTC month's allowance/spent/reserved in integer
micro-USD. Admin `PUT /api/v1/usage/budget` accepts
`{"allowance_microusd":10000000}` for $10. The allowance cannot drop below spent
plus reserved. Before enqueueing, atomic reservations cover configured maximum
input/output cost. Tokenized context limits are checked before calling the model.
Automatic provider retries are disabled in durable runs.

Successful calls settle reported usage against configured prices. Missing usage,
interrupted calls and uncertain crash attempts consume the full reservation.
Cancellation before execution releases it. This is a conservative local quota
ledger, not an invoice. Prices must match the model; provider-side budgets/alerts
are still required. Pending approvals retain reservations until approved/cancelled.
The cap is per tenant, not a global provider-project budget.

Embedding-provider calls for document ingestion and semantic queries are separate
from durable run generation and are not included in the tenant run-budget ledger.
Provider calls share the process-local `PROVIDER_MAX_CONCURRENCY` semaphore; configure
provider-side spending limits/alerts and control document ingestion access.

## Tools and Approval

Only `resolve_ticket` exists. No arbitrary shell, code, network or browser tools.
Admin `PUT /api/v1/tools/resolve_ticket` with `{"enabled":true}` enables it.
Run input may include:

```json
{"message":"Resolve this ticket","tool":{"name":"resolve_ticket","ticket_id":42,"expected_version":1}}
```

The run waits for approval. A different active admin POSTs
`{"approved":true,"expected_version":1}` to its approval endpoint. Arguments are
client-reviewed and schema/tenant validated, not invented by the model. Before
execution the worker rechecks grants, actor/approver roles and ticket version.
Ticket mutation and a unique tool receipt commit together, preventing duplicate
local side effects on recovery. External tools need their own idempotency and
sandbox/egress protocols before being added.

## Retrieval and Quality

- `/knowledge/documents`: create/list; delete by ID. When an embedding provider is
  configured, new documents are split into overlapping 1200-character chunks and
  embedded (1536 dimensions). `/knowledge/search` and runs combine tenant-scoped
  pgvector cosine and PostgreSQL full-text chunk rankings with reciprocal-rank
  fusion, returning the best matching chunk for each document. Legacy documents
  without chunks remain available through lexical search. Query embeddings are cached
  per tenant/model/query for seven days. Admins can use
  `POST /knowledge/documents/{id}/embed` to refresh chunks and embeddings after
  configuring the provider. Stored model IDs prevent comparisons across different
  embedding models. SQLite tests retain lexical search only.
- `/prompts`: admin creates immutable version IDs. POST `/prompts/{id}/activate`
  switches the default under a tenant lock and unique-active index. Runs snapshot
  prompt content/version; `builtin-v1` is the fallback.
- `/feedback`: score 1-5 plus a comment on completed runs, one record per user/run.
- `/evaluations`: evaluate completed runs using `required`/`forbidden` phrases and
  optional citation-presence checks. Reports retain specifications and prompt IDs.
  GET `/evaluations/{id}` retrieves them. These reproducible checks are not semantic
  factuality/safety grading or an LLM-as-judge evaluation system.

Treat documents and generated text as untrusted; sanitize Markdown/links in clients.
Source deletion prevents future retrieval but does not erase historical messages,
run snapshots or checkpoints. Implement retention/erasure across those stores before
processing regulated data. Document ingestion embeds the supplied bounded text;
there is no file parser or reranker, and retrieval has no semantic-quality guarantee.
The generic webhook accepts the same validated run request as the API and requires
the caller's JWT and `Idempotency-Key`; provider-specific signature verification is
not included.

`POST /api/v1/runs/batch` accepts at most 10 jobs and creates separately budgeted,
idempotent durable runs. Per-item failures are returned with HTTP 207; accepted runs
remain queued. `WORKER_CONCURRENCY` bounds concurrent worker executions (default 4).
`PROVIDER_MAX_CONCURRENCY` limits simultaneous chat and embedding-provider calls per
process (default 8); it is not a distributed quota across replicas.
Worker Prometheus metrics include `agent_time_to_first_token_seconds` and
`agent_retrieval_duration_seconds`. The cache stores embeddings only, not generated
answers, so budget accounting and conversation state are not bypassed.

Set `"structured_output": true` on a run to request a JSON answer contract with
`answer` and `citations`. The worker validates the completed model response using
Pydantic and rejects citation IDs not present in retrieved tenant documents before
committing the answer. In this opt-in mode, intermediate SSE `token` events contain
the model's provisional JSON text; `structured_response` is emitted only after
validation. Providers are not assumed to implement native JSON-schema mode.

## Audit, Tracing, Alerts and Load

Admin-only `/audit` is cursor-paginated. Core mutations record actor/action/resource
without credentials or prompts. PostgreSQL triggers reject audit UPDATE/DELETE/TRUNCATE
and prompt-content updates. Privileged database administrators can change triggers;
forward audits to immutable external storage where required.

Enable `OTEL_ENABLED=true` for redacted OTLP export. HTTP traces use canonical routes
and status, not bodies/credentials/raw URLs; trace context crosses the queue to worker
`agent.run` spans. JSON logs include trace IDs. Optional local monitoring:

```bash
docker compose --profile agent -f compose.yaml -f compose.observability.yaml up -d
```

Prometheus: `localhost:9090`; Alertmanager: `localhost:9093`; Jaeger: `localhost:16686`.
Recreate API/worker after enabling tracing. Secured metrics require a mounted key
file and the commented Prometheus `http_headers` entry; never commit that key.
Worker metrics stay private. Rules cover API/worker unavailability, error ratio and
p95 latency. Alertmanager's local receiver shows alerts but does not page anyone;
configure an approved external receiver. Jaeger's local all-in-one storage is not
a durable production trace store. Pin/scan infrastructure images in your release process.

No-model load smoke test, with a token supplied by your secure shell/session:

```bash
docker run --rm --network host -e ACCESS_TOKEN -e BASE_URL=http://127.0.0.1:8001 \
  -e VUS=1 -e DURATION=5s -v "$PWD/tests/load:/scripts:ro" \
  grafana/k6:0.57.0 run /scripts/api.js
```

Thresholds fail on API errors/check failures or p95 over one second. Larger load
requires test tenants, adjusted quotas and capacity planning. Tests intentionally
avoid paid model traffic. Configure secret rotation, trusted ingress, backups,
provider alerts and checkpoint/event retention. Password reset, MFA/email verification,
OIDC federation, external-tool sandboxes, semantic evaluators and vector ingestion
remain explicit extension work, not implemented capabilities.