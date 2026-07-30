# DL-08 — Security, Tenant Isolation and Access Control

**SOW clauses:** §8, §9, §23.4, §23.5, §23.7 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Convert tenant isolation from an application-level naming convention into an enforced security
boundary, and deliver the department-, executive-, and franchise-level access controls SOW §8
requires.

## Current state (verified 2026-07-28)

Solid foundations: KMS CMKs per data class; Cognito user pool with a JWT authorizer; tenant-path
authorization on every control-plane route; data classification with PII and SENSITIVE_PII masking
(SENSITIVE_PII correctly uses `FULL_MASK`, not the previously-reversible unsalted hash); OWASP
categories cited across 150+ code comments; `bandit`, pre-commit, CODEOWNERS, and CI gates;
`tests/test_tenant_isolation.py` covering every isolation mechanism with a deliberately skipped
placeholder for the known Secrets Manager gap.

Open gaps, all verified against source:

| # | Gap | Evidence |
|---|---|---|
| 1 | **No IAM-enforced tenant boundary anywhere.** S3/DynamoDB/Secrets policies scope to resource ARN only; no `Condition` block ties a principal to its tenant | `infrastructure/modules/iam/main.tf` |
| 2 | Secrets Manager holds one shared credential per connector type, not per tenant | `datalake/<env>/sources/{source_id}/credentials` |
| 5 | Glue/Athena wildcard grant across all tenants for 3 principals | `infrastructure/modules/glue/main.tf`, dev `terraform.tfvars` |
| 6 | ~~`POST /tenants` accepts any authenticated caller — no admin authorization~~ **Closed by deletion 2026-07-28**: the route does not belong in this system (see DL-SEC-12) | `control_plane_handler.py` |
| 7 | No WAF anywhere in the repo; no rate limiting on the control plane | no `aws_wafv2` resource exists |
| 8 | Control-plane auth never exercised end-to-end against the deployed Cognito pool | — |
| 10 | Lineage and quality-report S3 keys carry no `tenant_code` segment | `governance/lineage_record.py` |
| 19 | Credential rotation wired in Terraform but never activated; only expiry *notification* exists | `infrastructure/modules/secrets/variables.tf` |

Not built at all: role hierarchy, department/executive/franchise access tiers, row-level security,
access administration, SOC 2, formal incident response, BCDR.

---

## Functional requirements

### Tenant boundary

- **DL-SEC-01** **IAM-enforced tenant isolation.** Per-tenant IAM roles, or resource-tag and
  prefix `Condition`s across S3, DynamoDB, and Secrets Manager, so a path-construction bug or a
  compromised dependency cannot cross tenants. Phase carefully so the existing default tenant does
  not break: add conditions in audit mode, verify with CloudTrail that no legitimate access would be
  denied, then enforce. This is the platform's single largest security gap and the prerequisite for
  a credible multi-tenant claim.
- **DL-SEC-02** **S3 bucket-policy conditions** binding each principal to its `{tenant_code}/`
  prefix, turning today's write-path convention into a boundary.
- **DL-SEC-03** **Tenant-scoped keys everywhere.** The `datalake-entity-extraction-config-dev` migration to
  `tenant_scoped_key()` is written but **`scripts/migrate_entity_config_to_tenant_scoped_key.py`
  must be run with `--apply` against each environment before the new code deploys**, or existing
  configs go dark. Treat this as a release-blocking migration step, not a background task.
- **DL-SEC-04** **Tenant-prefix lineage and quality reports** (gap 10) — add `{tenant_code}/` to
  `governance/lineage_record.py` and the transformation quality-report writer, matching every other
  layer.

### Credentials

- **DL-SEC-05** **Per-tenant credential paths**: `datalake/<env>/tenants/{tenant_code}/sources/{source_id}/credentials`,
  with a migration for the existing shared entries and removal of the skipped placeholder test in
  `tests/test_tenant_isolation.py` once real coverage exists.
- **DL-SEC-06** **Activate credential rotation** (gap 19) — implement the per-connector rotation
  Lambda for at least the sources that support programmatic credential reset, and set the
  `rotation_lambda_arn` variables that exist but are never populated. Where a source has no reset
  API, document it and keep expiry notification as the compensating control.
- **DL-SEC-07** **Least-privilege runtime roles** per Lambda, reviewed against actual CloudTrail
  usage rather than against intent.

### Access control (§8)

- **DL-SEC-08** **Role model.** Platform roles (platform admin, tenant admin, data steward, analyst,
  viewer) plus tenant-defined roles, expressed as granular capabilities rather than a single
  access flag — mirroring the capability scheme the config service already uses
  (`datalake:field_mapping:write` style). Roles carry into JWT claims and are enforced at every API.
- **DL-SEC-09** **Data access permissions** at metric, dimension, and entity level via the semantic
  layer's access tags (already implemented in the compiler) — this requirement is to define and
  populate the tag taxonomy, not to build the mechanism.
- **DL-SEC-10** **Department- and executive-level controls**: access tags map to departments
  (Finance, Operations, Sales & Marketing) and to an executive tier that spans them. A Sales analyst
  cannot query AP bills; an executive can see all departments for their brands.
- **DL-SEC-11** **Franchise- and brand-level row security.** The highest-value control in this
  document for this customer: a franchisee sees only their own locations' rows; a brand manager sees
  only their brand. Implemented as a **row-level security predicate injected server-side by the
  query compiler** from verified JWT claims, plus database-level RLS or per-tenant views in the
  serving store for direct BI connections. The predicate is never caller-supplied and is applied
  before any other filter.
- **DL-SEC-12** ~~Admin authorization on tenant provisioning (gap 6) — an admin-scoped claim
  required for `POST /tenants`.~~ **WITHDRAWN 2026-07-28.** Tenant provisioning is not this
  system's concern: tenants, users, roles, and permissions are owned by the **Identity API**,
  which `enterprise-platform` is built on. The DataLake is a standalone, configuration-driven
  processing system — it consumes a verified tenant claim and never creates a tenant. The
  `POST /tenants` route, its request model, and the platform-admin capability check have been
  removed from `connector_runtime/api/control_plane_handler.py`, and a test asserts the route's
  absence so it is not re-added. Gap register item 6 is therefore closed by **deletion**, not by
  adding authorization. The equivalent requirement belongs to the Identity API / EP-RBAC track.

### Perimeter and assurance

- **DL-SEC-13** **WAF on the control plane** (gap 7) — a `aws_wafv2` module with AWS managed rule
  sets and per-IP and per-tenant rate limiting, associated with the existing API Gateway.
  §11's no-throttling clause applies to included platform capabilities, not to abuse protection;
  set generous limits and alarm before blocking.
- **DL-SEC-14** **Exercise the live authentication path end-to-end** (gap 8) — a real login and
  token round-trip against the deployed Cognito pool, confirming which claims shape API Gateway
  populates. The handler defensively checks both `authorizer.claims` and `authorizer.jwt.claims`
  and fails closed, but the assumption is unverified against a real deployment.
- **DL-SEC-15** **Security and access testing (§9)**: an automated authorization test matrix
  (role × resource × action) run in CI, plus an external penetration test before production
  go-live.
- **DL-SEC-16** **Incident response (§23.5)**: documented procedures for identification,
  investigation, containment, and remediation; a 72-hour customer-notification runbook with the
  six content elements §23.5 enumerates; and a rehearsed tabletop. `docs/PRODUCTION_INCIDENT_RUNBOOK.md`
  covers operational incidents — this extends it to security incidents specifically.
- **DL-SEC-17** **SOC 2 Type II readiness (§23.7)**: control mapping, evidence collection
  automation, and a subprocessor register (§23.6). Certification itself is a business process; the
  engineering obligation is producing auditable evidence continuously rather than at audit time.
- **DL-SEC-18** **Vulnerability management (§23.4)**: dependency scanning in CI with a defined
  remediation SLA by severity, container image scanning, and infrastructure drift detection.
  Includes clearing the 72–73 pre-existing `mypy` errors that keep the CI typecheck job red — a
  permanently-red gate trains everyone to ignore gate failures, which is itself a security risk.

---

## Design and patterns

- **Policy as data**: capabilities, roles, and access tags are configuration validated at publish,
  not conditionals in handlers.
- **Server-side predicate injection** for row-level security — the compiler is the single
  enforcement point, matching how tenant scope is already injected.
- **Defence in depth**: application access tags *and* IAM conditions *and* database GRANTs. Any one
  failing must not expose data.
- **Fail closed** everywhere; the existing handlers already do this and it must remain true.
- Deliberately **not** a per-tenant AWS account model — the operational cost outweighs the benefit
  at the expected tenant count, and per-tenant IAM roles achieve the boundary.

## Performance

- Authorization decisions are computed from JWT claims in-process; no per-request policy lookup.
- Role and capability resolution is cached with a short TTL and explicit invalidation on change.
- Row-level predicates are pushed into partition pruning where the predicate column is a partition
  key (brand), so security and performance align rather than conflict.
- Per-tenant IAM roles are assumed once per execution, not per operation.

## Observability

`AuthenticationFailures`, `AuthorizationDenials{capability}`, `AdminActions`,
`CrossTenantAccessAttempts`, `WafBlockedRequests{rule}`, `CredentialRotationAge{secret}`,
`RowLevelPredicateApplied`, `SecretRetrievalFailures` — all alarmed.

`CrossTenantAccessAttempts` must page. Any non-zero value is either an active attack or a bug that
would have leaked data before DL-SEC-01 landed.

Security events route to a dedicated log group with extended retention, separate from application
logs, to support incident forensics and SOC 2 evidence.

## Reuse and redundancy

- One identifier policy (`contracts/identifier_policy.py`) — never re-derive tenant regexes.
- One capability model shared between the DataLake control plane and the enterprise-platform config
  service; do not maintain two permission vocabularies for one product.
- One row-level predicate builder shared by the semantic compiler, the serving-store view generator,
  and the agent.
- `tests/test_tenant_isolation.py` remains the single cross-cutting regression test; every new
  isolation mechanism adds a case there rather than a parallel test file.

## Acceptance criteria

1. A principal scoped to tenant A is denied by IAM — not only by application code — when attempting
   to read tenant B's S3 prefix, DynamoDB item, or secret. Proven by test.
2. A franchisee user queries a shared dashboard and sees only their locations' rows.
3. A Sales-department user cannot query finance metrics through the API, the agent, or a direct
   serving-store connection.
4. `POST /tenants` is not routed by this system at all; a tenant-scoped route still rejects a
   request carrying no verified claim, and rejects one whose claim names a different tenant.
5. WAF blocks a simulated attack pattern; rate limiting alarms before it blocks legitimate traffic.
6. Full authorization test matrix green in CI; external penetration test completed with findings
   remediated or accepted.
7. CI typecheck job green.
8. Secrets Manager isolation placeholder test in `tests/test_tenant_isolation.py` replaced with a
   real assertion.

## Dependencies

- DL-SEC-01 should precede DL-05 (tenant-scoped training roles) and DL-SERV-07 (Lake Formation).
- DL-SEC-03 migration must run before the tenant-scoped-key code deploys to each environment.
