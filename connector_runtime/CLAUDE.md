# CLAUDE.md — connector_runtime/

Two connector families now live here:

- **Bespoke adapters** — one package per source under `connector_runtime/adapters/<source>/`,
  each implementing `connector_runtime/interfaces/connector_interface.py::ConnectorInterface`:
  Salesforce, NetSuite, MySQL RDS, Sage (Intacct + X3 products under `adapters/sage/products/`).
- **Spec-driven REST adapters** — `adapters/rest_api/` is one shared connector
  (`RestApiConnector`) plus one shared HTTP session (`RestHttpSession`); each of the ten SOW
  sources (HubSpot, MaidCentral, ServMan Pro, WellSky, Housecall Pro, Dialpad, SeniorPlace,
  Google Ads, Google Analytics, Meta Ads) is a **declarative `RestSourceSpec`**, not a new
  connector class. Adding a REST source means registering a spec — see
  `adapters/rest_api/rest_adapter_registration.py`.

Use `/new-connector` to scaffold a bespoke adapter — it encodes the checklist below as an
executable prompt. For a REST/report source, write a spec instead; the substrate is tested in
`connector_runtime/tests/test_rest_api_substrate.py`.

## Shared base classes — extend these, don't hand-roll a new connector from scratch

- **Credentials**: `connector_runtime/credential_client.py::SecretsManagerCredentialClient` —
  Secrets Manager fetch + TTL cache. Every credential client subclasses or wraps this rather than
  calling `boto3.client("secretsmanager")` directly. The *path* comes from
  `connection_credential_resolver.py::ConnectionCredentialPathResolver` — never build it by hand.
- **Raw layer writes**: `connector_runtime/raw_layer_writer.py::RawLayerWriter`, built on
  `observability/s3_writer.py::S3ParquetWriter`. Override `write_partition_streaming()` only if
  the source has genuinely different semantics (Sage does, for zero-record handling — see its
  subclass docstring for the documented reason, not a silent divergence).
- **Query building**: `connector_runtime/query_builders/incremental_query_builder.py::build_incremental_select()`
  is the shared SQL-text builder for Salesforce SOQL / NetSuite SuiteQL / MySQL. **Do not force a
  non-SQL source through this** — Sage's Intacct/X3 engines build JSON/OData request bodies and
  are intentionally left separate; each carries a docstring explaining why forcing them through
  the shared SQL builder would be a leaky abstraction. REST sources don't build SQL at all: the
  endpoint path is the query text and values stay in `query_parameters`.
- **Pagination / rate limiting / sync strategy**: registries, not conditionals —
  `pagination.py`, `rate_limiting.py`, `sync_strategy.py`. A new provider quirk is a registered
  strategy, not a branch in a connector.
- **Error taxonomy**: mark connector-specific exceptions as `TransientConnectorError` or
  `DeterministicConnectorError` (both in `connector_interface.py`) so `classify_extraction_error()`
  can collapse `isinstance` branches into one check. Leave genuinely ambiguous exceptions unmarked
  rather than forcing a wrong classification — see `MySqlIncrementalExtractorError`'s docstring
  for why it's deliberately unmarked (it covers both deterministic and ambiguous failure modes).

## Credentials and tenancy

Credentials are **per connection**, not per source:
`edl/tenants/{tenant_code}/connections/{connection_id}/credentials`, resolved through
`ConnectionCredentialPathResolver`. The legacy shared path `edl/sources/{source_id}/credentials`
is still read as a **fallback with a warning** while environments migrate — run
`make migrate-credentials` (dry-run by default) to populate the per-connection paths, and only
then `--delete-legacy`.

Write-back uses a **separate secret** (`.../credentials-writeback`, `write_back=True` on the
resolver) with no legacy fallback, so a read-only deployment cannot mutate a source.

Isolation is real but not IAM-enforced everywhere yet — `tests/test_tenant_isolation.py` is the
single regression test covering every mechanism, including `TestSecretsManagerConnectionIsolation`
(the old skipped placeholder is gone; it asserts real properties now). Read
`docs/PIPELINE_FLOW.md`'s isolation table before assuming a layer is isolated.

`*_params.py` connector param models use `extra="forbid"` — this is the boundary where
user/config-supplied connector params get validated. Keep new fields explicit rather than
permissive; don't add a catch-all `dict` escape hatch.

## Webhooks and write-back

- `webhook_receiver_handler.py` — signature verification is **mandatory and fails closed**; the
  spec per provider lives in `webhook_signature.py`. Events dedup on the provider event id and
  enqueue to a FIFO queue with `MessageGroupId = tenant#connection#entity`. Nothing is processed
  inline, and no rejection response says *why* verification failed.
- `writeback_handler.py` — gated on the entity's own `writeback_enabled` flag, which is
  deliberately distinct from `active`. Enabling reads must never enable writes.

## Control plane (`connector_runtime/api/`)

Cognito User Pool + JWT authorizer; routes split across
`control_plane_handler.py` (extraction/intelligence) and `config_governance_routes.py`
(config + semantic governance). **Not fully verified against a live AWS deployment**:
specifically unverified is which exact claims path (`authorizer.claims` vs
`authorizer.jwt.claims`) an HTTP API + JWT authorizer populates at payload format 1.0. The
handler defensively checks both and fails closed (401) either way, but don't treat this as
battle-tested until it's exercised against real API Gateway.

**There is deliberately no tenant-, user-, role-, or permission-management route here**, and
adding one is a design error. Identity is owned by the Identity API; this system only consumes a
verified claim. See the "System boundary" section of the root `CLAUDE.md`.
