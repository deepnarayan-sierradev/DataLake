# DL-06 — Workflow Automation Engine

**SOW clauses:** §6.3, §5.2, §12, §18 · **Priority:** P1 · **Owner repo:** DataLake

---

## Objective

A configurable business-process automation engine — triggers, conditions, actions, approvals,
escalation — that non-engineers author and operate. SOW §18 requires a "fully deployed workflow
automation engine"; §6.3 enumerates reporting workflows, data-validation workflows, alerts and
notifications, operational approvals, exception management, and recurring reporting.

## Current state (verified 2026-07-28)

**No general workflow engine exists.**

What exists and is sometimes mistaken for it:

- `orchestration/step_functions/extraction_workflow.py` — the ETL pipeline state machine. This is
  data-pipeline orchestration, not business-process automation. It is not configurable by a business
  user and does not model approvals, assignments, or escalation.
- SNS alerting from the DLQ processor and credential-expiry notifier — point notifications, not a
  rules engine.
- The config service's change-request approve/reject flow (`enterprise-platform`) — **one
  hardcoded** maker-checker workflow for configuration changes, not a general engine.

Everything in §6.3 beyond raw alerting is unbuilt.

---

## Functional requirements

### Engine

- **DL-WF-01** **Declarative workflow definitions**: versioned JSON per tenant describing
  `trigger`, `conditions`, `actions`, `on_failure`, and `escalation`. Authored in the console
  (EP-06), validated at publish, and executed by the engine. No code deploy to add a workflow.
  Workflow definitions adopt the **DL-11** propagation contract: version-bumping publishes, an
  effective-config record, and an execution pinned to one definition version for its whole run — a
  workflow that changes mid-execution is exactly the ambiguity `DL-CFG-01` exists to prevent.
  Reprocessing policy: **apply-forward** (a workflow is an action, not a derived dataset).
- **DL-WF-02** **Trigger types**: schedule (cron/rate), pipeline event (run completed/failed, quality
  gate blocked, reconciliation variance), data condition (a semantic metric crossing a threshold),
  ML signal (anomaly or drift from DL-05), manual invocation, and API/webhook.
- **DL-WF-03** **Condition evaluation** against semantic-layer results — a workflow condition is a
  semantic query plus a comparison, never raw SQL. This keeps thresholds consistent with the
  dashboards showing the same metric.
- **DL-WF-04** **Action types**: send notification (email, SNS, Teams/Slack webhook), generate and
  distribute a report (delegates to EP-06), create an approval task, write an exception record,
  invoke a pipeline run, invoke a connector write-back (DL-CONN-02), call a registered outbound
  webhook, and run a saved query.
- **DL-WF-05** **Human approval tasks**: assignment to a user or role, due date, reminder, escalation
  on breach, approve/reject with comment, and full audit. Approval state is queryable so the console
  can render a task inbox.
- **DL-WF-06** **Exception management**: DL-02 exception records flow into workflows for triage —
  assignment, status transitions, resolution notes, and closure. This is what turns quality findings
  into an operational process rather than a log.
- **DL-WF-07** **Idempotency and exactly-once semantics** on actions with external effects. Every
  action carries an idempotency key derived from `(workflow_id, execution_id, action_id)`; a retry
  never sends a duplicate notification or a duplicate write-back.
- **DL-WF-08** **Execution history**: every execution persists trigger context, evaluated conditions
  with values, actions attempted, outcomes, and duration.
- **DL-WF-09** **Failure handling**: per-action retry with backoff, dead-letter on exhaustion,
  and a circuit breaker per external destination so one dead webhook cannot stall the engine.
- **DL-WF-10** **Workflow test mode**: dry-run evaluates conditions and reports the actions it
  *would* take without performing them. Required for §9 "workflow testing" to be meaningful, and the
  only safe way to let business users author automation against production data.

---

## Data model

| Store | Purpose |
|---|---|
| `datalake-workflow-definitions-dev` (new) | PK `tenant_code`, SK `{workflow_id}#{version}` — definition, status, owner |
| `datalake-workflow-executions-dev` (new) | PK `tenant_code`, SK `{workflow_id}#{execution_id}` — context, results; GSI on status+started_at |
| `datalake-workflow-tasks-dev` (new) | PK `tenant_code`, SK `task_id`; GSI on assignee+status — approval and triage tasks |
| `datalake-workflow-idempotency-dev` (new) | PK `tenant_code`, SK `idempotency_key`, TTL — exactly-once guard |

Definitions above a size threshold hold their body in S3 under
`{tenant_code}/workflows/{workflow_id}/{version}.json` with a hash pointer in DynamoDB, matching the
semantic-model pattern.

## Interfaces

```
GET/POST /tenants/{tc}/workflows
GET/PUT  /tenants/{tc}/workflows/{workflow_id}
POST     /tenants/{tc}/workflows/{workflow_id}/validate
POST     /tenants/{tc}/workflows/{workflow_id}/publish
POST     /tenants/{tc}/workflows/{workflow_id}/run          manual trigger
POST     /tenants/{tc}/workflows/{workflow_id}/dry-run
GET      /tenants/{tc}/workflows/{workflow_id}/executions
GET      /tenants/{tc}/tasks?assignee=…&status=…
POST     /tenants/{tc}/tasks/{task_id}/approve|reject
```

## Design and patterns

- **Interpreter** over the declarative definition; the engine is a small evaluator, not a code
  generator.
- **Registry** for trigger types and action types — a new action is a registration implementing
  `WorkflowAction`, mirroring the connector and serving-store registries.
- **Chain of responsibility** for condition evaluation with short-circuit.
- **Command** for actions, each with an `execute` and an idempotency key, so retry semantics live in
  one place.
- **Circuit breaker** per external destination.
- **Step Functions as the execution substrate** for long-running or approval-bearing workflows —
  approvals need waits measured in days, which is exactly the task-token pattern Step Functions
  provides. Short workflows execute inline in Lambda. Do not build a bespoke durable executor.
- Explicitly **not** a general scripting or expression language with arbitrary code execution — the
  condition grammar is a closed set of comparisons over semantic results. This is a security
  decision as much as a design one.

## Performance

- Event-driven triggers consume from EventBridge and SQS; the engine never polls for pipeline events.
- Scheduled evaluation batches all workflows sharing a schedule into one evaluation pass, so a
  hundred workflows on the same cron do not become a hundred concurrent executions.
- EventBridge schedules use a **flexible time window** — this closes gap register item 15 (zero
  jitter today) and prevents a thundering herd as workflow count grows.
- Condition queries reuse the semantic result cache.
- Fan-out actions (notify 200 franchisees) are queued and rate-limited per destination, never
  executed in a single invocation.

## Security and OWASP

- **A01** — workflow definitions are tenant-scoped; a workflow cannot read or act on another
  tenant's data. Actions execute under the workflow owner's effective permissions, not an
  elevated service identity.
- **A03** — conditions compile through the semantic layer; there is no raw SQL and no expression
  `eval`.
- **A04** — no arbitrary code execution by design; the closed action registry is the control.
- **A05** — outbound webhook destinations are an allowlist per tenant, signed with a per-destination
  secret.
- **A08** — definition bodies hash-verified on load; publish is maker-checker for workflows with
  external-effect actions (notifications, write-back).
- **A09** — every execution, action, approval, and rejection is audited with actor and correlation
  id.
- **A10** — SSRF is mitigated by the destination allowlist plus egress restriction; no
  user-supplied URL is called directly.

## Observability

`WorkflowExecutions{status}`, `WorkflowConditionEvaluations`, `WorkflowActionsExecuted{type}`,
`WorkflowActionFailures{type}`, `WorkflowTasksOpen`, `WorkflowTaskAgeHours`,
`WorkflowEscalations`, `WorkflowCircuitBreakerOpen`, `WorkflowDlqDepth` — all alarmed.

An ageing approval-task backlog is an operational signal that belongs on the platform-operations
dashboard, not only in the console.

## Reuse and redundancy

- The engine subsumes the existing bespoke alerting paths: the DLQ processor's SNS alert and the
  credential-expiry notifier become **workflow definitions**, not standalone Lambdas. Retire the
  bespoke paths once parity is proven — do not run both.
- The config service's change-request approval flow (`enterprise-platform`) migrates onto this
  engine's task model rather than remaining a second, parallel approval implementation.
- Notification rendering is shared with EP-06 report distribution — one templating and delivery
  path.
- Reuses the semantic layer, the exception repository from DL-02, the versioned-config repository,
  and the shared handler scaffold.

## Acceptance criteria

1. A business user authors, dry-runs, publishes, and executes a workflow with no code deploy.
2. A quality-gate block raises an exception, creates a triage task, notifies the owner, and
   escalates on breach of the due date.
3. A metric-threshold workflow fires on a real semantic condition and distributes a report.
4. An induced duplicate trigger produces exactly one notification (idempotency proven).
5. A dead webhook destination opens its circuit breaker without affecting other workflows.
6. The DLQ-alert and credential-expiry Lambdas are retired in favour of workflow definitions.

## Dependencies

- DL-03 (semantic conditions), DL-02 (exception records), DL-05 (anomaly triggers), EP-06 (report
  actions and the authoring console).
