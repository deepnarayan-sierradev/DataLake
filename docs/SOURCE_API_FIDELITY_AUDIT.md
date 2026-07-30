# Source API Fidelity Audit — 2026-07-29, second pass 2026-07-30

---

## Second pass (2026-07-30) — what the first pass still got wrong

The first pass was asked to be re-checked "so there is no room for issues, gaps, timeout,
rate limit or any other threshold." Re-reading every artifact against the shipped code found
five further classes of defect. Two of them would have caused **silent** wrong behaviour,
which is the category that matters most.

### 1. Every hand-sized token bucket could breach its documented limit

`capacity` is an *instantaneous* burst that **adds** to whatever the bucket refills during a
window. The quantity a vendor caps is `capacity + refill x window`. Four registered policies
were over — three from this programme and HubSpot's, which predates it:

| Policy | Documented | Worst case it permitted |
|---|---|---|
| `maid-central-hourly` | 100 / 60 s | **113** in 60 s |
| `servicebridge-shared-ip` | 50 / 1 s | **53** in 1 s |
| `dialpad-standard` | 20 / 1 s | **36** in 1 s |
| `hubspot-standard` | 110 / 10 s | **200** in 10 s |

MaidCentral is the clearest illustration: the vendor's burst limit is 100 per *minute*, and a
capacity of 100 permits 100 in one *second*. Buckets are now derived by
`rate_limiting.token_bucket_within()` from `DocumentedRateLimit` values, so the invariant
holds by construction, and a parametrised test asserts it per policy per window.

### 2. Seven of sixteen WellSky entities pointed at endpoints that do not exist

Checking every declared path against the operations the Swagger actually publishes:

| Entity | Declared | Reality |
|---|---|---|
| `admin-task` | `POST /v1/adminTasks/_search/` | no `_search` path exists |
| `activity` | `POST /v1/activities/_search/` | no `_search` path exists |
| `document-reference` | `POST /v1/documentReferences/_search/` | no `_search`; `_profile` needs a `reference` |
| `referral-source` | `GET /v1/referralsource/` | **POST-create only** — a GET is 405 |
| `profile-tag` | `GET /v1/profileTags/` | **POST-create only** |
| `location` | `POST /v1/locations/_search/` | published **without** the trailing slash |
| `organization` | `page_number` paging | declares no `_page`/`_count` |

`organization` is the dangerous one: `page_number` stops only on a short page, so an endpoint
that ignores `_page` returns the same full page forever — 10,000 duplicate requests to the
`MAX_PAGES` ceiling. It now uses `single_request`, and `allergyintolerance/all-allergy`, a
bulk endpoint missed entirely, is added.

### 3. WellSky watermarks were claimed on eight entities that cannot filter

Only `patients`, `practitioners` and `relatedperson` document `created`/`updated` as
searchable. The other eight were sending `{"updated": "ge…"}` to a search that does not
define it: the filter is ignored, everything loads, **and the watermark still advances**. A
completeness illusion, not an error. A cross-source test now refuses a watermark on any
source that does not declare the incremental capability.

### 4. One timeout for every source

A single platform-wide 30 s applied to BePro's unpaginated per-frame tracking read and to a
10,000-row GA4 report alike. A timeout shorter than the response makes an entity permanently
unextractable while looking like a transient network fault. `request_timeout_seconds` is now
per source — BePro and GA4 180 s, MaidCentral 120 s, WellSky 90 s, ServiceBridge 60 s — and
bounded at 300 s so one read can never consume enough of the Lambda budget to prevent a
checkpoint.

### 5. Offset paging with no deterministic order

MaidCentral pages by `skipCount` and BePro defaults `sort_direction` to `desc`. Offset paging
over data being written to skips and repeats rows unless the server orders stably. MaidCentral
now sorts by each entity's own key ascending (the guide documents `sorting`); BePro pins
`sort_direction=asc`.

### Also closed

- **Path templates.** Several WellSky and BePro endpoints are `/{patient_id}`-shaped. The
  substrate does no substitution, so such a path would be requested literally. Now rejected at
  spec construction, with a cross-source test.
- **SeniorPlace multi-office.** The spec states `officeId` **must** be supplied "if your
  organization has multiple offices". Nothing supplies it, and the likely behaviour is a
  silent default-office scope — partial data reported as success. Recorded as
  `MULTI_OFFICE_SCOPE_REQUIRED`, and `GET /me` is now extracted as `seniorplace-office`
  because it is the only published way to enumerate offices.

### Re-confirmed, unchanged

- **ServMan Pro** publishes no API surface in 456 pages. Every "REST" hit is a *column
  description* inside the database (a `CCRESTACTIVITY` log table, a REST-provider config
  table). It is a database, as first reported.
- **WellSky Insights** is a warehouse — it even carries a `datawarehouse_availability` status
  table. No API, no rate limits. Separate from Home Connect, as first reported.
- **BePro** rate limits, required parameters and envelope are exactly as first recorded.
- **SeniorPlace** returns bare arrays, documents no paging and no rate limit — as first
  recorded.

---

# First pass — 2026-07-29

The customer supplied vendor API documentation for three sources (`API Docs/`) and doc URLs for six
more. This is what reading all of them against the shipped connector specs found, what changed as a
result, and what is deliberately still open.

**Headline:** two connectors were missing and have been built (ServiceBridge, BePro). Of the seven
existing specs checked against a vendor document, **three were specified against an API that does
not exist** and would have failed on their first HTTP request — while passing every test in the
suite, because the tests only ever exercised each spec against itself. A fourth was throttling
itself to a tenth of its documented allowance.

The structural lesson is the one already recorded in
`feedback: negative tests for every control` and in `ASSESSMENT_CLOSEOUT.md`: a gate that certifies
*shape* rather than *effect* goes green over its own blind spot. `test_rest_api_substrate.py`
asserted that the substrate paginates, authenticates and classifies errors correctly. It could not
assert that any given spec named a real endpoint. `connector_runtime/tests/test_documented_source_fidelity.py`
is the missing control: every assertion in it restates a fact from a vendor document, so the spec
and the document have to change together.

---

## Sources of truth

| Source | Document | Where |
|---|---|---|
| MaidCentral | *MaidCentral Reporting API Guide* (19pp) | `API Docs/maid_central/` |
| ServMan Pro | Data dictionary (456pp) + table relationships (41pp) | `API Docs/servman_pro/` |
| WellSky | Insights Data Dictionary + KPI Library | `API Docs/wellsky/` |
| WellSky | Home Connect API, Swagger 2.0 | `apidocs.clearcareonline.com/swagger.yaml` |
| SeniorPlace | OpenAPI 3.0.3 | `seniorplace-public.s3.us-west-2.amazonaws.com/docs` |
| BePro | OpenAPI 3.1 | `data-api-doc.bepro.ai` |
| ServiceBridge | Help centre 2490148 + the 2.0 upgrade note | `help.servicebridge.com` |
| DialPad | Rate-limits reference | `developers.dialpad.com` |
| HubSpot, Sage Intacct | — | login-gated; **not re-verified**, see *Open* below |

---

## 1. Connectors that were missing

### ServiceBridge — `DL-CONN-18`

`connector_runtime/adapters/servicebridge/servicebridge_connector.py`

Row 10 of the source list reads "HubSpot — Brothers Gutters — *migrating from Service Bridge*". So
this is a **history source**: the value is the pre-migration operational record, swept once and then
kept current on a shrinking tail.

Two documented facts drove the design, and both are unusual:

**The quota is per IP address, not per token** — 50 req/s and 60000 req/hour. Every Lambda in the
VPC egresses through the same NAT address, so every connection, every franchise, every tenant spends
one shared budget. The policy is registered `shared_across_connections=True`. Binding it per
connection — correct for HubSpot's per-token quota — would let N concurrent extractions each believe
they owned 50 rps and collectively issue 50N. This is the first source on the platform to use that
flag for a genuinely per-IP reason rather than a per-app one.

Sizing: 60000/hour is 16.7/s sustained, so the hourly ceiling binds long before the per-second one.
Token bucket, capacity 40 (80% of the per-second burst), refill 13.3/s (80% of the hourly rate).

**The session key is a query-string credential** with a 30-minute *sliding* expiry. Two consequences
are handled rather than tolerated: `AuthKind.SESSION_KEY_QUERY` puts it in `params` and never in a
header, and the HTTP session logs the spec-declared path template only — never a query string — so
the key cannot reach CloudWatch (OWASP A09). There is a negative test asserting exactly that. The
key is re-acquired through the shared token exchange when it lapses.

**Entity catalogue caveat.** The full method reference at `cloud.servicebridge.com/developer/index`
answers 403 to unauthenticated clients, so the ten declared entities come from the resources the
public help-centre pages name directly (`customers`, `locations`, `contacts`, `workOrders`,
`estimates`, `marketingCategories`, and the conventional siblings). Customers, locations and contacts
use the `/api/v2/` shape the 2.0 upgrade note describes; the rest stay on `/api/v1/`. **The
pagination parameter names and the `Results` envelope are inferred, not documented** — see *Open*.

### BePro — `DL-CONN-19`

`connector_runtime/adapters/bepro/bepro_connector.py`

Built directly from the vendor's OpenAPI 3.1 document, recovered from the doc portal's shared-data
endpoint. All 20 endpoints are declared: 7 `meta`, 5 `data`, 1 `video`, 6 `external`, plus schemas.

Three properties drove the design:

**No modification timestamp exists anywhere in the API.** Not one endpoint accepts an
`updated_since`-style filter, and no response schema carries a modified field. The connector
therefore **declines the `incremental` capability** and runs full loads. Claiming it would be worse
than not having it: the watermark repository would advance against data it never actually filtered
on, producing a silent, permanent gap that looks like a healthy incremental pipeline.

**The quota is two-tier** — 1000 req/min sustained, 100 req/s burst, per API token. A fixed window
cannot express that; a token bucket can. Capacity 80, refill 13.3/s, both at 80% of documented.

**Two endpoints are match-scoped.** `data/tracking` returns per-frame positional data for one match
and is not paginated at all; `video/timings` requires a `match_id`. Both are declared with
`required_run_parameters=("match_id",)`, so calling them unscoped fails closed as
`DETERMINISTIC_INVALID_CONFIGURATION` rather than reaching the provider, returning 422, and being
retried as though it were transient. They become schedulable when a match-scoped fan-out exists; the
declaration is what keeps that gap visible in the console rather than invisible.

Envelope is `{count, next, prev, data: [...]}` on every endpoint; `offset`/`limit` with a documented
default of 50 and no stated maximum, so page size is set to 200.

---

## 2. Specs that did not match their vendor documentation

### MaidCentral — rewritten

| | Spec claimed | Guide documents |
|---|---|---|
| Auth | `X-Api-Key` header | OAuth 2.0, `POST /token`, form-encoded, 1-hour token |
| Paths | `/v1/customers`, `/v1/jobs`… | `/api/v1/reporting/{entity}` |
| Envelope | `data` | `Result.Items`, with `Result.TotalCount` |
| Paging | `offset`/`limit` | `skipCount`/`maxResultCount`, default 50, **max 1000** |
| Entities | 7, mostly invented (`appointment`, `service`, `location`) | 13 documented |
| Natural key | `id` on every entity | `ServiceCompanyId`, `CustomerInformationId`, `JobInformationId`… — **not one is called `id`** |
| Watermark | `modifiedDate` | `DateLastModified` |
| Rate limit | `Retry-After` backoff | **1000 req/hour**, burst 100/min |

The rate limit is the tightest on the platform: 0.28 req/s sustained. At the old spec's 200-row pages
a full sweep of thirteen entities would not have fit inside the hourly budget; at the documented
maximum of 1000 it does. The policy is a token bucket with the burst as capacity and the hourly rate
as refill, so a burst drains and then settles rather than exhausting the hour in its first minute and
stalling every remaining entity.

The one-hour token is shorter than a full sweep, which is why the token exchange re-issues mid-run
instead of letting the second half of the extraction 401.

### WellSky — rewritten, and the source list's "~50 tables" is a different product

The previous spec declared fifty entities at `/api/v2/{domain}/{sub}` with keyset paging and a
`data.records` envelope. None of it exists.

The real API is **WellSky Personal Care Home Connect**, a FHIR-flavoured surface at
`connect.clearcareonline.com/v1/`:

- Reads are `POST /{resource}/_search/` with filters in a JSON body. The `GET` collection endpoint is
  the create surface's sibling, not a query.
- Incremental filtering is a comparator-prefixed body field: `{"updated": "ge2026-07-01T00:00:00"}`.
  There is **no upper-bound form**, so the connector binds only the lower bound and leaves the window
  open at the top rather than sending a parameter the API would ignore.
- Pagination is `_page`/`_count`, page-indexed from 0, `_count` capped at 100. Not offsets — reusing
  offset paging here would advance the page index by the row count and skip 99 pages of every 100.
- The response is a FHIR Bundle: `{resourceType: "Bundle", totalRecords: n, entry: [{resource: …}]}`.
  Rows are one level down.
- Every path needs a trailing slash; the vendor's implementation rules state that omitting it errors.
- Auth is OAuth 2.0 client credentials via `POST /oauth/accesstoken`.

**The "~50 tables, API rate limits" note on the source list refers to WellSky *Insights*** — a data
warehouse with `CARE`, `Agencies`, `meta` and default schemas, documented in
`API Docs/wellsky/EXTERNAL--Insights Data Dictionary (Feb 2023).xlsx`. That is a JDBC/warehouse source,
not this API. Conflating the two is what produced the fictional fifty-entity list. Insights remains
unmodelled — see *Open*.

Rate limit: the vendor says it does not explicitly throttle but asks for ≤100 req/s and explicitly
advises against batch use. Both halves are honoured; the bucket sits an order of magnitude below the
stated ceiling, because "we do not throttle" plus "do not use this for batch" is a request for
restraint, not a licence to saturate.

### SeniorPlace — rewritten, and the OData note belongs to a different system

The previous spec modelled OData (`/odata/Clients`, `$filter`, a `value` envelope) on the strength of
the source list's "currently OData to ALL IN". **ALL IN is the downstream system this agency feeds**;
the OData contract is ALL IN's, documented separately in `~/DataLake Docs/OneDrive_1_29-07-2026 (2)/`.
SeniorPlace's own public API is ordinary REST at `app.seniorplace.com/api/v1`:

- `Authorization: ApiKey <key>` — the scheme word is part of the *value*, so it is declared as
  `api_key_value_prefix` and the stored secret holds the key alone, which is what a rotation writes.
- Six collections: `clients`, `client-statuses`, `client/custom-questions`, `users`,
  `referral-contacts`, `referral-organizations`.
- **`updatedAfter` on `/clients` and nowhere else.** The rest are reference lists and declare no
  watermark.
- **No pagination parameters are documented on any endpoint.** Rather than invent `limit`/`offset`
  and hope, every entity uses the `single_request` strategy, which makes the absence a declared fact.
  If the vendor truncates silently, the reconciliation stage's count check surfaces it; a guessed
  parameter would instead look like it worked.
- No rate limit is documented. An undocumented limit is not an absent one, so the conservative fixed
  window stays.

The OData helpers are retained in the module — the ALL IN feed is still live for this agency, and the
timestamp-validation discipline they carry is what keeps that path free of injection.

### DialPad — rate limit corrected

Documented: **20 requests/second per company**. The spec was refilling a token bucket at 2/s — a
tenth of the allowance, with no stated reason, making a full call-log sweep ten times longer than it
needed to be. Now 16/s, keeping 20% headroom for the endpoint-specific per-minute caps that sit under
the global limit.

### ServMan Pro — **not corrected, because the documents are not an API reference**

The two supplied PDFs are a **database schema**: 346 tables with column types, lengths and parent
links, for a Microsoft SQL Server / Codebase installation. `CLIENT`, `CONTACT`, `APPTMENT`, `INVHDR`,
`INVDET`, `JOB`, `EMPLOYEE`, `ORDEMPS`, `SCHEDULE`, `M14_CALLLOG` and so on — plus `APIUSER`,
`APISECURITY` and `CCRESTACTIVITY`, which show the product does have an API layer, but not what it
exposes.

DL-01 already records the ServMan API as "being built out". The REST spec is therefore left exactly
as it is, with its `PENDING_VENDOR_ENTITIES` guard raising `SourceCapabilityUnavailableError` for
endpoints the vendor has not shipped. **Modelling a SQL Server source from a data dictionary is a
different integration** — closer to the existing `mysql_rds` adapter than to anything in
`rest_api/` — and it is a decision for the repo owner, not something to infer. See *Open*.

---

## 3. Substrate changes these documents forced

Every one of these exists because a published API needed it, and each has a negative control in
`connector_runtime/tests/test_rest_substrate_extensions.py`.

| Change | Why | Needed by |
|---|---|---|
| `PaginationParameterNames` on spec and entity | The strategies hardcoded `offset`/`limit`/`after`, so any provider naming them differently got an unpaginated first page and nothing else | MaidCentral, WellSky, ServiceBridge |
| `PageNumberPagination` | A provider that counts pages, not rows | WellSky, ServiceBridge |
| `SingleRequestPagination` | An endpoint with no paging at all — declared rather than left implicit | SeniorPlace, BePro tracking/timings/schemas |
| `record_unwrap_field` | FHIR nests each row under `resource` | WellSky |
| `read_method` + `search_body` + `watermark_body_field` + `watermark_comparator_prefix` | A read that is a POST search with body filters | WellSky |
| `QueryContract.request_body` | A query contract for a POST-search source genuinely has a body; values stay bound, never interpolated | WellSky |
| `RestTokenExchange` | Three sources issue tokens shorter than a full sweep (1 h, 30 min sliding) | MaidCentral, WellSky, ServiceBridge |
| `AuthKind.SESSION_KEY_QUERY` | A credential in the query string, which must never be logged | ServiceBridge |
| `api_key_value_prefix` | `Authorization: ApiKey <key>` — scheme word in the value | SeniorPlace |
| `required_run_parameters` | Fail closed on a provider-required scope a schedule cannot supply, as a *configuration* error rather than a retryable 422 | BePro |
| One-shot 401 re-exchange in `RestHttpSession` | A token that expires mid-entity is not a bad credential; a revoked one still must be | MaidCentral, WellSky, ServiceBridge |

Two defensive details worth recording:

- An **error body is no longer parsed**. Previously a 502 with an HTML error page would have raised
  `RestSourceRequestError` (deterministic, not retried) instead of `RestSourceTransientError`. The
  401-retry restructure surfaced this; it is now explicit and tested.
- The **401 retry is exactly one attempt**. A genuinely revoked credential must still surface as
  deterministic rather than looping.

---

## 4. Rate-limit and threshold summary

Every figure below is the vendor's, and the encoded policy is deliberately under it. Nothing runs
unthrottled: `test_documented_source_fidelity.py` asserts that every spec names a registered policy.

| Source | Documented | Scope | Encoded policy |
|---|---|---|---|
| MaidCentral | 1000/hour, burst 100/min | per API key | token bucket, capacity 100, refill 0.22/s |
| ServiceBridge | 50/s **and** 60000/hour | **per IP** | token bucket, capacity 40, refill 13.3/s, **shared across connections** |
| BePro | 1000/min, burst 100/s | per token | token bucket, capacity 80, refill 13.3/s |
| WellSky | "≤100/s", not enforced, batch discouraged | per client | token bucket, capacity 10, refill 5/s |
| DialPad | 20/s | per company | token bucket, capacity 20, refill 16/s |
| SeniorPlace | undocumented | — | fixed window, 5/s |
| HubSpot | 110/10 s (unchanged) | per token | token bucket, capacity 100, refill 10/s |

Two platform-level protections already sit under all of these and needed no change: sustained 429s
raise `SustainedThrottleError` so the run checkpoints instead of burning the Lambda budget, and any
required backoff above 5 s raises `ResumeAfterBackoffRequired` so the wait is handed to a Step
Functions `Wait` state, which costs nothing, rather than billed as Lambda wall-clock. MaidCentral's
0.22/s refill means a 4.5 s inter-request wait once the burst drains — just inside that threshold, by
arithmetic rather than by luck, and worth knowing before anyone tunes it.

---

## 5. Open — decisions and unverified facts

These are recorded rather than guessed at.

1. **ServMan Pro's real integration surface.** The supplied documents describe a SQL Server database,
   not an API. Options: a SQL Server adapter alongside `mysql_rds`, a replica/CDC feed, or waiting for
   the vendor's REST API. This is the repo owner's call.
2. **WellSky Insights is unmodelled.** The `CARE` / `Agencies` / `meta` warehouse is a separate,
   substantial source — and it is what the "~50 tables" in the SOW actually refers to. It is a
   warehouse integration, not a REST one.
3. **ServiceBridge's endpoint catalogue, pagination parameter names, and response envelope are
   inferred.** `cloud.servicebridge.com/developer/index` answers 403 to unauthenticated clients, so
   the full method reference could not be read. `page`/`pageSize` and a `Results` envelope are the
   conventional shape for this API's generation, but they are **not documented facts**. Verify against
   a live account before the first extraction; the fix is a one-line change to `_PAGINATION` and
   `records_json_path`.
4. **BePro's match-scoped fan-out.** `tracking` and `video-timings` need a parent-scoped driver over
   `bepro-match`. Declared and failing closed; not built.
5. **HubSpot and Sage Intacct were not re-verified.** Both doc sites are login-gated. Their existing
   policies are internally consistent and conservative, so they were left alone rather than changed on
   unverifiable information.
6. **MaidCentral's base URL is unconfirmed.** The guide documents paths but never a host;
   `api.maidcentral.com` is carried over from the previous spec. Confirm at onboarding.
7. **Nothing here has been applied or run against a live vendor account.** Every claim in this
   document is a documentation-to-code claim. The first real extraction per source remains the
   acceptance criterion, exactly as `DL-01` states.
