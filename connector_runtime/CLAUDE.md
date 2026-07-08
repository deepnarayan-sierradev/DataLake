# CLAUDE.md — connector_runtime/

Adapter pattern: each source lives under `connector_runtime/adapters/<source>/` and implements
`connector_runtime/interfaces/connector_interface.py::ConnectorInterface`. Current sources:
Salesforce, NetSuite, MySQL RDS, Sage (Intacct + X3 products under `adapters/sage/products/`).

Use `/new-connector` to scaffold a new one — it encodes the checklist below as an executable
prompt. This file is the reference if you're doing it by hand or reviewing someone else's PR.

## Shared base classes — extend these, don't hand-roll a new connector from scratch

- **Credentials**: `connector_runtime/credential_client.py::SecretsManagerCredentialClient` —
  Secrets Manager fetch + TTL cache. All 4 connectors' credential clients subclass or wrap this
  rather than calling `boto3.client("secretsmanager")` directly.
- **Raw layer writes**: `connector_runtime/raw_layer_writer.py::RawLayerWriter`, built on
  `observability/s3_writer.py::S3ParquetWriter`. Override `write_partition_streaming()` only if
  the source has genuinely different semantics (Sage does, for zero-record handling — see its
  subclass docstring for the documented reason, not a silent divergence).
- **Query building**: `connector_runtime/query_builders/incremental_query_builder.py::build_incremental_select()`
  is the shared SQL-text builder for Salesforce SOQL / NetSuite SuiteQL / MySQL. **Do not force a
  non-SQL source through this** — Sage's Intacct/X3 engines build JSON/OData request bodies and
  are intentionally left separate; each carries a "DUP-4 scope note" docstring explaining why. If
  your new source is JSON/OData/GraphQL, follow Sage's pattern instead, don't bend the SQL builder.
- **Error taxonomy**: mark connector-specific exceptions as `TransientConnectorError` or
  `DeterministicConnectorError` (both in `connector_interface.py`) so `classify_extraction_error()`
  can collapse `isinstance` branches into one check. Leave genuinely ambiguous exceptions unmarked
  rather than forcing a wrong classification — see `MySqlIncrementalExtractorError`'s docstring
  for why it's deliberately unmarked (it covers both deterministic and ambiguous failure modes).

## Credentials and tenancy

One Secrets Manager path per source: `edl/sources/{source_id}/credentials` (see README's
Connector Credentials table — not environment-prefixed, since each environment is its own AWS
account already). **Not tenant-scoped today** — every tenant using a given
source-connector type shares one credential set. This is a known, documented gap, not a bug —
`tests/test_tenant_isolation.py` tracks it via a skipped placeholder test rather than a fake pass.
Don't add tenant-scoping to credentials speculatively without checking that test first.

`*_params.py` connector param models use `extra="forbid"` — this is the boundary where
user/config-supplied connector params get validated. Keep new fields explicit rather than
permissive; don't add a catch-all `dict` escape hatch.

## Control plane (`connector_runtime/api/`)

Code-complete — Cognito User Pool + JWT authorizer, 6 REST routes (`connector_runtime/api/control_plane_handler.py`)
— but **not verified against a live AWS deployment**. Specifically unverified: which exact claims
path (`authorizer.claims` vs `authorizer.jwt.claims`) an HTTP API + JWT authorizer actually
populates at payload format 1.0. The handler defensively checks both and fails closed (401)
either way, but don't treat this as battle-tested until it's exercised against real API Gateway.
