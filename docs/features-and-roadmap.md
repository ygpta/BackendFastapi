# Screen APIs and Feature Roadmap

## Platform Update

The gaps below were the pre-platform backlog. Initial bounded implementations now
exist: login/roles/isolation, PostgreSQL rate/spend controls, idempotent runs/tickets,
audits, durable worker/checkpoints, SSE/cancellation, approved allowlisted tools,
lexical RAG, prompt versions, feedback/evaluations, tracing, alerts and k6 tests.
See [the current platform guide](agent-platform.md) for setup, contracts, tested
guarantees and limitations. The original gap analysis below is historical, not
the current implementation status.

## Implemented

The app includes structured JSON logging, sanitized errors, request correlation,
API-key authentication, host/CORS checks, bounded DB pooling, PostgreSQL migrations,
health probes, Docker hardening, and CI. It now also includes two additional
screen backends, request-size protection, and HTTP/process/agent metrics.

Logging is not login: there are no user accounts, browser sessions, JWT login,
roles, or tenant isolation. The shared key represents one trusted service.

## Screen 1: Dashboard

`GET /api/v1/dashboard/summary` returns:

```json
{
  "tickets": {
    "total": 12,
    "open": 5,
    "in_progress": 3,
    "resolved": 3,
    "closed": 1,
    "created_last_24h": 4
  },
  "conversations": 6,
  "messages": 24,
  "chat_enabled": false,
  "generated_at": "2026-10-21T12:00:00Z"
}
```

Counts are computed in one database statement, not from different snapshots.
Chat counts are scoped to the authenticated service identity; tickets are
service-wide, matching the existing access model. `chat_enabled` is configuration,
not proof of provider reachability. Fetch recent tickets separately with
`GET /api/v1/tickets?limit=5` rather than embedding unbounded data in the dashboard.
These exact counts scan records; introduce rollups/caching when scale requires it.

## Screen 2: Ticket Management

`GET /api/v1/tickets` supports:

| Query | Contract |
| --- | --- |
| `limit` | 1-100, default 20 |
| `before_id` | Exclusive positive ID cursor |
| `status` | `open`, `in_progress`, `resolved`, or `closed` |
| `search` | Case-insensitive literal subject substring, maximum 120 characters |
| `customer_reference` | Exact match, maximum 64 characters |

Results are newest ID first:

```json
{
  "items": [
    {
      "id": 42,
      "subject": "Login issue",
      "message": "I cannot log in today.",
      "customer_reference": "CUST-123",
      "status": "open",
      "version": 1,
      "created_at": "2026-10-21T12:00:00Z"
    }
  ],
  "next_cursor": 42
}
```

When `next_cursor` is null there is no subsequent page. Otherwise pass it as
`before_id`, keeping the same filters. `%` and `_` in search are literal characters,
not SQL wildcards. Pagination is stable under inserts, but it is not a frozen
snapshot when other operators change status filters between requests.

Update a ticket using `PATCH /api/v1/tickets/{id}`:

```json
{"status":"in_progress","expected_version":1}
```

The response contains the ticket with its incremented version. A stale version
returns `409`; reload the record and let the operator decide. Missing tickets
return `404`, invalid input `422`. All four statuses can transition to each other,
including reopening; no organization-specific approval policy is implied.

Migration `0003` backfills existing tickets to `open`, version `1`, adds a status
constraint and `(status, id)` index. The create contract remains unchanged;
create/read responses gain additive `status` and `version` fields. Clients that
reject additional response fields must update their response schemas.

## Remaining FastAPI Work

These are explicit gaps, not implemented features:

1. **Identity and authorization (P0 before multi-user exposure):** integrate your
   chosen OIDC provider, verify issuer/audience/signature, enforce roles and tenant
   ownership in every query, and add cross-tenant denial tests. Choosing a provider
   and identity model is required; a homegrown login would not solve this safely.
2. **Distributed rate limits and quotas (P0 before public access):** configure a
   gateway or Redis-backed per-principal limits shared across replicas. Return
   consistent `429`/`Retry-After` responses and test outage/fail-closed policy.
3. **Idempotent writes and audit records (P0 before automated retries):** persist
   keys, request hashes, completion state and retention; reject reused keys with
   different input and recover in-flight work. Store immutable actor/status events.
4. **Operational telemetry (P1):** export redacted OpenTelemetry traces, provision
   Prometheus scraping/alerts and service dashboards, add DB-pool instrumentation,
   and define tested SLOs. Metrics endpoints alone are not a monitoring deployment.
5. **Resilience and release assurance (P1):** add load/soak tests, verified restore
   drills, container/SAST/secret scanning, dependency updates, stricter static type
   gates, and deployment-platform rollout/rollback automation.
6. **Ticket domain features (P2):** assignees, priority, tags, SLA rules, attachments,
   notifications and full-text search. Each needs its own access and data-retention
   contracts; no generic upload endpoint should bypass size/type/malware checks.

## Remaining Agentic AI Work

1. **Durable execution and idempotency (P0 before tool side effects):** add durable
   LangGraph checkpoints, a worker queue and transactional run/outbox records;
   expose run status/cancellation and resume after process failure. Stored history
   is not a checkpoint or exactly-once execution guarantee.
2. **Tool safety and human approvals (P0 before enabling tools):** explicit tool
   allowlists, least-privilege credentials, validated arguments/results, sandboxing,
   network egress rules, approval interrupts, and auditable side effects. A system
   prompt is not an authorization boundary or prompt-injection defense.
3. **Budgets and context control (P1):** enforce token/context limits before calls,
   persist per-user usage and model pricing, cap spend across replicas, and select
   a tokenizer appropriate to the configured model. Current token metrics are
   observability only and cannot enforce spending.
4. **Streaming and cancellation (P1):** SSE event contracts, disconnect handling,
   backpressure and clear final-message persistence semantics. Do not report a
   streamed partial response as a committed turn.
5. **Evaluation and feedback (P1):** version prompts, curate a redacted evaluation
   dataset, test quality/safety/tool behavior, collect authorized feedback, and gate
   releases against regressions. Fake-model tests verify plumbing, not answer quality.
6. **RAG and memory (P2 when domain data is available):** permission-aware ingestion,
   chunking/embeddings, vector search, citations, deletion propagation, retrieval
   evaluations and bounded summaries. Choose document sources and access policy first.
7. **Provider resilience (P2):** circuit breakers, bounded fallback policies,
   consistent usage accounting and model capability checks. Retrying or switching
   providers may cost more and must not replay side effects.

Start with identity/quotas for a public multi-user app, or durable execution and
tool permissions for an autonomous agent. Deployment secrets, provider access,
retention policy and infrastructure choices remain operator-owned decisions.