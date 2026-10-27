# Sandbox personal-state outcome — 2026-10-27

Status: actual provisioning, persistence, backup/recovery and isolated readiness
checks pass; independent actual-outcome review returned **go**. This technical
record establishes neither a deployment nor ordinary-user/frontend acceptance.
Follow the [operations contract](personal-state-storage.md) for guarded
administration and rollback.

## Applied resources

| Resource | Verified actual state |
| --- | --- |
| Database | `energymap_personal_state`, template0, UTF8, C |
| Owner/migrator | `energymap_state_migrator`, separate from runtime/admin/source reader |
| Runtime | `energymap_state_writer`, non-owner, NOINHERIT, no privileged flags or memberships |
| Saved data | `energymap_state.energymap_user_buildings` |
| Share data | `energymap_state.energymap_user_building_shares`, digest-only capability, saved UUID FK/cascade |
| Independent version | `energymap_state_meta.alembic_version = es0001_saved_share` |
| Spatial resources | None: no PostGIS, geometry table, projection, trigger or public layer |

Guarded apply and repeat verification passed after both persistent startup and
active reload-only protections were installed. Both new accounts authenticate
over verify-full TLS to state and receive explicit HBA rejection from the real
source database. Existing source ACLs (including PUBLIC CONNECT), account policy,
data and reader access remain unchanged. The writer has CONNECT/schema USAGE,
saved/share SELECT/INSERT/DELETE and only the seven documented saved-column
UPDATE grants; it has no ownership, CREATE/TEMP, DDL, share UPDATE or grant option.
The full schema/version/default/effective grant audit and narrow-writer readiness
passed. Source and state connections remain distinct.

## Credentials and runtime configuration

The Energy vault's `reference/personal-state-sandbox-credentials.md` is the private
recovery note for the new roles. Its `sensitivity: secret`, expected ownership,
regular single-link metadata and mode 0600 were verified privately. The ignored
runtime environment is also 0600 and contains the writer connection only.
Administrator credentials remain in their original private infrastructure note;
neither frontend nor public documentation receives database credentials.

The existing dedicated Energy Zitadel API application's server configuration is
retained. Its non-secret handoff is under the backend's private orchestration
directory at `auth-integration-20261026/handoff.md`. Do not substitute management,
core OAuth or Hiilikartta credentials. Exact issuer is `https://auth.avoin.org`;
accepted project audience is `249571727204417539`. Existing live-provider evidence
uses a machine identity and does not establish ordinary-human login.

For a separately authorized release, inject the private writer settings and five
existing `ENERGY_MAP_ZITADEL_*` settings server-side. Mount the reviewed public CA
bundle read-only at `/run/energymap-state/tls/public-roots.pem` and point
`ENERGY_MAP_STATE_SSLROOTCERT` there; retain `ENERGY_MAP_STATE_SSLMODE=verify-full`
and explicit state enablement. Preserve analytical
`PGOPTIONS=-c default_transaction_read_only=on` and `MIGRATE_ON_START=false`.
State explicitly overrides its own options with bounded connect/query/lock waits.
Never render secret configuration to logs or put it in frontend settings.
These release instructions are prepared configuration, not an applied deployment.

## Confinement and persistence

The existing persistent custom-script bind contains the reviewed fixed adapter,
frozen helper/request, original-modifier backup and manifest. Its new-account-only
prefix is installed in the active HBA and re-established by checked startup calls
after final HBA generation and before the initialized early return. The protected
original suffix, ownership and mode remain preserved. Unknown image/script/library
or baseline drift fails closed. The immutable running image is
`sha256:0fea25c4d3ffb65a8cd7148af702544d835baa3c208a7fe215f8c6f65f48a9f9`;
the bounded logging exception is pinned to stock PostgreSQL 17.2 and pg_cron
package `1.6.4-2.pgdg120+1`.

The full exact-image disposable proof passed fresh initialization, same-container
initialized replay, prefix re-establishment, idempotence, drift/fault refusal and
exact rollback. Pre-exec observations covered temporary and final listeners,
with separate running-server HBA-path correlation. The live installation used
reload only. No live startup/reboot/recreation was performed.

Rollback requires marker-verified quarantine of both new accounts before removing
either protection, exact reverse persistent CAS restoration and guarded active
rollback/reload, parser/readback and existing-reader checks. Retain protected
originals and helper/request through rollback. Never disable confinement for
enabled accounts, adopt drift, reset data or restore over shared resources.

The first synthetic committed read exposed a probe-only JSON timestamp loader
mistake; production already used strict JSON deserialization with key matching.
Its exact reserved saved/share pair was cleaned up. The corrected helper now uses
that production loader; a focused regression accepts JSON timestamps and rejects
wrong keys, malformed timestamps and naive timestamps. Current focused checks:
47 passed, zero skips. The corrected create and read passed in separate fresh
interpreters with the real narrow writer. ETag/share checks and rollback-scoped
allowed/forbidden updates passed. After recovery acceptance, exact synthetic
cleanup and share cascade passed; its completed private authority was retired.
No real private owner records were inspected or reported by the probe.

## Backup, readiness and remaining acceptance

An on-demand full real-state recovery point was captured at
2026-10-27 02:30:23 UTC in the ignored local directory
`.codex-orch/runtime/f003-sandbox-state/backup/`: `state.dump` and `manifest.json`
are owner-1000 mode 0600 under a 0700 directory. Keep both private; they contain
complete saved/share/version data and scoped ownership/grant recovery metadata.
The snapshot includes the reserved synthetic pair used for the recovery proof;
that pair was subsequently deleted from the live database.

The target-specific PostgreSQL 17 client bridge uses the existing authenticated
administrative transport for this database's dump/inventory only. It replaced
the descriptor's binding-excluded client path; database identity, resource UUID,
credentials and confinement were unchanged. The original loopback fixture bridge
remains separate. To take another backup, prepare a new empty protected backup
directory through runtime doctor; never overwrite this accepted recovery point.

A NEW distinct loopback disposable target restored in **2.144 seconds** using
the guarded standard restore flow. Complete private row/version/grant comparison,
real restored-writer readiness and a second-process saved/share ETag and
revoke/rotate/delete rollback rehearsal passed. The live target was never a
restore destination. Existing archive/descriptor controls retain the reproducible
procedure; no private row/hash/token is exposed here.

This is one immediately available local, operator-created backup. No automated
schedule, retention operator/SLA, off-host replication or PITR capability has
been verified for it. The local archive remains with the backend checkout owner;
periodic retention ownership is still an operational follow-up. Daily backups,
seven daily/four weekly retention, RPO 24h and RTO 4h remain objectives.

Fresh read-only checks confirm persistent ext4 data backing, persistent startup
binding and enabled `fsync`, `full_page_writes` and `synchronous_commit`. These
observations and committed cross-process reads do not prove a live reboot,
container recreation, storage-retention SLA or cutover.

Combined existing dedicated auth and new writer settings validated in a
short-lived loopback Uvicorn process: `/healthz` 200, `/readyz/state` 200/no-store,
public `/readyz` 200 through the separate read-only source, and unauthenticated
owner listing 401/no-store. Analytical read-only `PGOPTIONS` and disabled startup
migrations remained intact. The test process was terminated; it is not an
available deployed API and did not perform ordinary-human login. Keep analytical
TLS overrides on their analytical connection; ambient libpq TLS overrides are
deliberately rejected by state transport. Parse protected environment files as
data, including conventional quoted values, never by shell evaluation.

The published API-contract baseline is `d0b3451`; storage tooling prerequisite is
reviewed local commit `1eef5552`. F003's reviewed source is committed locally;
no F003 remote push or deployment is authorized. Database creation does not
supply a deployed public API URL.

The final actual-outcome review returned **go** and the sanitized backend handoff
is finalized by the manager. Keep release/proxy encoded-path and header
forwarding, ETag and no-store behavior, redaction at all hops, ordinary-human
core login, independent owner access, deployed persistence and frontend/UI/browser
acceptance open. A separate manager-only continuation may authorize independent
frontend work; it does not close these release gates.

The API contracts are [owner records](../../src/api/user-buildings.md),
[sharing](../../src/api/building-shares.md), [building keys/DTOs](../../src/api/building-contract.md)
and [provider-token authentication](../../src/api/authentication.md). Core forwards
the provider access token; encode the entire opaque key, preserve quoted ETags
and conditional headers, partition caches by exact session principal and clear
them on sign-out. Shares remain explicit revocable read-only capabilities with
no-store/no-referrer and no token retention. A share grants no GIS publication.
Future geometry remains saved-UUID-linked, owner-filtered, separately reviewed
and private, with deletion cascade and no public tile/source/aggregate mutation.
