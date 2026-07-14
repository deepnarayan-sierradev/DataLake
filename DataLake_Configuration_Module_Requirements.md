# Data Lake Configuration Module — Requirements & Architecture

Status: Draft, pending review. Frontend-integration details are placeholders until the existing
frontend repo is available for reference (see [Open Items](#open-items)).

**Repo provenance note (read this before moving/using this doc elsewhere):** this document was
authored inside, and describes, the `DataLake` repo. It is meant to travel with you into the new
`datalake-config-service` repo and the existing frontend repo, but it does **not** move the code it
references. Every relative file path in this document (`contracts/identifier_policy.py`,
`connector_runtime/api/control_plane_handler.py`, `docs/KNOWN_GAPS_AND_ROADMAP.md`,
`infrastructure/modules/...`, `scripts/seed_*.py`, `docs/PLATFORM_STATUS.md`) refers to a location
**inside the `DataLake` repo**, not the repo this document currently sits in. Once the new backend
repo depends on the published `edl_shared_contracts` package (§4), it no longer needs source access
to `DataLake` at runtime — but a human or a Claude Code session working in that repo will still need
a pointer back to `DataLake` (absolute local path or git remote URL) to consult anything this
document doesn't fully capture, especially `docs/PLATFORM_STATUS.md`, which is that repo's living
source of truth for canonical resource names and drifts faster than this document will. Put that
pointer in the new repo's own `CLAUDE.md`, not just in this file — a fresh Claude Code session run
from a different working directory does not automatically see `DataLake`'s `CLAUDE.md` or its
memory, only what's physically present (or explicitly pathed to) from its own directory.

## 1. Background

This repo (`DataLake`) is a metadata-driven, connector-based multi-tenant data lake — its scope is
extract/load/transform, orchestrated via Lambda/Step Functions/DynamoDB/S3/Glue/Athena. It does not
own tenant/user/role/permission management — that already lives in a separate, existing Identity
API, part of a larger application (React frontend, mixed .NET/Python backend).

Today, tenant-specific configuration (field mappings, entity resolution rules, connector/entity
selection, source credentials, extraction schedules) is set up manually — via one-off seed scripts
(`scripts/seed_entity_config.py`, `scripts/seed_schedules.py`, `scripts/seed_field_mappings.py`,
`scripts/seed_entity_resolution_configs.py`) and direct `aws secretsmanager put-secret-value` calls.
There is a code-complete but **unused** SaaS control-plane API in this repo
(`connector_runtime/api/control_plane_handler.py`, Lambda `EdlControlPlane`, API Gateway
`EdlControlPlaneApi`) that was built toward self-service tenant/entity onboarding — confirmed via
direct AWS CloudWatch/API Gateway inspection (2026-07-10) to have **zero invocations and zero log
streams** since deployment on 2026-07-09. Nothing depends on it today.

This document defines the requirements for a new, dedicated module that replaces the manual/seed-
script process with self-service, UI-driven configuration, gated by fine-grained permissions issued
by the existing Identity API.

## 2. Goals

- Let a tenant user with sufficient data-lake permissions configure, without engineering
  involvement:
  - (a) Field mappings for their tenant
  - (b) Entity resolution and survivorship rules
  - (c) Which entities (of their configured connectors) are active
  - (d) Source credentials (Salesforce, MySQL RDS, NetSuite, Sage, and future connectors)
  - (e) Extraction cron schedules per entity
- Enforce all of the above via granular, per-capability permissions (not a single "has data lake
  access" flag), issued through the existing Identity API's role/permission system.
- Keep this repo scoped to extract/load/transform only — configuration lives in a new, separate
  small backend project, not in `connector_runtime/api/`.
- Reuse the existing frontend application and its design system — this is a new module inside
  that app, not a separate product.

## 3. Non-Goals (this phase)

- Changes to the Identity API's own tenant/user creation flows — out of scope; this module only
  consumes JWTs it issues.
- Retiring `connector_runtime/api/control_plane_handler.py` now — see [§9](#9-disposition-of-the-existing-control-plane-api).
- UI visual design/wireframes — functional requirements only; visual design happens once the
  frontend repo is available.
- Historical migration of already-seeded tenant config (e.g. today's Salesforce/MySQL RDS config in
  dev) into the new module's write path — assumed to be a one-time backfill task done separately.

## 4. Architecture Overview

| Concern | Decision |
|---|---|
| Backend repo | New, standalone repo (e.g. `datalake-config-service`) — not part of this `DataLake` repo |
| Backend language/framework | Python 3.14, FastAPI + Pydantic v2 (`extra="forbid"` on all request/response models, matching this repo's existing convention) |
| Frontend | New module/routes added inside the existing React SPA (not a separately-deployed frontend) |
| Deployment | Docker image built and deployed to ECS, matching how the platform's other modules are deployed |
| Auth | Identity API issues a JWT carrying `tenant_code` + granular data-lake permission claims; new backend verifies it locally via JWKS (no per-request callback to Identity) |
| Data access | New backend writes **directly** into this repo's existing DynamoDB tables and Secrets Manager — no separate config datastore, no sync/event layer |
| Shared tenant-scoping logic | This repo publishes `contracts/identifier_policy.py` and the relevant `*RepositoryClient` classes as a versioned package to **AWS CodeArtifact**; the new backend depends on it rather than reimplementing tenant-key construction |

### Why direct writes + a shared package (not a separate datastore)

This repo's tenant isolation has a real history of subtle bugs from hand-rolled key construction
(13+ confirmed tenant-collision fixes across S3 keys, DynamoDB keys, schedule names, and more —
see `docs/PIPELINE_FLOW.md`'s canonical isolation model and `docs/KNOWN_GAPS_AND_ROADMAP.md` for
what's still open). The fix now lives in exactly one place —
`contracts/identifier_policy.py`'s `TENANT_CODE_PATTERN`/`tenant_scoped_key()` and each domain's
`*RepositoryClient`. The new backend must depend on that logic as a library, not re-derive it,
or it risks reintroducing the same bug class independently.

### Why AWS CodeArtifact over a pinned git dependency

The new backend is a Docker image built for ECS — CodeArtifact gives a normal `pip install` inside
the image build (one `aws codeartifact login` step), proper semver versioning, and a clean upgrade
path as `identifier_policy.py`/repository clients evolve. A git dependency would need SSH/PAT
credential management inside the container build and manual re-pinning on every change — worse for
a package expected to be consumed by more than one service over time.

## 5. Permission Model

Permissions are granular per configuration surface, issued as claims in the Identity API's JWT.
Proposed permission strings (final naming to be confirmed with the Identity API owner):

- `datalake:field_mapping:read` / `:write`
- `datalake:entity_resolution:read` / `:write`
- `datalake:entity_selection:read` / `:write`
- `datalake:credentials:read` / `:write`
- `datalake:schedule:read` / `:write`
- `datalake:config:approve` — required to approve a maker-checker change request (§7)

Each API route declares its required permission; the auth layer verifies the JWT signature via
JWKS, extracts `tenant_code`, and checks the specific permission claim (extending this repo's
existing `_authorize_path_tenant` pattern from tenant-only matching to tenant+permission matching).

## 6. Configuration Capabilities (v1)

### 6a. Field Mapping
Create/edit/save field mappings per tenant, per connector entity — maps source fields to the
platform's canonical schema.

### 6b. Entity Resolution & Survivorship
Configure match rules and survivorship policy per entity type/tenant (which source wins on
conflicting field values, match key definitions).

### 6c. Entity Selection
Select which entities, from the tenant's configured connectors, are active for extraction.
Supersedes `POST/GET /tenants/{tenant_code}/entities` in the dormant control-plane API (§9).

### 6d. Source Credentials
Set up credentials for Salesforce, MySQL RDS, NetSuite, Sage, and future connectors. Writes to
Secrets Manager under the existing `edl/sources/*` credential path convention.

### 6e. Extraction Schedule
Configure a cron/interval schedule per entity, driving the same EventBridge Scheduler mechanism
this repo's pipeline already uses.

## 7. Additional Features (v1)

- **Config versioning / audit trail** — every save writes to a new `config-audit-log` table
  (`tenant_code`, `config_type`, `entity_id`, `version`, `changed_by`, `changed_at`, `diff`),
  transactionally alongside the config write itself.
- **Dry-run / validate before save** — a `POST /{resource}/validate` endpoint per config type,
  running the same Pydantic validation plus a live schema-introspection check against the
  connector, without persisting.
- **Draft vs. published state** — every config item carries `status: "draft" | "published"` and
  `version: int`. Downstream effects (EventBridge schedule updates, credential rotation taking
  effect) only fire on publish, not on save.
- **Maker-checker approval** — for credentials and schedules specifically (highest blast radius if
  wrong), changes go into a `config-change-requests` table (`requested_by`, `approved_by`,
  `status`) and require `datalake:config:approve` from a second user before publishing.
- **Per-tenant limits** — a `tenant-limits` item per tenant (max entities, max connectors, minimum
  schedule interval), enforced at write time to prevent one tenant's config from overloading shared
  infrastructure.

## 8. Data Model Additions (this repo)

New DynamoDB tables/attributes needed to support §7, provisioned via this repo's Terraform (in
`infrastructure/modules/`, following the existing per-table module pattern):

| Table | Key | Purpose |
|---|---|---|
| `config-audit-log` | `tenant_code` (partition), `changed_at#config_type#entity_id` (sort) | Versioned change history for every config write |
| `config-change-requests` | `tenant_code` (partition), `request_id` (sort) | Maker-checker queue for credential/schedule changes |
| `tenant-limits` | `tenant_code` (partition) | Per-tenant caps on entities/connectors/schedule frequency |

Existing config tables (entity config, field mappings, entity resolution config, schedules) gain a
`status` and `version` attribute; no key-structure changes.

## 9. Disposition of the Existing Control-Plane API

Confirmed dormant (zero invocations/logs since 2026-07-09), so there is no live-traffic migration
risk either way. Split:

- **Redundant, to be removed in a follow-up cleanup PR** (after the new module is built and
  verified end-to-end): `POST /tenants` (tenant creation — was always redundant; the Identity API
  owns this) and `POST/GET /tenants/{tenant_code}/entities` (entity registration — superseded by
  §6c).
- **Not redundant, stays in this repo**: `POST /tenants/{tenant_code}/pipelines/trigger` and
  `GET /tenants/{tenant_code}/runs...` — these trigger/monitor actual pipeline execution, which is
  this repo's core extract/load/transform responsibility, not tenant configuration. The new
  module's UI may call these to let a tenant admin trigger a manual run or check status, but the
  implementation stays here.

No deletion happens as part of this work — tracked as a follow-up once the new module is live.

## 10. API Surface (new backend)

Routers, one per capability, each exposing standard CRUD plus a `/validate` dry-run endpoint:

```
POST/GET/PUT   /tenants/{tenant_code}/field-mappings/{entity_id}
POST           /tenants/{tenant_code}/field-mappings/{entity_id}/validate
POST/GET/PUT   /tenants/{tenant_code}/entity-resolution/{entity_id}
POST/GET/PUT   /tenants/{tenant_code}/entities                        # selection (§6c)
POST/GET/PUT   /tenants/{tenant_code}/credentials/{source_id}
POST/GET/PUT   /tenants/{tenant_code}/schedules/{entity_id}
POST           /tenants/{tenant_code}/{resource}/{id}/publish          # promotes draft -> published
GET            /tenants/{tenant_code}/audit-log
POST/GET       /tenants/{tenant_code}/change-requests
POST           /tenants/{tenant_code}/change-requests/{id}/approve
```

## 11. Deployment

- Docker image, built via CI, deployed to ECS — matching this platform's other modules.
- JWT verification happens in application code (FastAPI dependency), independent of whatever sits
  in front of the ECS service (ALB/API Gateway) — no dependency on gateway-level auth.
- IAM task role scoped to exactly the DynamoDB tables/Secrets Manager paths this service needs to
  write (new work — `docs/KNOWN_GAPS_AND_ROADMAP.md` notes this repo's own IAM access is currently
  unscoped wildcard for S3/Secrets Manager/DynamoDB alike; the new service's role should not repeat
  that pattern).

## Open Items

- Frontend integration specifics (routing conventions, component library, auth context/token
  handling) — pending access to the existing frontend repo.
- Final permission-string naming — needs sign-off from whoever owns the Identity API's
  role/permission definitions, since new permissions must be creatable there.
- Exact ECS topology (cluster, task definition, ALB vs. existing ingress pattern) — implementation
  detail for the new repo's own Terraform, to be settled when that repo is scaffolded.
- Backfill of already-seeded dev tenant config into the new tables/attributes (`status`/`version`)
  — one-time task, not yet scoped.
