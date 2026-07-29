# CLAUDE.md — connector_runtime/

Two connector families now live here:

- **Bespoke adapters** — one package per source under `connector_runtime/adapters/<source>/`,
  each implementing `connector_runtime/interfaces/connector_interface.py::ConnectorInterface`:
  Salesforce, NetSuite, MySQL RDS, Sage (Intacct + X3 products under `adapters/sage/products/`).
- **Spec-driven REST adapters** — `adapters/rest_api/` is one shared connector
  (`RestApiConnector`) plus one shared HTTP session (`RestHttpSession`); each of the twelve REST
  sources (HubSpot, MaidCentral, ServMan Pro, WellSky, Housecall Pro, Dialpad, SeniorPlace,
  Google Ads, Google Analytics, Meta Ads, **ServiceBridge**, **BePro**) is a **declarative
  `RestSourceSpec`**, not a new connector class. Adding a REST source means registering a spec —
  see `adapters/rest_api/rest_adapter_registration.py`.

**Write the spec from the vendor's document, and assert the document.** On 2026-07-29 an audit
against customer-supplied API documentation found MaidCentral, WellSky and SeniorPlace each
specified against an imagined API — wrong auth kind, wrong paths, wrong envelope, wrong paging,
invented entities. All three passed the entire substrate suite, because a substrate test exercises
a spec against itself and cannot know whether an endpoint exists. `DL-CONN-20` and
`tests/test_documented_source_fidelity.py` are the control: every assertion there restates a fact
from a vendor document, so a spec and its source document must change together. Read
`docs/SOURCE_API_FIDELITY_AUDIT.md` before writing or editing a spec.

Provider vocabulary is spec-declared, not hardcoded in a strategy: `PaginationParameterNames`
(`skipCount`/`maxResultCount`, `_page`/`_count`, …), `record_unwrap_field` (FHIR `entry[].resource`),
`read_method` + `search_body` + `watermark_body_field` (a read that is a POST search),
`api_key_value_prefix` (`Authorization: ApiKey <key>`), `token_endpoint_path` + `token_grant_kind`
(a token shorter than a full sweep — see `rest_token_exchange.py`), and `required_run_parameters`
(a provider-required scope a schedule cannot supply — fails closed as a *configuration* error, not
a retryable 422).

**An entity does not have to be declared here.** `DL-CONN-21`: the configuration console can add
a REST entity this repo has never heard of by supplying `entity_path` (plus optional
`entity_records_json_path`, `entity_watermark_field`, `entity_natural_key_field`,
`entity_pagination_strategy`, `entity_record_unwrap_field`, `entity_read_method`) in the entity's
`connector_params` — the same property Salesforce's `object_name` and MySQL's `table_name` always
had. `resolve_entity_spec()` in `rest_adapter_registration.py` is where a declared entity wins over
configuration, and where an unknown entity with no path fails as a *configuration* error naming
what to supply. A spec's `entities` tuple is the curated set, **not** the extractable set; the
`default_records_json_path` / `default_page_size` fields exist so a console-added entity inherits
that source's conventions.

Config-declared paths are a validated boundary, not an escape hatch: no traversal segment, no
protocol-relative `//`, safe characters only, host allowlist enforced at call time, GET or POST
only — and **write-back is never settable from configuration**, because enabling a read must not
be able to enable a source mutation.

**A rate limit's scope matters as much as its number.** ServiceBridge's quota is documented **per
IP address**, and every Lambda egresses through one NAT address — so its policy is
`shared_across_connections=True`. HubSpot's and BePro's are per token, so theirs are not. Getting
this backwards lets N concurrent extractions each believe they own the whole budget.

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
