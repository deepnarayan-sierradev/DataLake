# DL-04 — Conversational AI Agent Runtime

**SOW clauses:** §6.1, §5.3, §9, §18, §23.2, §23.3 · **Priority:** ⏸ **DEFERRED** · **Owner repo:** DataLake

> ## ⏸ Deferred — separate team, 2026-07-28
>
> This requirement is **out of the current programme's delivery scope** and assigned to a separate
> team. The specification below is complete and remains authoritative — it is not withdrawn, and no
> part of it is superseded.
>
> **Binding on whoever picks it up:** the provider-neutral port
> (`LlmStructuredClient`) must not be bypassed, the agent must consume `SemanticQueryService` only
> and never reach S3/Athena/Glue directly, and the verification loop stays mandatory. Those three
> constraints are what allow this to land additively later without reopening the security model.
>
> **What the remaining programme must not assume:** no concrete LLM adapter exists, so nothing in
> the active scope may depend on model inference being available. Features that would have used it
> (narrative generation, workflow summaries) either degrade gracefully or are dropped — never
> stubbed with a placeholder provider.
>
> Paired experience-layer requirement `EP-07` is deferred in step.

---

## Objective

Make the enterprise AI chat answer real questions. The reasoning shell is built and tested; it
cannot answer anything today because there is no model behind it and no endpoint in front of it.

## Current state (verified 2026-07-28)

Built and tested (`agent/`, 255 LOC across four files):

- `ConversationalAgent.ask()` with a **mandatory verification loop** — schema check → execute →
  ground → self-correct on hallucination → access-denied terminal → "cannot answer" fallback.
- `SemanticRequestProposer` interface and `ModelSemanticRequestProposer`, which grounds the prompt in
  the tenant's semantic model; the compiler re-validates the proposed request.
- `LlmStructuredClient` — an abstract port, deliberately provider-neutral.

Missing:

- **No concrete `LlmStructuredClient` implementation.** `agent/llm_client.py` is 23 lines of ABC.
  A Claude-specific adapter was written and deliberately deleted to keep the port neutral.
- **No API endpoint, no Lambda, no deployable.** The agent is library code nothing calls.
- **No `datalake-agent-sessions-dev` / `datalake-agent-audit-dev` tables** — Terraform intentionally omitted them.
- No conversation memory, no streaming, no cost controls, no provider configuration.

The agent layer was explicitly deferred by the user during the platform-evolution effort. This
requirement un-defers it.

---

## Functional requirements

- **DL-AGENT-01** **Concrete LLM adapter(s)** behind `LlmStructuredClient`. Ship an Amazon Bedrock
  adapter first — it keeps inference inside the customer's AWS account and boundary, which is the
  cleanest answer to SOW §23.2's prohibition on training and cross-customer data combination. A
  direct Anthropic API adapter is a second, configuration-selected implementation. The agent and
  proposer must not import any provider SDK; provider choice is tenant configuration.
  Use the current Claude model generation; do not hardcode a model id in application code — resolve
  it from configuration so a model upgrade is a config change.
- **DL-AGENT-02** **Agent service deployable.** A standalone service (its own Lambda behind the
  control-plane API, or its own ECS service if streaming demands it), never on the ingestion path.
  Routes: `POST /tenants/{tc}/agent/ask` (streamed), `GET/POST /tenants/{tc}/agent/sessions`,
  `GET /tenants/{tc}/agent/sessions/{id}`.
- **DL-AGENT-03** **Session and turn persistence.** `datalake-agent-sessions-dev` and `datalake-agent-audit-dev` tables;
  every turn persists question, resolved semantic request, compiled query, per-check verification
  results, retry count, final answer, and cited sources (FR-3.3).
- **DL-AGENT-04** **Streaming responses** with incremental token delivery and a final verified
  answer. The verification loop runs **before** the answer is committed — never stream an
  unverified claim and retract it.
- **DL-AGENT-05** **Conversation memory** scoped to a session, bounded in turns and tokens, storing
  resolved semantic requests rather than raw result rows.
- **DL-AGENT-06** **Prompt-injection defences.** The proposer's output is a structured semantic
  request re-validated by the compiler — already the core defence. Add: source content is never
  interpolated into the system prompt; user text is delimited and never granted instruction
  authority; the tool surface is fixed to `resolve_semantic_request`, `compile_and_run` (read-only),
  `cite`; the agent cannot select its own tenant scope.
- **DL-AGENT-07** **Cost and abuse controls.** Per-tenant and per-user token budgets, request rate
  limits, and a maximum retry count. SOW §11 forbids customer-facing throttling of included
  capabilities, so these are **abuse and runaway-loop guards with generous defaults**, alarmed
  rather than silently enforced, and never billed.
- **DL-AGENT-08** **Data-use guarantees (§23.2, §23.3).** Provider configuration must assert no
  training on customer data; Bedrock satisfies this by deployment model. Customer data from one
  tenant is never present in another tenant's context — enforced by tenant-scoped model loading and
  tested. Provider API keys in Secrets Manager under the secrets CMK.
- **DL-AGENT-09** **NL query evaluation harness (§9).** A fixture set of natural-language questions
  with expected semantic requests and expected answers, run in CI. Regression in grounding accuracy
  fails the build. This is the only defensible way to claim "AI and natural-language query testing".
- **DL-AGENT-10** **Explainability in every answer**: the answer carries the metrics and dimensions
  used, the filters applied, the row count, the model version, and the freshness timestamp of the
  underlying partition.
- **DL-AGENT-11** **Report generation from a turn**: render the result as a table or narrative
  summary and hand it to the reporting pipeline (EP-06) or export it.

---

## Data model

| Table | Key | Contents |
|---|---|---|
| `datalake-agent-sessions-dev` | PK `tenant_code`, SK `session_id` | user, created/updated, turn count, token usage, TTL |
| `datalake-agent-audit-dev` | PK `tenant_code`, SK `{session_id}#{turn_seq}` | question, semantic request, compiled query hash, verification results, retries, answer, citations, latency, cost |

Agent configuration per tenant at S3 `{tenant_code}/agent-config/{version}.json` — provider, model
id, limits, enabled tools — versioned through the same repository pattern as the semantic model.

No raw result rows are stored in sessions; only references and aggregates.

## Design and patterns

- **Explicit state machine** for the verify → correct → answer loop. Already implemented this way;
  keep it. No free-form agent loop.
- **Port and adapter** for the provider — the existing `LlmStructuredClient` is the port.
- **Strategy** for provider selection resolved from tenant config at request time.
- **Decorator** for cross-cutting concerns on the port: retry, timeout, token accounting, redaction.
  These belong around the adapter, not inside each provider implementation.
- **Circuit breaker** on the provider call — a provider outage must degrade to "cannot answer
  right now", never cascade.
- The agent consumes `SemanticQueryService` only. It has no direct access to S3, Athena, Glue, or the
  serving store. This is the security architecture, not an implementation detail.

## Performance

- Streaming first token target < 2s; full verified answer p95 < 10s on a warm path.
- Semantic result cache (DL-SEM-12) is shared — repeated dashboard-style questions must not
  re-execute.
- Model context is bounded: the semantic model is summarised into the prompt, never serialised whole
  once the model grows past a threshold; entity/metric retrieval is by relevance.
- Provider calls are the dominant latency — retries are capped and run against a cheaper/faster
  model tier for the self-correction pass where accuracy permits.

## Security and OWASP

- **A01** — the agent inherits the caller's access tags; it cannot surface a metric the user cannot
  query. Tenant scope is injected server-side from verified JWT claims, never from the prompt.
- **A03** — no SQL is ever produced by the model. The port's contract states it explicitly and the
  compiler is the only SQL producer.
- **A04** — insecure design is mitigated by the fixed tool surface and the mandatory verification
  loop; an ungrounded answer is a refusal, not a guess.
- **A05** — streaming endpoint requires auth on the initial request; no anonymous SSE.
- **A08** — agent config is hash-verified on load.
- **A09** — every turn, every verification failure, and every access denial is audited with the
  correlation id.
- **A10** — provider endpoints are an allowlist; a tenant cannot point the agent at an arbitrary URL.
- **LLM-specific**: prompt-injection handling (DL-AGENT-06), output validation via the compiler,
  and no agency beyond read — the three controls that matter for this class of system.

## Observability

`AgentTurns`, `AgentVerificationFailures{check}`, `AgentSelfCorrections`, `AgentCannotAnswer`,
`AgentAccessDenied`, `AgentLatencyMs{p50,p95}`, `AgentTokensConsumed{tenant}`,
`AgentProviderErrors`, `AgentCircuitBreakerOpen` — all alarmed.

A rising `AgentCannotAnswer` rate is the leading indicator of semantic-model gaps and must be
reviewed in the weekly operating review (§16).

## Reuse and redundancy

- Reuses `SemanticQueryService`, the compiler, saved queries, the twin read API, the versioned-config
  repository, and the shared handler scaffold. The agent adds no new data access path.
- The provider port is reused by DL-05 (ML narrative generation) and DL-06 (workflow-triggered
  summaries) — one LLM integration for the platform, not three.

## Acceptance criteria

1. A user asks "what was collected revenue by brand last quarter" and receives a grounded, cited
   answer matching the semantic layer's own query result exactly.
2. A question about a metric the user lacks access to returns an access-denied response that does
   not disclose the metric's existence.
3. An induced hallucination triggers self-correction, then "cannot answer confidently".
4. NL evaluation harness green in CI.
5. Provider outage opens the circuit breaker and degrades cleanly.
6. Audit record present for every turn including failures.

## Dependencies

- DL-03 — the agent is only as good as the semantic model; content must land first.
- EP-07 provides the chat UI.
