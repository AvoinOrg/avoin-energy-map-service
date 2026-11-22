# Personal saved buildings, schema version 5

Personal reads default to v5. Explicit older format selectors return 422.
PUT and PATCH require `schema_version: 5`.

This backend accepts the provider access token supplied/refreshed by Avoin Map
core: `Authorization: Bearer <access-token>`. It uses the existing
[Zitadel dependency](authentication.md), never cookies, emails, request owner
fields or owner filters. Every private operation compares the exact trusted
issuer **and** subject. Another owner's resource and an absent owned resource
both return 404. The [field matrix](building-contract.md) remains the input
authority, including units, supported codes, scenario defaults and unsupported
official/derived fields. Plain text must be escaped by consumers.

All paths below start with `/energy-data/user/buildings`. Keys are exact opaque
strings, 1–256 UTF-8 bytes; percent-encode them and configure the direct/proxy path
to preserve encoded slashes. Identity, saved UUID, ownership, creation time and
source/certificate metadata are never editable. PUT/PATCH JSON bodies are bounded
to 32 KiB before parsing, even without an honest Content-Length.

| Method/path | Behavior |
| --- | --- |
| GET collection | Current owner's summaries, default `limit=20`, range 1–100 |
| GET `/{building_key}` | Saved document/current projection and ETag; never creates |
| PUT `/{building_key}` | Create 201 or replace 200, with a required conditional header |
| PATCH `/{building_key}` | Partial set/unset/name changes, 200 |
| DELETE `/{building_key}` | Delete personal state only, 204 |

The [sharing extension](building-shares.md) adds revocable live links and atomic
cascading deletion. `share_enabled` is a bool reflecting actual active state.

## Conditional writes and sparse inputs

The exact strong saved-state validator is `"ub1:<saved-uuid>:<positive-revision>"`.
It includes the incarnation, so deletion/recreation invalidates an old tag.
It describes persisted personal state, not current source freshness. Responses
are always no-store and never 304; a source refresh can alter unoverridden facts
without changing the personal revision. `resolved_at`, `captured_at` and
`source_observed_at` have different meanings; null source observation is unknown.

Create explicitly (an empty document is a deliberate saved record):

```http
PUT /energy-data/user/buildings/example-key
Authorization: Bearer <access-token>
Content-Type: application/json
If-None-Match: *

{"schema_version":5,"display_name":"Home","overrides":{"floor_area":0,"total_area":null},"scenario_inputs":{"resident_count":2}}
```

GET the same path to retrieve its current projection and ETag. Full replacement:

```http
PUT /energy-data/user/buildings/example-key
Authorization: Bearer <access-token>
Content-Type: application/json
If-Match: "ub1:00000000-0000-0000-0000-000000000001:1"

{"schema_version":5,"overrides":{"construction_year":1998}}
```

An omitted PUT name clears it; omitted override/scenario fields reset to source
fallback/frontend default. PATCH omission preserves the current stored value;
supplied name string sets it and null clears it. A supported null marks a field
unknown; `unset` restores fallback. A field cannot be set and unset together.

```http
PATCH /energy-data/user/buildings/example-key
Authorization: Bearer <access-token>
Content-Type: application/json
If-Match: "ub1:00000000-0000-0000-0000-000000000001:2"

{"schema_version":5,"display_name":null,"set":{"overrides":{"heated_net_area":null},"scenario_inputs":{"resident_count":3}},"unset":["overrides.construction_year"]}
```

Reset all overrides with PUT `{"schema_version":5}` plus the current If-Match. The intentionally
saved record remains listed. DELETE uses the same current If-Match, has no body
and returns 204. Every accepted PUT/PATCH advances revision once, including an
unchanged save; created time stays fixed and updated time advances monotonically.
All comparisons, PATCH merges, revision increments and commits are protected by
an owner-scoped row lock. Concurrent creation is constrained by PostgreSQL:
exactly one creator wins 201, the duplicate returns 412. No helpers commit;
`locked_owned`, `bump_revision` and caller-owned transactions are reusable by
sharing. Representation construction occurs before commit, with no post-commit
source reload that could make an accepted save appear to fail.

| Condition | Status |
| --- | --- |
| Missing/invalid/expired/revoked credentials | 401 with Bearer challenge |
| Configured permission missing | 403 |
| Missing owned GET/PATCH/DELETE; missing source on first save | 404 |
| Ambiguous source on first save | 409 |
| Required creation or existing-write precondition absent | 428 |
| Existing create, stale/different-incarnation tag, If-Match with no record | 412 |
| Contradictory/duplicate/malformed headers, weak/wildcard If-Match | 400 |
| Raw UTF-8 body over 32 KiB | 413 |
| Unknown fields/query filters, invalid code/type/bounds/cursor | 422 |
| Disabled/invalid/unavailable state, required catalog/source or identity outage | 503 |

Only one exact quoted strong If-Match is supported, not lists or wildcards.
If-None-Match accepts only `*`; never send both. On 412 reload, show the current
values and let the user reconcile before retrying. Resource ownership is checked
before existing-resource preconditions disclose anything. Errors contain only
`{"detail":{"code":"precondition_failed","message":"Saved building changed; reload before saving"}}`
or sanitized field-level 422 `detail` entries (`loc`, `type`, `msg`, no input).
Unexpected failures use a sanitized 500. All private success/errors, including
204/401/422/503, use `Cache-Control: no-store`.

## Detail, source loss and front-page listing

Detail DTOs extend `PersonalBuildingProjectionV5` with `saved_id`, `display_name`,
`revision`, UTC `created_at`/`updated_at`, and `share_enabled`. `base`, sparse
`overrides`, `scenario_inputs`, `effective` and field provenance remain separate.
User values never become official certificate/model/source facts.

Only a successful explicit save captures/refreshes the small
`last_known_summary` (address, location, nullable source timestamp, capture time).
GET/list/readiness never refresh it or save state. Existing GET/list still return
200 during missing/ambiguous/unavailable source: `base_status: unavailable`,
`base_unavailable_reason`, null current base, retained overrides and labelled
historical summary. Historical facts never silently become the current base.
Existing PUT/PATCH/name edits/reset/delete remain available; new saves require
an available unambiguous canonical building. A source snapshot is coherent but
not a distributed transaction with the separate state commit/ETL rebuild.

Non-code edits, null/unset and deletion need no catalog. Every newly supplied
non-null registry code needs current catalog membership (full PUT revalidates
all supplied codes). A deprecated stored code stays readable and survives an
unrelated PATCH. Required catalog outage fails 503 with rollback.

Collection accepts only one `limit` and one `cursor`, returning:

```json
{"schema_version":5,"items":[{"schema_version":5,"document_schema_version":5,"saved_id":"00000000-0000-0000-0000-000000000001","building_key":"example-key","display_name":"Home","revision":3,"created_at":"2026-10-23T12:00:00Z","updated_at":"2026-10-23T12:01:00Z","share_enabled":false,"base_status":"unavailable","base_unavailable_reason":"missing","current_summary":null,"last_known_summary":{"schema_version":1,"building_key":"example-key","name":null,"address_fin":"Example street 1","postal_code":"00100","postal_office_fin":"City","location":null,"source_observed_at":null,"source_availability":{},"captured_at":"2026-10-23T12:00:00Z"}}],"next_cursor":null}
```

Available `current_summary` is the canonical `BuildingSummaryV1`, with
address/postal/location/source availability. Personal display name stays separate
from canonical `name: null`. Items have no complete payload, owner IDs or tokens.
Ordering is `updated_at DESC, saved_id DESC`. A closed versioned URL-safe cursor
preserves UTC microseconds and UUID; it contains no owner and grants no access.
Reuse `next_cursor` unchanged. This is keyset pagination, not a frozen snapshot:
mutations can move rows ahead of a boundary; deletion can shorten a page.
Deduplicate by saved UUID and refresh the first page after saves/deletes.
The page queries summary columns only, fetches limit+1 then trims, and resolves
at most 100 returned keys in one canonical batch; empty pages do no source work.
There is no count/offset/full-geometry-per-item query. Main lacks a key index;
production-scale latency remains unverified; this feature adds no source index.

The front page should load this collection after core sign-in, partition its
cache by the actual session principal, clear on sign-out, load details before
editing, retain their ETag and refresh listing/detail after mutation. Show
historical address and unavailable-source state explicitly. Core refreshes and
forwards provider access tokens; frontend cookies do not authenticate this API.

Configured `ENERGY_MAP_API_ORIGINS` remains authoritative. Direct browsers send
Authorization, Content-Type, If-Match/If-None-Match; preflight supports these and
actual responses expose ETag. A same-origin core proxy must forward those
headers and preserve statuses, ETag and no-store. CORS does not authorize access.
Do not put tokens in URLs/logs, log bodies, enable provider debug tracing, or
include private responses in shared caches. This feature does not add a UI.

## State configuration and least privilege

Default `ENERGY_MAP_STATE_ENABLED=false`. Enabling requires an explicit
`ENERGY_MAP_STATE_DATABASE_URL=postgresql+psycopg2://<writer>:<encoded-password>@<host>:5432/<database>`.
Use a URL-encoded password and private secret injection, not command history.
No DSN query options are accepted. Never fall back to local/sandbox analytical
credentials. The engine is lazy; import/OpenAPI/startup do not connect/create/
migrate state. Missing configuration/schema/grants and connection/lock failures
are sanitized. Waits are bounded: connect/pool 3s, lock 2s, statement 10s.

Keep global `PGOPTIONS=-c default_transaction_read_only=on` and
`MIGRATE_ON_START=false`. Only the dedicated writer supplies libpq connection
options overriding inherited read-only to off; it also restricts search_path.
Real grants enforce isolation. Source resolution always uses a separate existing
read-only analytical connection, even when state is hosted in the same database.
For deployment, follow the complete
[personal-state configuration and verified-TLS contract](../../docs/operations/personal-state-storage.md#migration-and-runtime-transport),
including the required CA settings and read-only trust-material mount. Compose's
global public-read safeguard is unchanged.

`/healthz` remains liveness; `/readyz` remains area-data readiness.
`GET /readyz/state` independently probes expected column types, valid required
indexes and required grants
with read-only queries, returning 200 `{"status":"ready"}` or sanitized 503
`{"status":"personal_state_unavailable"}`. It performs no DML/DDL/introspection.
It is operational readiness, not provider/area/backup verification. A state
outage must not remove public browsing from service.

Private requests also run two fixed catalog/grant probes before their state
query. Unsafe superuser/owner/membership/CREATE or public-source write privileges
fail closed with 503, as do absent required indexes/types/grants. Pointing the
writer configuration at migration-owner credentials is rejected.

Use separate migration-owner, runtime-writer and analytical-reader roles. For a
new dedicated database, the canonical
[resource and privilege contract](../../docs/operations/personal-state-storage.md#resource-and-connection-contract)
defines the exact grants and guarded provisioning command. It requires proven
writer entry denial on the analytical source database: effective CONNECT denial,
or targeted new-account HBA confinement when PUBLIC CONNECT is present. Fresh
state success and explicit source refusal are required; source ACLs stay unchanged.
Analytical readers retain their separate source connection and receive no
personal-state access.

Durable rows live only in `energymap_state.energymap_user_buildings`, without any
FK/dependency on main/buildings/addresses/certificates. The migration adds UUID
PK, positive revision/schema/object/name/key constraints and owner-list indexes.
Provider subjects can exceed the B-tree tuple limit, so indexes use bounded
`md5(owner_issuer), md5(owner_subject)` plus exact building key/order. **Every**
lookup also binds/compares both original strings. The unique-conflict handler
checks exact ownership before returning 412; a hash collision fails closed 503
and cannot authorize access. Exact identities are never truncated/normalized.
This intentionally rejects a theoretical colliding principal/key pair rather
than aliasing owners. One exact owner/key tuple cannot create duplicate rows.

## Migration, backup, restore and downgrade

For a **new dedicated state database**, use the guarded isolated
[personal-state operations contract](../../docs/operations/personal-state-storage.md).
It provisions the exact narrow grants, runs the independent lineage through
`es0005_utilities_v5` (historical bootstrap `es0001_saved_share`), and backs up/restores the **entire dedicated database**,
including independent version metadata and private saved/share rows. Never run
the source Alembic chain in a new state-only database or stamp its source head.

The original combined-database lineage remains `c7d3e921ab40` (saved) followed by
`d4e8f021bc63` (shares), then the state-only `e5f102acd890` personal-v2
successor. Existing combined installations alone use the global
Alembic entry point, with a separately reviewed migration connection and source
prerequisites. A schema-scoped `pg_dump --schema=energymap_state` from such an
installation omits public migration metadata and source prerequisites; it is
not a complete dedicated-state recovery archive. Preserve matching migration
metadata/configuration separately and restore only into a new reviewed target.
All archives contain private identities, text and share digests; protect and
encrypt them and verify actual complete row recovery and reader denial.

Isolated tooling refuses destructive downgrade/adoption/reset. Historical global
downgrades delete personal records/links; do not use them as repair or downgrade
older source/area revisions. Use a verified complete backup and fresh target.

Deployed state DB/roles/grants/migration/backups, Energy API audience/Basic-client
provisioning, a real-token Zitadel smoke test, direct/proxy header forwarding,
frontend/browser acceptance and deployment remain external unverified work.
Fixture auth establishes no real provider integration; tmpfs fixture persistence
is process/ETL survival evidence, not host-reboot/production durability.


## Sharing integration v1

[Building sharing](building-shares.md) now supplies actual bool share_enabled in
owner detail/list (no token/digest). Saves/reset preserve active links. The share
migration must be present for state readiness. DELETE cascades shares atomically
and returns a terminal ETag; DELETE share returns the next usable owner ETag even
on already-disabled state. Encode entire keys, especially a terminal /share.
The v1 share prerequisite is d4e8f021bc63; the current combined head is
e5f102acd890; the owner-only migration/downgrade
examples above describe its prerequisite, not the share release procedure.
