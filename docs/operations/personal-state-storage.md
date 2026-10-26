# Dedicated personal-state database operations

This contract applies to NEW isolated Energy state databases. It does not adopt
combined/source databases. F003.1 implements and proves disposable tooling;
F003.2 applies it to the authorized sandbox only after independent review.
No command publishes, deploys, edits source ACLs or enables a frontend.
Discover the independent head with `alembic -c state-alembic.ini heads`; online
use of that environment requires the guarded tool-supplied connection.

## Resource and connection contract

Live names: database `energymap_personal_state`, owner/login
`energymap_state_migrator`, runtime/login `energymap_state_writer`.
Create from template0, UTF8, C collation/ctype. Both roles: NOINHERIT,
NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOREPLICATION, NOBYPASSRLS, no
memberships. The writer owns nothing. Administrator is used only by tooling.
Migration credentials never enter the API environment. Analytical reads retain
an independent connection to the existing source database and read-only options.

| Object | Writer rights, without grant option |
| --- | --- |
| Dedicated database | CONNECT |
| energymap_state | USAGE |
| energymap_user_buildings | SELECT, INSERT, DELETE; column UPDATE on display_name, schema_version, overrides, scenario_inputs, last_known_summary, revision, updated_at |
| energymap_user_building_shares | SELECT, INSERT, DELETE |
| public, energymap_state_meta, all other objects | none |

Revoke database PUBLIC CONNECT/TEMP/CREATE and public schema privileges. Owner
creates only two data relations and `energymap_state_meta.alembic_version`.
Revoke PUBLIC function EXECUTE and type USAGE defaults for the owner. No default writer grants.
Audit effective/direct privileges, column grants, memberships and ownership,
PUBLIC/default ACLs and analytical-source isolation. Support two explicit
source-entry modes: existing effective CONNECT denial (`acl`, the default), or
new-account authentication confinement (`hba`). In ACL mode both new accounts
must lack source CONNECT including PUBLIC/inherited rights. In HBA mode the
source's existing PUBLIC CONNECT stays intact, but fresh valid accounts must be
refused at entry. A connected role can write large objects through PostgreSQL
built-ins even without source table/schema DML; read-only options are insufficient.
Both modes retain source CREATE/table/column/sequence/security-definer audits.
Neither mode modifies source ACLs. The known sandbox requires HBA mode.
No PostGIS is needed: saved documents and digests are non-geometric.

## Protected descriptor and commands

`python -m src.state_storage {inspect,apply,verify,backup,restore,entry-prepare,entry-reload} --config-file /absolute/private/target.json`

Only paths and operation names enter argv. JSON is closed (unknown keys fail).
Required keys: `mode` (`disposable` or `sandbox`), `host`, integer `port`,
`maintenance_database` (`postgres`), `admin_user`, `admin_password_file`,
`system_identifier` (pg_control_system identity), `source_database`,
`source_reader`, `database`, `migrator`, `writer`, `provisioning_id` (UUID),
`private_dir`, `note_file`, `runtime_file`, `sslmode`, `sslrootcert`,
`disposable_insecure`, `client_bin_dir`. For disposable mode,
`admin_password_file` is the doctor's protected synthetic password file. For
sandbox mode that key MUST name the authoritative private GIS note
`/home/xbexbex/syncthing/work/avoin/projects/avoin-infra-stuff/gis/secrets.md`:
the tool structurally selects its unique sandbox/PostGIS PostgreSQL administrator
section, binds the username to `admin_user`, and consumes the password only in
memory. A later application's Owner fields are never selected. Do not create an
extra administrator-password file or copy that password into a bundle/output.
Optional `sslcert`, `sslkey` must be paired. Optional `source_entry` is exactly
`{"mode":"acl"}` or
`{"mode":"hba","baseline_sha256":"<64 lowercase hex>","reader_password_file":"/absolute/protected/source-reader-password"}`.
The HBA digest pins the entire original root HBA file at policy preparation time;
reader credentials stay protected and are used only for a fresh read-only health
probe. F003.2's runtime doctor must consume the existing reader reference privately
and supply this protected input; do not print/copy it into reports or the new
state note. HBA mode requires verify-full. All fields participate in retry binding.
Backup adds `backup_dir`; restore also adds `restore_from` (backup directory).
All filesystem paths are absolute, with no symlinks or '..'. Descriptor, password,
outputs, bundle, manifests and archives must be owned regular files 0600;
private working/backup directories must be 0700. The credential note may have an
existing vault directory; its file is still 0600. Prepare directories explicitly.
Never source/eval descriptors. `inspect` is read-only and reports status only.

Sandbox names are fixed as above. Disposable resources must match
`energymap_f0031_[a-z0-9_]+_test`, at literal loopback, with explicit opt-in;
never aim old truncating fixtures at these databases. Pin instance ID, maintenance
identity and transport before any DDL. Reject source, maintenance/template,
production names and unrelated collisions. New roles/db have a provisioning UUID
comment tied to the private credential bundle; comments alone cannot adopt data.

Acquire the bounded maintenance-database advisory lock before reading or
preparing `private_dir/bundle.json`. Generate separate credentials once into that
bundle before role creation. A rejected concurrent invocation never reads,
generates or persists retry credentials. The bundle is the durable retry
authority. Roles start NOLOGIN; transactional
role creation precedes nontransactional CREATE DATABASE, with bounded advisory
serialization. Bind markers and bundle before/after that boundary. Harden new DB,
run transactional migration/grants, audit, enable writer, probe real runtime, then
atomically finalize and read back outputs. A retry checks exact managed state and
uses the same credentials; it does not rotate or reset. Drift/collisions fail
closed. An interrupted operation may leave a disabled new resource; resume the
same descriptor/bundle. Do not delete the bundle until outputs are verified. A process interruption
between CREATE DATABASE and its marker leaves an unmarked empty database;
normal apply deliberately refuses it. Delegate a read-only identity/owner/empty
catalog/credential-boundary assessment to runtime_doctor and the manager before
explicitly restoring the missing marker. Do not adopt a database on name alone,
remove resources, or reset data to make a retry pass.
After completion it retains only binding/phase and credential hashes; passwords
remain only in the two intended outputs. A failure between two output writes
retains the bundle so finalization can resume without rotation. Never repair an
unrelated resource, drop a live database or use an administrator as the runtime.

Before password DDL the provisioner verifies session logging is secret-safe
(disabled statement/duration/parameter logging, verified
`log_transaction_sample_rate=0`, and no audit/preload hooks).
If it cannot establish that policy it fails closed; it never changes shared
logging globally. Driver/subprocess failures use fixed error categories.

F003.2 outputs are fixed:
`/home/xbexbex/syncthing/work/avoin/projects/energymap/reference/personal-state-sandbox-credentials.md`
(frontmatter `sensitivity: secret`, 0600) and
`.codex-orch/runtime/f003-sandbox-state/energy-state.env` (0600, writer only).
Do not copy admin credentials into either. Existing Energy Zitadel configuration
stays in its existing protected file. Disposable outputs never use these paths.

## Confined new-account authentication (HBA mode)

PostgreSQL uses the first matching HBA rule; its parser view reports file contents,
not proof that those contents are loaded. See the PostgreSQL 17
[HBA contract](https://www.postgresql.org/docs/17/auth-pg-hba-conf.html) and
[parser view](https://www.postgresql.org/docs/17/view-pg-hba-file-rules.html).
This helper prepends exactly five rules for the two explicitly named NEW roles:
local/socket all-database and replication rejection; verified-client TLS state
access using SCRAM; all other TCP database and replication rejection. It never
matches existing users, groups or PUBLIC. Non-TLS state TCP is also refused.
The state allow rule is scoped to the new database and accounts; network/firewall
and application trust policy remain separate. No restart is used.

F003.2 performs this sequence through runtime_doctor using the existing
authenticated server administrative/SSH channel. F003.1 tests it only on the
pinned disposable instance. An unavailable authenticated file channel is a
concrete runtime prerequisite; never replace it with source ACL changes.

1. Discover `SHOW hba_file`, file ownership/mode and the SHA256 of its complete
   original bytes privately. Require a regular, single-link, nonsymlink HBA file
   owned by the server-local executing identity, with mode 0600, 0640 or 0644,
   and an owned parent that is not writable by group/others. Active configuration
   is distinct from private controls: requests/backups/locks remain strictly 0600.
   Refresh actual UID/GID/mode evidence at application time; never chmod a shared
   configuration to fit fixture assumptions. Pin that digest in the protected descriptor. Check parser errors,
   source-reader health, and name collisions before any file edit or role creation.
2. Run `python -m src.state_storage entry-prepare --config-file /private/target.json`.
   It verifies the pinned instance/admin/absent resource names and original HBA,
   rejects existing exact-user rule collisions, and writes the protected
   `private_dir/entry-request.json`. Keep descriptor/request private. If an earlier
   unrelated policy change alters the baseline, stop and rediscover before any
   resource exists; never silently rebind a managed target.
3. Transfer the reviewed standalone `src/state_storage/hba_file.py` and request
   over that authenticated channel to protected server-local paths. Run as the
   HBA file owner: `python3 /private/hba_file.py install --request-file /private/entry-request.json`.
   This is a file operation only. It validates fixed live/test name boundaries,
   writes/fsyncs an exclusive 0600 backup adjacent to the HBA file named
   `<hba_file>.energy-<provisioning_id>.backup`, then atomically prepends the
   deterministic block, preserving every original byte/order/UID/GID/mode. A
   matching rerun is safe; unexpected original/current bytes fail closed.
   A stable nonblocking file lock serializes cooperating helper invocations. Also
   serialize the operation with the administrative maintenance workflow;
   no concurrent external HBA editor is supported. Retain backup/request for rollback.
4. Run `python -m src.state_storage entry-reload --config-file /private/target.json`.
   It reads back exact bytes/baseline, verifies all five parsed rules and no parser
   errors, requests only `pg_reload_conf()`, waits at most 3 seconds for this
   backend's reload receipt, then freshly verifies existing reader access.
   This receipt is not entry acceptance. Proceed to ordinary apply/verify only
   after it succeeds. Migration LOGIN is gated on the policy audit too.
5. Apply/verify audits schema/grants and uses BOTH real new account passwords to
   connect freshly to state, then to source over the same verified transport.
   Accept only explicit database permission denial in ACL mode or explicit
   `pg_hba.conf rejects connection` for the correct user/database in HBA mode.
   Missing DB, bad password, TLS, DNS/network, no-HBA-entry and other errors
   are `source_entry_unproven`, never isolation. Current refusal classification
   requires PostgreSQL English FATAL diagnostics; other locales fail closed.
   Existing source-reader health must pass. Failed validation of incomplete
   provisioning returns writer to NOLOGIN and preserves credentials/rows for retry;
   output finalization failures do the same. A completed rerun is verification-only.
6. On parser/reload/probe failure retain the admin connection and use the protected
   provisioning evidence to set ONLY the two marked new accounts NOLOGIN before
   rollback. Recheck instance, both UUID role markers and no unrelated ownership;
   no name-only role alteration. On the server run the same file command with
   `rollback` instead of `install`. It accepts only exact installed or original
   bytes and the verified backup, never overwriting unrelated edits. Then run
   `entry-reload`: original baseline is accepted only if both new roles are absent
   or marker-verified NOLOGIN, parser is clean and source-reader access succeeds.
   Verify original bytes and existing client/service health after rollback. Leave
   failed new accounts disabled; runtime doctor/manager must assess a resume.
   Never restart the shared server or broaden an existing user's rules. A managed
   quarantined migrator is not automatically re-enabled outside normal provisioning
   stages; do not claim a successful retry after a full administrative rollback.

A later fresh recovery target has distinct roles and its own baseline digest and
protected request. Capture that baseline after any earlier policy installation,
before the new target bundle exists. Exact unrelated-user prefixes may precede
an existing target's block; verification rejects any preceding broad/group/regex
or matching-user rule. Each target's suffix must still match its pinned baseline.
Roll back stacked policies in reverse order. Keep completed live policies and
backups in place; recovery does not remove the original target's protection.
Keep the server filesystem access path and private receipts out of Git.

An active HBA on a container writable layer is not durable account confinement.
F003.2 must use the existing persistent sandbox custom-startup binding to recreate
this same new-account prefix after ordinary HBA generation and before client
entry, preserving original scripts/rules/order with protected backup, hash guards
and rollback. The primary's private read-only startup/parity evidence identifies
the existing hook; the sibling owns its narrowly scoped reviewed integration.
Verify construction on an isolated fixture/protected copy and read back the
persistent host/container binding. Do not claim reboot/recreation acceptance,
restart a shared server, install a live interpreter or deploy an application to
establish it. A one-time active prefix alone cannot satisfy sibling readiness.

## Migration and runtime transport

Independent revision `es0001_saved_share` reuses only frozen upgrades
`c7d3e921ab40` then `d4e8f021bc63`. Its version table lives in
`energymap_state_meta`; no public/source stamp or source lineage is installed.
The original global head stays `d4e8f021bc63`. Future changes append explicit
state revisions and expected catalog definitions. Unknown versions/drift fail;
no in-place adoption, downgrade or speculative upgrade converter is supported.
Recover a compatible export into a new target. Global metadata create_all and
source autogeneration must never initialize personal state.

Direct session PostgreSQL only, certificate AND hostname validation (`verify-full`).
State URLs forbid query parameters. Separate settings:
`ENERGY_MAP_STATE_SSLMODE`, `ENERGY_MAP_STATE_SSLROOTCERT`, optional paired
`ENERGY_MAP_STATE_SSLCERT`/`ENERGY_MAP_STATE_SSLKEY`. The sibling copies the
verified installed Node public CA bundle to protected
`.codex-orch/runtime/f003-sandbox-state/tls/public-roots.pem`; deployment must
mount it read-only at `/run/energymap-state/tls/public-roots.pem`.
No TLS require/prefer fallback, pooler, tunnel or server TLS change is supported.
Only literal loopback plus `ENERGY_MAP_STATE_DISPOSABLE_INSECURE=true` permits
`disable`. Nonempty ambient libpq redirect/TLS/service/passfile overrides fail;
analytical PGUSER/PGPASSWORD/PGHOST/PGPORT/PGDATABASE remain supported and are
explicitly overridden for state. PGOPTIONS is overridden without process-global
mutation. State stays lazy/disabled by default: pool 5, overflow 0, connect/pool
3s, lock 2s, statement 10s. Global analytical read-only and MIGRATE_ON_START=false
remain unchanged. Failures expose no paths, endpoints or driver text.

## Backup and fresh-target restore

`backup` requires a verified ready descriptor and `backup_dir`, an existing
empty private 0700 directory. It creates complete custom-format `state.dump`
and private `manifest.json` (0600), including both relations/data, independent
version metadata, definitions and ACLs. No cluster-role dump or password hashes.
Clients are PostgreSQL 17 under `client_bin_dir`; secrets use a protected
passfile and clean subprocess environment, never argv. Timeout is 180 seconds.
The manifest includes archive integrity, full data comparison hash/counts,
server/client/schema version, and this scoped role/ownership/ACL policy. It
contains private hashes; never print or commit it. Failed dumps lack a successful
manifest. Keep archives private and encrypt before any authorized off-host copy;
this tooling schedules/transfers nothing.

`restore` requires a NEW descriptor with distinct database/roles/UUID and
`restore_from`. It validates hash/manifest and an allowlisted archive inventory,
provisions an empty hardened target without bootstrapping state schemas, then
uses pg_restore --no-owner --no-privileges --single-transaction --exit-on-error.
It never uses --clean. If the archive includes public, remove only the verified
empty baseline public schema without CASCADE. If interrupted after that removal
but before pg_restore commits, repeat the same restore command: an already absent
public schema on the identified incomplete target requires no further removal.
A present public schema must still be empty and owned by pg_database_owner.
PostgreSQL 17 with --no-owner can omit recreating that baseline despite its
archive entry. After verifying restored schema and rows, the tool recreates an
absent empty public schema with pg_database_owner ownership and no PUBLIC rights
in the grant transaction; interruption rolls back that reconstruction too.
The writer stays disabled until restoration and audits succeed. Reconstruct
ownership, explicit
column/table/database/schema/default ACLs from this contract, never archive SQL.
Verify full catalog/version/grants/source separation, writer readiness and exact
row equality. Passwords/roles are recovered separately via new protected bundle;
original target and credentials remain intact. Restore is not live cutover.

If interrupted after pg_restore commits and before grants finish, repeat the
same `restore` command with the same descriptor/bundle/archive. For a managed
disabled writer in phase `migrating` (or the older interrupted `migrated` phase),
the retry verifies the complete schema and exact archived rows, rejects excess
privileges, then transactionally fills in missing contract grants and audits
them before enabling the writer. It does not replay pg_restore over those rows.
Enabled/completed targets receive full verification without privilege repair.

The focused regression command is
`python -m unittest tests.test_state_storage_recovery_db -v`. Runtime doctor must
prepare two distinct pristine disposable restore descriptors, supplied as
`ENERGY_MAP_STATE_RESTORE_RETRY_CONFIG` and
`ENERGY_MAP_STATE_RESTORE_MIGRATED_CONFIG`, against the guarded loopback fixture
and an existing protected backup. The tests require absent targets, interrupt the
real restore after commit and the grant transaction before commit, then verify
both retry phases, exact rows/credentials and real writer readiness. Repeating
the tests requires the doctor's exact guarded reset helper or new descriptors;
never reset the source/archive, existing state targets or sandbox for this test.

Run the database fixture suites serially, including provisioning and interrupted
restore tests. They intentionally share the same maintenance-database advisory
lock even when target database names differ; overlapping runs fail with
`provision_busy`.
The main `tests.test_state_storage_db` suite also requires a pristine restore
target. It creates a supported public-schema archive, interrupts before the
restore client commits, and verifies retry with unchanged credentials, exact
rows/version/grants and actual writer readiness. Obtain fresh descriptors or use
the doctor's exact guarded reset before repeating these creation tests.

Operator objectives: full backup daily and before maintenance/migration, seven
daily plus four weekly recovery points, RPO 24h/RTO 4h. These are objectives,
not configured or measured SLAs. Sibling must record actual protected destination,
automation/retention owner, first measured disposable restore, and read-only
persistent host-volume evidence. fsync/full_page_writes/synchronous_commit alone
are insufficient. No shared server restart, WAL/archive change or durability
claim without evidence.

## Future private geometry boundary

Later reserve `energymap_state_private.saved_building_geometry`, one row per
saved UUID with FK ON DELETE CASCADE to saved state only. Exact parent issuer
AND subject authorize access. Keep saved revision, provenance/source ID, nullable
source observation time, capture time, geometry fingerprint and freshness/status.
Use validated canonical point/polygon/multipolygon geometry, transformed from
its declared 3857 (fallback 4326) to EPSG:3067. Never infer shapes from addresses
or accept arbitrary geometry in saved JSON. Invalid/unknown geometry is unavailable.
No spatial resource, API, worker or trigger is created now.

Future authenticated backend layer joins the exact owner and uses FORCE RLS on
the projection with a separate non-owner GIS role: projection SELECT plus only
saved_id/owner_issuer/owner_subject/revision column SELECT on parent; no DML,
ownership or memberships. Trusted transaction-local issuer/subject equality and
parent EXISTS enforce access; missing/empty context denies it. Backend alone sets
context, transaction completion resets it, callers get no SQL credentials or
arbitrary owner settings. Distinct worker policy handles authorized refresh.
Share digests grant no GIS access. Save/refresh publishes only matching UUID and
revision; source changes independently stale the projection. Unknown lineage
never means fresh; failures may retain labelled stale geometry. Deletion cascades.
UI uses a separate authenticated layer, clears private caches on sign-out, and
never uses public tiles, shared caches, public GeoServer or official aggregates.

## Sibling acceptance checklist

After positive independent tooling review, freshly verify correct admin/instance,
verified TLS, source entry mode, name collisions, persistent host backing and
secret-safe logging. For the known PUBLIC-CONNECT sandbox use the reviewed HBA
procedure below; do not revoke PUBLIC or stall on an impossible role-only REVOKE.
Prepare protected descriptor/outputs; inspect/apply/verify reviewed source. Read
back secret note and writer env, verify actual readiness and denied privileges,
source independence, full backup and a separate disposable restore. Record
backup scheduling/retention evidence or its absence. Obtain independent review.
No deployed API, ordinary-human/core login, proxy/header/log/cache, frontend or
performance acceptance follows. Only the backend manager may dispatch the
owner-authorized frontend continuation after F003.2 review, live readiness,
private-note readback and sanitized final handoff; retain remaining release limits.
