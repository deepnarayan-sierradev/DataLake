---
description: Scaffold a new source connector following this repo's existing adapter pattern
---

Scaffold a new connector for source `$1` (if not given, ask the user for: the source name; is it
SQL-like — build on `incremental_query_builder.py` — or a JSON/OData/GraphQL API — build a
source-specific request builder like Sage's; does it need OAuth or static credentials).

Before writing anything, read one existing connector fully as your reference implementation —
Salesforce (`connector_runtime/adapters/salesforce/`) for an OAuth+SOQL source, or Sage
(`connector_runtime/adapters/sage/`) for a source with multiple products or non-SQL query bodies.
Also read `connector_runtime/CLAUDE.md` for the shared-base-class conventions.

Files to create, mirroring that reference's structure exactly:

1. `connector_runtime/adapters/<source>/<source>_connector.py` — implements `ConnectorInterface`
   (`connector_runtime/interfaces/connector_interface.py`); registers itself via
   `connector_registry.register_builder(...)` and `connector_registry.register_params_model(...)`
   at the bottom of the module (copy the exact pattern from any existing connector's last ~5 lines).
2. `connector_runtime/adapters/<source>/<source>_auth_client.py` (or `_credentials_client.py`) —
   subclass/wrap `connector_runtime/credential_client.py::SecretsManagerCredentialClient`. Do not
   hand-roll Secrets Manager calls.
3. `connector_runtime/adapters/<source>/<source>_raw_layer_writer.py` — subclass
   `connector_runtime/raw_layer_writer.py::RawLayerWriter`. Only override
   `write_partition_streaming()` if this source has genuinely different zero-record or chunking
   semantics, and document why (follow Sage's example).
4. Query/request builder: if SQL-like, call `incremental_query_builder.py::build_incremental_select()`
   directly rather than writing a new builder. If not, write a source-specific builder and add a
   short docstring note explaining why it doesn't use the shared SQL builder (match Sage's
   "DUP-4 scope note" convention).
5. `connector_runtime/adapters/<source>/<source>_params.py` — Pydantic model with
   `extra="forbid"`, following an existing `*_params.py` file's structure.
6. Error classes: mark them `TransientConnectorError` or `DeterministicConnectorError` from
   `connector_interface.py`. Only leave an exception unmarked if it's genuinely ambiguous, and
   document why (see `MySqlIncrementalExtractorError`'s docstring for the precedent).
7. Terraform: add the credential secret to `infrastructure/modules/secrets/main.tf`, following the
   `sage_intacct_credentials` resource as a template, with the `{environment}/sources/<source>/credentials`
   naming convention. Mirror the change into `infrastructure/environments/{dev,staging,prod}/main.tf`
   if new module variables are introduced.
8. Tests: `connector_runtime/tests/<source>/test_<source>_connector.py` etc., one test file per
   new module, using `moto.mock_aws`. No shared fixtures/conftest — this repo's tests are
   self-contained per-file. **Register the new test directory in `pyproject.toml`'s `testpaths`**
   (and `known-first-party` / coverage `source` if it's a new top-level package) or it silently
   never runs in CI — this exact gap existed for `analytics_publisher/tests` and
   `connector_runtime/tests/sage` before being caught.

After scaffolding, run `/verify` before considering the connector done. Do not report connector
onboarding as complete without a green mypy/ruff/pytest pass on every new file.
