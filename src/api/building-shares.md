# Building sharing v1

An owner explicitly creates one live read-only link for a saved building. POST
always rotates the capability and advances the owner's revision once. DELETE
share, including an already-disabled share, advances once with current If-Match.
Both use the saved-row lock and the exact trusted issuer/subject. Revocation,
rotation and owner deletion invalidate the old link at commit. Accepted mutations
return the resulting strong owner ETag, including bodyless 204 responses.
Owner deletion advances once and returns a terminal deletion receipt; recreation
requires If-None-Match: * and a new saved UUID. The receipt cannot authorize it.

Tokens contain 256 random bits; only SHA-256 digests are stored. POST returns
schema_version, share_token, resolver_path, revision, updated_at, with 201 and
ETag. The relative path begins /energy-data/shared-buildings/. GET/list cannot
recover a raw token. Losing a POST response requires reload and deliberate
rotation. Host and Forwarded never determine URLs.

The public DTO is closed and independent of owner DTOs. Its exact top-level
allowlist is schema_version, building_key, display_name, revision, created_at,
updated_at, base_status, base_unavailable_reason, base, overrides,
scenario_inputs, effective, field_provenance, source_availability,
source_observed_at and last_known_summary. Base allows the canonical v1 fields,
with identity limited to permanent_building_identifier and property_identifier.
All typed detail, certificate, published-certificate, model, geometry, location,
code-label and source components retain their v1 field allowlists. Provenance
and availability maps are filtered to documented public field paths; internal
address keys, saved UUIDs, owner identity, share metadata/digests and arbitrary
metadata are excluded. No owner ETag is sent publicly. Saved personal name and
address text and all supported overrides/scenario inputs are intentionally visible
to anyone possessing the link; the UI must explain this before enablement.
Consumers must escape all plain text. User inputs remain assumptions; published
models and certificates are source facts, never recalculated by sharing.

GET performs a fresh indexed digest lookup, observes the current canonical source,
then re-reads the active capability and latest saved document in a new read-only
state transaction. That final SELECT is the read's linearization point: requests
starting after a completed revoke/rotate/delete cannot succeed. Overlapping
reads may finish after a mutation; previously delivered content cannot be
retracted. No token/projection cache, authentication dependency, access timestamp
write or implicit save exists. Source observation is separate from state; missing,
ambiguous or unavailable source retains overrides and labelled historical summary,
with null current base. Sharing/revoking existing records needs no source access.

Unknown, malformed, rotated, revoked and deleted tokens return the same JSON 404.
Every private/share response, including framework/CORS/error responses, is
Cache-Control: no-store. Public analytical endpoints retain their cache policy.

Encode the entire opaque building key with encodeURIComponent. Literal terminal
/share denotes the operation: /a/share manages sharing for key a;
/a%2Fshare deletes/reads saved key a/share, and /a%2Fshare/share manages that key's
share. Proxies must preserve encoded slashes. Never guess by record existence.

## HTTP contract and examples

[Complete schema-valid synthetic examples](building-api-v1.examples.json) and
[closed schema bundle](building-contract-v1.schema.json) accompany OpenAPI. Public
canonical details keep their existing GET and no-cache policy. All saved/share
DTOs are version 1. Request excerpts below omit transport boilerplate only.

| Action | Request | Response and next validator |
| --- | --- | --- |
| Initial save | PUT /energy-data/user/buildings/{encoded_key}, If-None-Match: *, JSON `{"schema_version":1,"display_name":"Home","overrides":{"floor_area":0,"total_area":null},"scenario_inputs":{"resident_count":2}}` | 201 SavedBuildingV1, ETag |
| Owner read | GET same path, Bearer | 200 SavedBuildingV1, ETag |
| List | GET /energy-data/user/buildings?limit=20&cursor={opaque_cursor}, Bearer | 200 SavedBuildingPageV1, next_cursor nullable |
| Full replace | PUT saved path, If-Match from read/mutation, JSON `{"overrides":{"floor_area":120}}` | 200, name/omitted leaves cleared, new ETag |
| Partial edit | PATCH saved path, If-Match, JSON `{"set":{"overrides":{"floor_area":150,"total_area":null},"scenario_inputs":{"resident_count":4}},"unset":["overrides.heated_net_area"]}` | 200, omitted leaves/name retained, new ETag |
| Reset leaf/name | PATCH saved path, If-Match, JSON `{"display_name":null,"unset":["overrides.floor_area","scenario_inputs.resident_count"]}` | 200, current source/default fallback, new ETag |
| Reset all | PUT saved path, If-Match, JSON `{}` | 200, saved record and active link retained, new ETag |
| Create/rotate link | POST saved path + /share, If-Match, no body or `{}` | 201 ShareCreatedV1, new ETag; former token immediately invalid |
| Public read | GET resolver_path, no Bearer needed | 200 SharedBuildingV1, no ETag; latest saved document/current source |
| Revoke | DELETE saved path + /share, Bearer, If-Match | 204 empty body, new ETag, including already-disabled state |
| Discard saved record | DELETE saved path, Bearer, If-Match | 204 empty body, terminal ETag; dependent link removed atomically |

Every owner request uses `Authorization: Bearer <provider-access-token>`.
For JSON send Content-Type: application/json. Preserve the exact quoted ETag
(e.g. `"ub1:00000000-0000-0000-0000-000000000001:2"`) in If-Match. A 204 has no
JSON to parse. It still exposes ETag and no-store. A public link can only GET;
it cannot revoke or edit. Unsupported public mutation methods return 405 and
perform no DML. No owner filter/header/body can substitute for authentication.

| Status | Client handling |
| --- | --- |
| 400 | Malformed, duplicate or contradictory conditional headers; fix request |
| 401 | WWW-Authenticate: Bearer; use core refresh/relogin; do not retry a write with the capability as bearer |
| 403 | Configured permission missing; explain permission requirement, ordinary users need no invented admin role |
| 404 | Owned record/new source missing or another owner; public invalid/unknown/revoked/deleted link has identical `{"detail":{"code":"shared_building_not_found","message":"Shared building not found"}}` |
| 409 | New canonical identity ambiguous; do not create/reattach by address |
| 412 | Current saved incarnation/revision changed, including duplicate create; reload/reconcile before deliberate retry |
| 413 | Personal JSON exceeds 32 KiB before parsing |
| 422 | Sanitized field errors (`loc`, `type`, `msg`); no echoed values; fix input |
| 428 | Supply initial If-None-Match: * or current If-Match as applicable |
| 503 | Sanitized provider/state/required catalog failure; no write; keep source browsing usable |
| 500 | Sanitized unexpected failure, transaction rolls back; reload current state before retry |

Public share GET never introspects even when an invalid Authorization header is
supplied. Disabled/old/unavailable state fails personal/share APIs with 503;
health and analytical building/area/tile APIs remain independent. Malformed
capabilities are rejected as 404 before database access. Successful reads never
insert records, refresh historical summaries or mutate source/state.

## Frontend integration handoff

Use Avoin Map core `useAuthSession`/`getAuthAccessToken()` for browser provider
access tokens, or `getStartAccessTokenFromAuth` server-side. Keep refresh tokens,
cookies, callbacks and login in core. Forward its provider access token as Bearer;
an ID token or a frontend user ID is insufficient. Core's configured Zitadel
project audience scope must include the dedicated Energy API accepted audience.
The backend uses its own Basic-authenticated introspection API application, with
bounded timeouts and no successful-token cache; do not reuse Hiilikartta secrets.
See [authentication.md](authentication.md) for placeholder-only server variables
and the later real-token smoke-test boundary. Carbon queries are conceptual
list/external/save references; this API takes JSON, not their multipart payloads.

On selected-building change, encode the entire canonical building_key, load
public GET, and separately load owner GET if signed in. Viewing/sign-in never
saves. Present base, effective, overrides, per-field provenance and nullable source
time separately. Personal values/unknowns remain assumptions, and published
certificate/model context stays source-based even when model_input_divergence
is populated. Explicit save creates with If-None-Match: *. Owner read/mutation
returns the ETag for edit/reset/delete/share. On 412 reload both detail and tag,
show changed state and let the user reconcile. A source disappearance displays
unavailable current source plus labelled last-known identity, never stale facts
presented as current. Revoke/delete still work without source availability.

The bottom front-page section uses only the current owner's bounded collection.
Deduplicate by saved_id, follow next_cursor unchanged, refresh page one after
save/share/revoke/delete, and expect rows to move between pages as updated_at
changes. Scope query caches by actual session identity and clear on sign-out.
No complete building payload is needed per list item. Main's production-scale
lookup latency is unverified; the page uses indexed share EXISTS and one bounded
canonical summary batch, with two fixed state catalog/grant probes.

Recipients use a separate read-only frontend route/query; the capability does
not grant owner UI actions. The backend supplies a relative resolver path only.
Construct locale/domain-aware frontend page URLs with trusted application routing
and configured app origin; no backend Host/Forwarded-derived absolute URL exists.
Before enablement, explain that saved personal name/address and supported inputs
become visible, subsequent saves update the same link, and revocation cannot
retract previously copied content. Lost create/rotate responses cannot be
recovered from GET/list: reload the current revision and deliberately rotate.

Set `Referrer-Policy: no-referrer` on the share page and `referrerPolicy:
'no-referrer'` on fetches. Exclude share URLs/tokens from analytics, error monitoring,
APM, session replay, browser persistence and logs; avoid placing them in unrelated
links. Escape plain text. Keep responses out of browser/shared query persistence,
CDN and proxy caches; ordinary per-render memory is sufficient. The UI/privacy
controls and browser acceptance require later frontend work.

Direct clients must send Authorization, Content-Type, If-Match/If-None-Match;
configured origins support these and expose ETag. A same-origin proxy must preserve
encoded keys, these headers, quoted ETags even on 204, statuses and Cache-Control.
It must not redirect resolver URLs, log their request/upstream URI, derive page
origins from untrusted Host, or cache authenticated responses. CORS grants no
ownership; adding anonymous sharing does not broaden configured origins.

## Additive migration, narrow permissions and recovery

Share migration d4e8f021bc63 follows the re-observed c7d3e921ab40. It adds only
energymap_state.energymap_user_building_shares: saved_id UUID PK/FK ON DELETE
CASCADE to the durable saved row, unique non-null bytea token_digest constrained
to 32 bytes. Row existence is the only active flag; no raw URL, history or source
FK exists. Runtime readiness checks types/nullability, valid unique indexes,
PK/unique/length/FK target/action constraints and effective minimum privileges.

Keep the existing explicit ENERGY_MAP_STATE_ENABLED and private
ENERGY_MAP_STATE_DATABASE_URL, separate migration/runtime/analytical roles and
global analytical read-only PGOPTIONS. Do not remove source protection. Follow
[user-buildings.md](user-buildings.md) for owner schema and backup configuration,
then apply these **reviewed placeholder operator instructions**, not live startup:

```sh
poetry run alembic heads
poetry run alembic upgrade d4e8f021bc63
```

```sql
REVOKE ALL ON energymap_state.energymap_user_building_shares FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON energymap_state.energymap_user_building_shares TO energy_state_writer;
-- No UPDATE, TRUNCATE, REFERENCES, TRIGGER or schema CREATE needed for shares.
-- Retain USAGE and the existing seven mutable-owner-column UPDATE grants.
```

Grant neither source writes nor personal-table reads to analytical readers.
Runtime roles must not own tables, inherit privileged roles or have CREATE,
superuser/BYPASSRLS/grant options. Startup does not auto-migrate. Apply reviewed
migration/grants before enabling this backend; a missing share prerequisite
makes personal state/readiness fail closed while public source remains usable.
`/healthz` is liveness, `/readyz` is area readiness, `/readyz/state` is independent
read-only state schema/role readiness, not identity/backup verification.

Take a **full-data** private custom schema backup using `umask 077` and
`pg_dump --schema=energymap_state --format=custom --file=/private/state.backup`.
Never use --schema-only for recovery. Backups contain personal text/principal
identity and capability digests: encrypt, restrict, rotate and verify recovery.
Restore into a separately guarded/provisioned target with
`pg_restore --no-owner --no-privileges --dbname=<new-target> /private/state.backup`.
Reapply schema USAGE, owner-column and share SELECT/INSERT/DELETE grants; review
PUBLIC/inherited ACLs and reader denial. Compare complete rows, UUIDs, revisions,
UTC timestamps, sparse null/zero documents, summaries and digests privately,
then verify a persisted capability from a second API interpreter. Protect
migration metadata/config separately; the schema archive omits public source and
Alembic version metadata. Persistent storage/backups require deployed verification.

**Destructive share downgrade:** `poetry run alembic downgrade c7d3e921ab40`
deletes every capability and invalidates every link. It leaves saved owners,
schema and analytical/source data intact. Re-upgrade creates empty sharing; only
a full private-state recovery restores former capabilities. Do not downgrade
older owner/area/source revisions accidentally. Test failed migration rollback,
new-revision-only downgrade and recovery on disposable PostGIS via runtime_doctor.

## Log and proxy privacy

Every direct launch uses:

```sh
poetry run uvicorn src.api.main:app --log-config src/api/logging.json
```

The packaged entrypoint uses the same shipped configuration under /app/src.
It applies final-format redaction to root application, Uvicorn access and error
handlers, after exception/argument formatting, including encoded paths and query
strings. Token-shaped standalone strings and bearer credentials are also removed.
Do not add a raw alternate handler, SQL parameter/body/request-URL logging or
APM request capture. Custom launch commands/loggers must use the same formatter
boundary. Retain method/status and redacted route diagnostics. Docker retains
only the redacted stdout/stderr emitted by that configuration.

The following conservative **placeholder Nginx example** suppresses access/error
logs for resolver requests when conditional error redaction is unavailable. It
is not deployed here. Adapt location ordering to the actual ingress; ensure no
regex/alternate server/global logger, CDN or tracing exporter captures raw URI.
Apply private cache bypass to the owner namespace too.

```nginx
# Within the reviewed server; normalized URI matches encoded route components.
location ^~ /energy-data/shared-buildings {
    access_log off;
    error_log /dev/null crit;
    proxy_cache off;
    proxy_no_cache 1;
    proxy_cache_bypass 1;
    add_header Cache-Control "no-store" always;
    # No URI suffix/rewrite: preserve original encoded request URI upstream.
    proxy_pass http://energy_api;
    # Disable ingress request tracing/APM and any inherited alternate URI logs.
}
location ^~ /energy-data/user/buildings {
    proxy_cache off;
    proxy_no_cache 1;
    proxy_cache_bypass 1;
    add_header Cache-Control "no-store" always;
    proxy_pass http://energy_api;
    # Default pass-through preserves Authorization/If-Match/If-None-Match;
    # retain upstream ETag on 201/204/error responses, never strip quotes.
}
```

Parent ingress/WAF/load-balancer errors may log a URL before location selection.
Use a dedicated reviewed server with global access logging disabled and global
error_log /dev/null crit (and no inherited alternate destination) if that layer
cannot redact both normal and rejected requests. Disable request-URI/upstream-URI
fields in proxy logs, tracing/APM, CDN and analytics at every earlier hop. Reject
unpreserved encoded slashes as an integration incompatibility. Do not claim the
location snippet alone protects pre-routing errors or an upstream CDN.

Operator verification uses a throwaway capability, then checks success, malformed
path, unknown/revoked/deleted token, OPTIONS/405, source/state failures and an
injected unexpected error. Inspect every access/error/upstream/APM destination
privately for raw and encoded URL/token/query spellings; assert no retention.
Check no Age/cache HIT/304 after rotate/revoke/delete and unchanged immediate
404s, plus ETag/conditional/CORS forwarding on 201/204/412/428. Check frontend
Referrer-Policy and analytics exclusions in the persistent Brave Personal
profile. These deployed checks, actual issuer/audience/client/ordinary-user token
smoke test, state provisioning/storage/migrations/grants/backup recovery,
production-scale performance, frontend/UI/browser acceptance and deployment
remain external and unverified by local backend fixtures.
