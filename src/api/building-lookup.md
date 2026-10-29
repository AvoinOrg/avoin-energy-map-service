# Building lookup v1

`POST /energy-data/building-lookup` is anonymous, read-only JSON discovery.
This implementation supports **partial Main-scope permanent-building-ID lookup**.
Address and property inputs have strict validation and explicit capability errors;
they do not yet have implemented discovery. This delivery does not clear the
complete F107.4.2 lookup gate or establish a deployed API origin.

The independent confirmation step remains
`GET /energy-data/buildings/{whole encoded building_key}`. Always present an
explicit candidate choice, including a single result, then confirm its exact key.
Handle canonical 404/409/503 independently. Lookup never saves personal state,
requires login, or substitutes an identifier/address/tile ID for a canonical key.
The canonical v1 contract and its cache policy remain unchanged.

## Requests and implemented capabilities

Use `Content-Type: application/json`, without URL query parameters. Bodies are
bounded to 16 KiB before JSON parsing, including chunked transfers. Invalid
UTF-8/JSON, nonfinite JSON constants, unknown fields and incorrect types return
sanitized 422. Non-JSON/encoded bodies return 415; oversized bodies return 413.
All nested DTOs are closed. `schema_version` is required integer 1, `limit` is a
strict integer 1–50 (default 20), and `cursor` is omitted/null or an opaque
base64url string up to 2 KiB. Booleans and numeric strings are not integers.

These requests are **synthetic examples**, not known successful source queries:

```json
{"schema_version":1,"query":{"mode":"address","address_fin":"Mäkelänkatu 1","postal_code":"00550","municipality_number":"091"},"limit":20}
{"schema_version":1,"query":{"mode":"property_identifier","property_identifier":"00012345678901"},"limit":20}
{"schema_version":1,"query":{"mode":"permanent_building_identifier","permanent_building_identifier":"0000000001"},"limit":20}
```

| Mode | Exact validation and semantics | Current discovery |
| --- | --- | --- |
| `address` | Nonblank `address_fin`, ≤512 UTF-8 bytes, no control/surrogate code points. Optional postal code exactly 5 ASCII digits and municipality number exactly 3; omit unused filters, explicit null/empty is invalid. | 503 `lookup_capability_unavailable`, before source access. |
| `property_identifier` | Exactly 14 ASCII digits, not all zeroes, as JSON string. No hyphenated human form, whitespace, short components, Unicode digits, numeric input or inferred padding. | 503 `lookup_capability_unavailable`, before source access. |
| `permanent_building_identifier` | Nonblank opaque JSON string, 1–256 UTF-8 bytes, no controls/surrogates. No trimming, numeric coercion, assumed length, checksum, case change, padding or uniqueness assumption. | Indexed equality on `public.main.permanent_building_identifier` only, without a geometry predicate. |

Address-v1 defines exact equality on the combined Finnish-named street-and-number
field. Preserve case, diacritics, spacing and Unicode sequences (including
decomposed accents). No normalization, fuzzy/prefix/wildcard match, transliteration,
apartment parsing, translation or nearest match is supported. The definition is
not an implemented address source path. Future discovery must honor every supplied
filter and all matching secondary address relationships. The observed upstream
Swedish fields do not establish supported Swedish/English search aliases or
language completeness. No address/property mode automatically becomes enabled
when someone adds an index; new source evidence and a reviewed source plan are
required.

## Candidates, ambiguity and honest geometry

See the [closed JSON Schema](building-lookup-v1.schema.json) and
[complete typed examples](building-lookup-v1.examples.json) for every field,
request, ambiguous/single/empty response, first/last page and error body.
Regenerate with `python -m src.api.export_building_lookup_contract`.

Each candidate has an exact opaque string `building_key`,
`requires_canonical_confirmation: true`, nullable permanent/property identities,
separate identity availability states, `identity_conflict: false`, matched
mode/source, one representative address, address variant count, source evidence,
geometry availability and nullable location. Malformed/numeric source keys are
503 source-contract failures, never converted or silently dropped. Valid leading
zeroes are retained. Invalid product-format source property IDs become null with
`property_identity_status: invalid`. Distinct nonblank identity values for one
key **within the enumerated match set** cause 409 `lookup_identity_conflict`.
No arbitrary identity winner is selected. Different keys sharing a permanent ID
remain separate candidates and set `page.permanent_id_multiplicity: true`.

Duplicate rows and geometry fragments collapse by exact key. Addresses are
deduplicated as whole `(address_fin, postal_code, postal_office_fin)` tuples,
with blank source fields represented as missing. Choose the smallest tuple by
UTF-8 bytes, nulls last; fields never come from different rows. Variant counts
cover Main display-address tuples in this match set, not all registry secondary
addresses. Source strings otherwise preserve their bytes. Municipality is null
because it is not part of this reviewed Main projection. `source_language: fi`
identifies the Finnish-named fields, not a language detector. `matching_address`
is null in the implemented permanent-ID path. A future actual address path must
keep matching evidence distinct from its display representative.

No geometry blobs or per-candidate canonical queries are fetched. A nonnull
Main point/polygon yields `present_unvalidated`; all null geometry columns yield
`missing_in_scope`; no projected geometry columns yields `unknown`. All keep
the candidate. `location` is always null: canonical GET supplies validated
geometry/location and its SRID/WGS84 contract. Geometry presence does not prove
a usable polygon or map highlight. Building-only records are excluded from this
partial path, rather than silently advertised as covered.

Main has no reviewed row-modification field: evidence explicitly reports
`timestamp_status: unknown`, null time, `reason: not_projected`. Distinct geometry
fragments/identical duplicates do not create an authoritative version or a
conflict; changes that leave the semantic candidate output unchanged do not
invalidate a page. Contradictory facts outside the equality range are not
searched by key (no supported Main-key reverse index); canonical confirmation
retains independent identity checks and Main/Building precedence.

## Coverage and source version

Successful responses say `scope: main`, `enumeration_complete: true`,
`scope_complete: false`, `national_completeness: unknown`. Main is included via
indexed permanent equality; Building is unsupported for bounded reverse access;
Address and Polygon are outside the Main scope. Excluded components are not
queried for candidates, even when their relation is present. Catalog metadata
records their shape/readability for page invalidation; exclusion does not assert
they are empty, missing or unreadable.

`outcome: candidates` means candidates exist. A successful empty equality range
returns 200 `no_match_in_scope`, not a national no-match. `no_match` is reserved
in the schema for a future complete scope; this adapter never emits it. An unsafe
or unavailable path is 503, not a successful empty result. No success result in
this delivery advertises complete scope or national coverage.

`observed_at` is acquisition/request time. Dataset version, checksum and refresh
time remain null with `no_authoritative_generation_lineage`. Table OIDs, relation
shape, import timestamps, request clocks, PostgreSQL snapshots and HTTP conformance
validators are not dataset generations. The internal scope fingerprint uses
relation/index identity and shape solely to detect relevant pagination changes.

## Bounded source access and pages

The lookup-owned connection pool uses the existing analytical source configuration,
not private state. It has 1s pool checkout and 2s libpq connection timeout;
queries use a fresh clean `REPEATABLE READ, READ ONLY` snapshot, rollback-only,
≤3s statement timeout and ≤1s lock timeout. A monotonic 8s source-work budget
is checked before/after each source statement and before success, reducing each
subsequent statement timeout to the remaining budget. There are no speculative
retries. Connection/DNS/OS scheduling and rollback cleanup can exceed the budget;
it is not a hard HTTP wall-clock cancellation SLA.

Before discovery, catalog metadata verifies the ordinary `public.main` table,
SELECT access, string-typed fields, deterministic identifier collation and a
valid/ready, nonpartial, nonexpression btree with a leading permanent-ID text
operator class and matching collation. An actual parameterized EXPLAIN (never
ANALYZE) must show a direct Index Scan/Index Only Scan below LIMIT. Sequential,
bitmap, join, sort and aggregate paths are rejected before data execution.
Planner switches are never forced. This may conservatively reject a common
identifier whose current plan is unsafe; a timeout does not license a scan.

The raw equality query takes at most 2,049 rows, before joins/deduplication/sorts
and before any geometry payload. More than 2,048 raw matches or 512 distinct
keys returns 422 `lookup_query_too_broad` without candidates. Source text is
length-guarded in SQL, so oversized fields fail the source contract without
fetching unbounded values. Serialized responses are capped at 512 KiB. These
are service limits, not claims that every national query fits. No partial
overflow result is returned. Response-size enforcement is exercised with a
reduced test ceiling; the allowlisted v1 fields/page limit also constrain size.

Within a completely enumerated bounded Main match set, candidates are sorted by
exact UTF-8 key bytes ascending (PostgreSQL `COLLATE "C"` ordering), then sliced
at `limit`. `page` supplies exact total, `has_more`, nullable `next_cursor`,
order-policy version and permanent-ID multiplicity. Every page re-enumerates
the complete same bounded scope in a new snapshot; no transaction is held
between requests, persistent cache/table or cursor secret is added.

Cursors contain strict canonical base64url JSON: version/order policy, exact
query fingerprint, source shape/scope fingerprint, semantic candidate digest,
last exact key, limit, original Unix issue/expiry time. Lifetime is five minutes;
normal paging carries the original times forward. Cursors are untrusted public
navigation data, not signed authorization. They cannot bypass validation,
source work or limits; no authenticity or tamper-proof original issue time is
claimed. Malformed/noncanonical/oversized cursors are 422. Expiry, changed
query/limit/policy, changed candidate identities/addresses/availability, changed
relation/index shape or missing last key is 409 `lookup_page_invalidated`.
Restart at page one. A current source failure stays 503; current identity
conflict stays 409 identity conflict. Identical duplicates and unchanged semantic
output stay stable. The digest cannot detect transient changes that reverted
between page requests or establish national immutability. Canonical confirmation
must still run after selection.

## Errors, privacy and client/release policy

All error bodies are `{ "detail": { "code", "message", "mode", "component" } }`.
Only allowlisted mode/component values are returned, otherwise null. No validation
input/location/context, SQL, DSN, provider bodies, upstream endpoint, source row,
auth token, owner/share fields or private-state facts are exposed.

| HTTP | Codes |
| --- | --- |
| 409 | `lookup_identity_conflict`, `lookup_page_invalidated` |
| 413 | `lookup_payload_too_large` |
| 415 | `lookup_unsupported_media_type` |
| 422 | `lookup_invalid_request`, `lookup_query_too_broad` |
| 503 | `lookup_capability_unavailable` (unsupported/unsafe discovery), `lookup_source_unavailable` (outage/timeout/unreadable required source), `lookup_source_contract_failure` (bad source shape/values) |
| 500 | `lookup_internal_error` (sanitized unexpected application failure) |

Every response under the lookup prefix, including CORS preflights, method errors
and framework failures, has `Cache-Control: no-store`. Framework 404/405 and CORS
denials keep their ordinary framework bodies. Existing configured CORS is reused;
origins/credentials are not widened. Canonical GET keeps `no-cache`; saved/share
auth, ETags, no-store and redaction remain unchanged. Request bodies are never
logged by lookup, SQL parameter/result logging is disabled on its dedicated
engine, and final URI redaction covers lookup URLs (including malformed query
parameters) and bearer values. Deployment access/proxy/APM body logging must
remain disabled; release verification is outstanding.

Clients submit explicitly once, cancel superseded requests and fence stale
results/selections. Do not query on every keystroke or prefetch pages. No
distributed rate limiter is added here; ingress rate policy remains a release
configuration item. No public application origin, deployment, frontend UI or
ordinary-user acceptance is established by local tests.

## Real-source evidence and remaining capability resolution

The October 29 bounded diagnostic and independent rerun are retained under
`.codex-orch/features/F004.1-building-lookup/source-observation.json`,
`source-diagnostic-runtime-doctor-report.md` and the no-argument guarded
`collect-source-observation.sh`. The helper uses protected reader configuration
in memory and records no source addresses, keys, IDs, DSNs or credentials.
It proves one Main permanent-ID equality range used a ready nonunique btree
and returned one distinct candidate with geometry. Its bounded Building-key
check found no row; canonical GET was not run and Main-only canonicals are valid.
No real successful no-geometry reverse sample, uniqueness, national coverage,
latency guarantee or source freshness was demonstrated.

Address/property SQL reverse indexes were absent; unindexed queries were
EXPLAIN-only. Building-only permanent discovery has no proven bounded path.
Ryhti CQL2 behavioral address/property/permanent probes returned 3/4/1 features,
with impossible controls returning zero. Simple field parameters failed negative
controls and must not be used. Both advertised queryables returned HTTP500;
no supported per-field semantics, complete canonical union, stable pages or
dataset version were established. Samples/metadata estimates do not prove
national emptiness, completeness, uniqueness or usable latency. This is source
evidence, separate from disposable adapter tests and deployed acceptance.

The one proposed resolution is an **ETL-owned immutable indexed lookup generation**
covering exact keys from both Main and Building, every available secondary
address relationship, no geometry predicate, explicit conflicts/coverage and
authoritative generation/checksum/refresh lineage. Provide indexed exact address
plus optional postal/municipality filters, property ID and permanent ID, each
with exact canonical-key ordering. Atomically publish generations, retain them
under a defined budget and invalidate retired-generation cursors. Ryhti CQL2 is
a concrete input to investigate with supported filter/ingestion lineage, not
an already approved complete resolver. Manager/source-owner decisions still
needed: projection owner/database/location, refresh/retention budget, complete
address input and Finnish exact matching versus additional language/normalization
requirements. No live ETL/index/schema/provisioning action is authorized here.
