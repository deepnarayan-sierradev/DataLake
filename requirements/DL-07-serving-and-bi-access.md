# DL-07 — Serving Layer and BI Network Access

**SOW clauses:** §5, §11, §18, §24.4 · **Priority:** P0 · **Owner repo:** DataLake

---

## Objective

Make the analytics data reachable, fast, and safe for the consumption layer — dashboards, reports,
Excel, chat, and any third-party BI tool the customer chooses. Today the serving store is deployed
and correct but physically unreachable, and it holds no data.

## Current state (verified 2026-07-28)

Built and deployed to dev on 2026-07-24:

- `serving_store/` with an adapter+registry pattern and five loaders — MySQL RDS, PostgreSQL, SQL
  Server, Azure SQL, Redshift — behind `ServingStoreLoaderRegistry`.
- **Genuine tenant isolation**: database-per-tenant (MySQL) or schema-per-tenant
  (PostgreSQL/SQL Server/Azure SQL/Redshift), enforced by the database engine's own GRANT model.
  This is the only place in the platform where tenant isolation is enforced by something other than
  application convention.
- Redshift Serverless loads set-based via `COPY` from analytics Parquet with IAM auth (no writer
  password).
- Conditional `LoadServingStore` Step Functions stage, live in dev.

Blocking problems:

1. **`datalake-serving-store-config-dev` is empty** — no tenant/entity onboarded, so the loader skips every run
   and `datalake-serving-store-mysql-dev` holds no databases or tables.
2. **No network path exists for any BI tool.** The RDS instance is `publicly_accessible = false`, in
   private subnets, with a security group whose only inbound rule is the loader Lambda's own SG.
   There is no VPN, PrivateLink, or bastion anywhere in `infrastructure/modules/networking/`.
   Redshift Serverless has the identical gap. (Gap register item 4.)
3. **No mechanism to deliver a tenant its reader credential** once the loader provisions it.
4. **Athena/Glue access is a wildcard grant** across every tenant's data for three configured
   principals (gap register item 5).

---

## Functional requirements

### Network reachability

- **DL-SERV-01** **Establish a BI-reachable network path.** Recommended: **AWS Client VPN with
  per-tenant client certificates**, paired with Power BI On-premises Data Gateway or Tableau Bridge
  running as the VPN client. It keeps the database fully private, matches how these tools already
  reach private data, and supports self-service onboarding. Rejected alternatives and why:
  site-to-site VPN/Direct Connect (too heavy for self-service, viable only for a large tenant that
  already runs one); PrivateLink alone (good for AWS-native tenants, does not help a laptop-based BI
  Desktop connection); public instance with IP allowlists (fastest but widens attack surface and is
  fragile against BI vendors' dynamic egress ranges). PrivateLink may be added later as a
  complement, not a substitute.
- **DL-SERV-02** **Per-tenant credential delivery.** An authenticated API and console flow
  (EP-09) that issues, rotates, and revokes a tenant's read-only serving-store credential. The
  credential is never emailed, never logged, and is retrievable exactly once through a
  time-limited link; rotation is self-service.

### Data availability

- **DL-SERV-03** **Onboard tenants and entities** into `datalake-serving-store-config-dev` so the loader
  actually runs (`scripts/seed_serving_store_config.py` exists; this is execution plus an API for
  EP-04 to drive it).
- **DL-SERV-04** **Serving-layer modelling**: publish denormalised, BI-friendly views — dimensional
  tables plus twin-derived wide views — rather than raw golden-record shapes. A BI tool should not
  need to know the entity-resolution internals. Views are generated from the semantic model so the
  physical serving layer and the governed definitions cannot drift.
- **DL-SERV-05** **Performance for interactive BI (§11).** SOW forbids throttling included
  capabilities, so the serving layer must be sized and indexed for concurrency rather than
  rate-limited: appropriate indexing and partitioning per engine, materialised aggregates for
  dashboard queries, Redshift sort/dist keys aligned to the dominant filter columns, and a
  documented concurrency target.
- **DL-SERV-06** **Incremental serving-store loads.** Today's loader path must be verified to upsert
  incrementally rather than reload full entities as volume grows; Redshift's `COPY` path needs a
  merge strategy (staging table plus `MERGE`) rather than append.

### Query-layer governance

- **DL-SERV-07** **Replace the Athena/Glue wildcard grant** with per-tenant Lake Formation LF-Tags
  or data-cell filters, and add `tenant_code` as a partition column where it is not already the key
  prefix. Closes gap register item 5. Required before a second tenant's data lands in a shared
  environment.
- **DL-SERV-08** **Wire `glue_catalog_database` on the transformation Lambda** so curated-layer
  registration stops being dead code in dev, or explicitly retire the curated-registration path if
  curated is not intended to be Athena-queryable. Either decision is acceptable; the current
  ambiguity is not.

---

## Data model

No new stores. Additions:

- `datalake-serving-store-config-dev` gains `view_definitions_version`, `load_mode` (`full`/`incremental`), and
  `credential_rotation_at`.
- Serving-layer view definitions generated from the semantic model, versioned in S3 at
  `{tenant_code}/serving-views/{version}.sql` per dialect.

## Design and patterns

- **Adapter + registry** — already correct; new engines join the registry.
- **Strategy** per dialect for DDL and merge semantics; Redshift's bulk path is a strategy, not a
  special case in the loader.
- **Template method** for the load lifecycle (prepare → stage → merge → grant → verify).
- Network design favours **least privilege by construction**: the database stays private and the
  client comes to it, rather than the database being exposed and filtered.

## Performance

- Bulk load paths (`supports_s3_bulk_load`) preferred over row upserts wherever the engine supports
  them; row upserts batched and transactional.
- Incremental merge on a staging table, not delete-then-insert.
- Materialised aggregates refreshed as part of the load, so dashboards read pre-aggregated data.
- Loader concurrency bounded per database instance to avoid connection exhaustion.
- Connection pooling in the loader; RDS Proxy considered if Lambda concurrency grows.

## Security and OWASP

- **A01** — per-tenant database/schema with a dedicated read-only role scoped to that container
  only. Already implemented correctly; the requirement is to keep it correct as views are added —
  a view must not span tenants.
- **A02** — credentials in Secrets Manager under the secrets CMK; one-time retrieval; TLS enforced
  on every client connection; encryption at rest on both RDS and Redshift.
- **A05** — no public accessibility; security groups reference SGs, not CIDRs, except the VPN
  client SG; enhanced VPC routing on Redshift retained.
- **A07** — per-tenant VPN certificates, revocable individually; certificate expiry monitored.
- **A09** — connection and query logging enabled and shipped to CloudWatch; credential issuance and
  rotation audited.

## Observability

`ServingStoreLoadRows`, `ServingStoreLoadDurationMs`, `ServingStoreLoadFailures`,
`ServingStoreSkippedNoConfig`, `ServingStoreConnectionErrors`, `VpnClientConnections`,
`VpnCertificateDaysToExpiry`, `ServingQueryLatencyMs`, `ServingConcurrentConnections` — all alarmed.

`ServingStoreSkippedNoConfig` is currently the loader's steady state in dev; it must alarm once a
tenant is expected to be onboarded, so a silent skip is never mistaken for success.

## Reuse and redundancy

- One loader registry for all engines; no per-engine handler.
- Serving views generated from the semantic model — the physical layer and the governed definitions
  share one source.
- Credential issuance reuses the Secrets Manager client and rotation notifier already in the repo.
- The VPN module is shared by every environment; no per-environment network fork.

## Acceptance criteria

1. A BI tool (Power BI Desktop via gateway, or Tableau) connects from outside AWS to a tenant's
   serving-store schema and returns rows.
2. A tenant retrieves its reader credential once through the console and can rotate it.
3. A second tenant cannot see the first tenant's schema — proven by test, not by inspection.
4. Athena wildcard grant replaced; a principal scoped to one tenant cannot query another's tables.
5. Incremental load proven on a large entity; no full reload.
6. `ServingStoreSkippedNoConfig` alarms when a configured tenant produces no load.

## Dependencies

- DL-03 for view generation from the semantic model.
- DL-SEC-01 (IAM tenant boundary) for the Athena/LF work.
- Customer decision on the VPN topology — this is the one requirement here that needs a customer
  answer before build.
