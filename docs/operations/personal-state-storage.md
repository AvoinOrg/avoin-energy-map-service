# Dedicated personal-state database operations

This contract applies to NEW isolated Energy state databases. It does not adopt
combined/source databases. F003.1 implements and proves disposable tooling;
F003.2 applies it to the authorized sandbox only after independent review.
No command publishes, deploys, edits source ACLs or enables a frontend.
Discover the independent head with `alembic -c state-alembic.ini heads`; online
use of that environment requires the guarded tool-supplied connection.
The [sandbox outcome record](personal-state-sandbox.md) distinguishes applied
resources, completed checks and the remaining review/release gates.

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
(disabled statement/duration/parameter/error-statement logging, verified
`log_transaction_sample_rate=0`, and the fixed preload predicate below).
If it cannot establish that policy it fails closed; it never changes shared
logging globally. Driver/subprocess failures use fixed error categories.

### Bounded stock scheduler compatibility

Empty shared preloads remain supported. The only nonempty accepted spelling is
exactly `pg_cron`, with empty session/local preloads. The fixed implementation
in `logging_policy.py` requires PostgreSQL 17.2, the standard PG17 library
directory and `$libdir` loader path, no pending shared-preload restart, and
server-read SHA256 equality for the reviewed module and control bytes. These
pins came from `postgresql-17-cron 1.6.4-2.pgdg120+1` in immutable sandbox image
`sha256:0fea25c4d3ffb65a8cd7148af702544d835baa3c208a7fe215f8c6f65f48a9f9`.
The SQL extension default version `1.6` is not the package version. Neither an
extension nor a scheduler job is needed in the provisioning database. Additional
libraries, aliases, paths, changed bytes/version or unreadable provenance fail
before credential encryption/DDL. No descriptor, CLI option or receipt can waive
this check. An image/package upgrade requires a new review and updated pins.
The fixed Linux guard also reads this fresh backend's `/proc/self/maps`, requires
the exact nondeleted mapped module path, and requires module/control modification
and change timestamps strictly before the postmaster startup second (the file
view has second precision). This ties installed bytes to inherited mappings;
unavailable provenance fails closed. At application time the doctor also verifies
loaded-file/package/image parity over the authenticated administrative channel.

The source rationale uses pg_cron 1.6.4 commit
[`9490f9cc9803f75105f2f7d89839a998f011f8d8`](https://github.com/citusdata/pg_cron/tree/9490f9cc9803f75105f2f7d89839a998f011f8d8/src).
In [scheduler initialization and execution](https://github.com/citusdata/pg_cron/blob/9490f9cc9803f75105f2f7d89839a998f011f8d8/src/pg_cron.c),
initialization registers a scheduler background worker and relcache invalidation;
`CronLogStatement` branches log scheduled commands/results. Ordinary statement
execution hooks are absent from the reviewed C sources. The supported inference
is that this stock scheduler does not intercept direct-session role DDL. It
must be supported by isolated actual-log tests with synthetic secrets and a
positive capture control before live application. This is not a claim about
unknown instrumentation, modified binaries or administrator/server compromise.

`cron.log_statement=on` is a scheduler setting, not proof of direct-session
auditing. It is postmaster-only and must not be changed by the provisioner.
All native controls remain session-local and are read back exactly:
`log_statement=none`, `log_duration=off`, both duration thresholds `-1`,
transaction sample rate `0`, error-statement threshold `panic`, and both
parameter lengths `0`. Denied or ineffective controls produce only
`unsafe_logging`. Direct SCRAM credential DDL stays on the short-lived protected
admin connection; never submit it to cron or store it in scheduler tables.

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

### Persistent sandbox startup adapter

The active HBA is on container overlay. Its prefix must also be reconstructed
through the existing persistent custom-script bind before any temporary or final
listener. `sandbox_startup.py` is a fixed adapter for the immutable image above;
it is not a general entrypoint manager. Frozen private copies of it, `hba_file.py`
and the same `entry-request.json` belong under `/scripts/custom/energy-state/`
(directory 0700, files 0600, expected sandbox service identity 101:103).
The request contains no credential. Nothing installs an interpreter or application
dependencies on the shared server. The existing `/usr/bin/python3 -I` executes
only the fixed protected helper, ignoring ambient Python paths and user packages.

The image invokes the sourced custom modifier as root. The adapter checks that
caller explicitly; private controls retain exact service ownership 101:103 and
0600 modes. Only the fixed pinned native base-script paths accept the observed
root:root/0755 pre-repair phase or service101:103/0755 phase, with the same exact
parent identities/mode. Persistent custom scripts retain exact service ownership.
The fixture must exercise untouched root-owned image scripts, without an artificial
pre-chown, then service-owned initialized replay. Before HBA mutation the short-lived guard clears supplementary
groups and permanently sets real/effective/saved IDs to 101:103, then verifies
the transition. It uses the unchanged owner-only HBA helper after that descent.
The enclosing startup shell retains its original identity and behavior.
On the native initialization path the completed HBA can still be root:root/0644;
database setup normally repairs its ownership later. Before descent the adapter
moves only that ownership repair forward: it requires a regular single-link
0644 file, exact unprefixed generated baseline, unchanged inode/content metadata,
and a service-owned 101:103/0755 parent, then changes only that file to 101:103.
Bytes and mode are read back unchanged. Other unexpected ownership refuses;
already service-owned files are not changed. The owner-only HBA helper's checks
remain intact, and no live ownership change is part of installing this adapter.

The earlier configuration modifier repeats its exact `max_connections = 1000`
line at the existing `shared_buffers` site on each startup. For chain validation,
the adapter reduces one contiguous run of that exact line to a single copy
**in memory**, then requires the entire result to match the pinned configuration
script. It changes neither the configuration script nor modifier behavior.
Missing, moved, separated or differently spelled insertions and any surrounding
byte drift remain errors. The transitive user, SSL and replication setup scripts
are pinned along with the entrypoint and HBA-generation chain.

The existing HBA modifier's original bytes are preserved. Insert only the
adapter's checked `NORMALIZE_CALL` after its shebang and append `PREPARE_CALL`.
Both failures execute `exit 1` in the enclosing sourced shell. The old modifier's
sed injection otherwise repeats on the same overlay at an ordinary restart.
Normalization accepts only the pinned pristine file, the single canonical prior
injection, or that same file with this guard. It backs up the canonical file,
removes only the known prior injection/guard to the pinned pristine bytes, then
the unchanged sourced body inserts the additions exactly once. Unknown edits or
duplicate injections are refused; arbitrary drift is never normalized.
After the original modifier completes,
`prepare` verifies exact setup/entrypoint byte pins and metadata, then atomically
adds `GUARD_CALL` before the pinned unique initialized-path `return 0` and at the
**end** of `/scripts/setup-pg_hba.sh`. The initialized return otherwise skips the
end-of-file guard on replay. It must re-establish a missing prefix on the exact
known baseline or refuse drift before a listener; an old prefix alone is not proof
that the startup guard executed. The end-of-file guard is after
the existing additions source (line 82) and final HBA write (line 86), before
base database setup (line 67) can start its initialization listener. The base's
final listener is later (line 100). A guard attached only inside the additions
hook would run too early. Guard failure explicitly exits the caller even when
the script is sourced in a function or conditional; `set -e` alone is insufficient.
Preparation takes a protected nonblocking startup lock and fsyncs an exclusive
0600 `setup-original.backup` in the persistent private directory before replacing
the generated setup file. Existing unrelated backup contents are refused.

The guard checks the chain again and calls the exact frozen `hba_file.change()`.
The prefix, suffix digest and file metadata must match the reviewed active-file
contract. A repeat accepts only the same managed bytes. Missing files/interpreter,
drift, wrong identity, links or a changed baseline refuse startup. The reviewed
baseline already has SQL parser validation; before any live LOGIN/reload, the
doctor must separately repeat real parser/reload and entry-success/refusal checks.
The adapter starts no listener, issues no SQL and changes no shared setting.

Before live installation, protect exclusive byte-exact original backups of
every changed persistent file, record UID/GID/mode/hash, serialize against
administrative edits, install atomically and read back. Refresh the current
image, whole startup chain and baseline first. Unknown bytes are never adopted.
Install/read back the persistent integration and active prefix while both new
accounts are absent/disabled; only then may reviewed provisioning enable LOGIN.
Keep a rollback receipt and all backup material private. For rollback first
quarantine only marker-verified new accounts, then remove the exact persistent
modifier wrapper/owned new files in reverse order and restore the active HBA
through its guarded rollback/reload. Never remove confinement for enabled roles.
If the generated setup file already has both guards, restore only its exact pinned
original bytes with metadata-preserving CAS before removing the helper directory.
Construction, reversal and restart normalization accept only the exact two-site
managed form; a moved or missing call is drift.

The actual-image isolated proof must execute the copied complete sourced chain
on initialization and existing-data paths, instrumenting **every** listener
boundary. Require the final original suffix/order/metadata, repeated-start
idempotency, failure propagation with no listener, drift refusal, and exact
rollback. Pure helper tests are additional evidence. Neither copy tests nor
persistent-bind observations establish a live reboot/recreation result. No
shared restart/recreation is part of this operation. Independent pre-application
review must approve both the concrete adapter proof and credential-log proof;
then the doctor may perform only the reviewed reload-only live recipe.
Keep the server filesystem access path and private receipts out of Git.

## Scoped sandbox persistence probe

`python -m tests.sandbox_state_probe {create,read,cleanup} --config-file
/absolute/private/target.json --probe-file
/absolute/private/target-work/synthetic-probe.json --allow-sandbox-synthetic`
is an explicitly authorized sandbox check, not a destructive fixture suite.
Use the same reviewed sandbox descriptor and exactly
`private_dir/synthetic-probe.json`. Run each operation in a separate interpreter.
The helper exclusively reserves and durably writes its private cleanup authority
before connecting, creates one synthetic owner/saved/share pair, and reports only
success/failure. Repeated or concurrent creation refuses an existing authority;
it never overwrites it or adopts an existing row.

`read` verifies only that exact synthetic UUID, reserved test issuer/subject/key,
revision/strong ETag and capability digest. It rehearses an allowed column update in a savepoint and
requires PostgreSQL permission denial for a forbidden owner-column update, also
within a savepoint targeting only that record. Neither change is committed.
Persisted JSON summaries use the production `user_buildings.stored_summary`
loader: strict JSON timestamp parsing and exact building-key matching remain
enforced. Python-mode validation of JSON-decoded timestamp strings is unsuitable
for this strict DTO.
Keep the record and protected authority through backup/restore acceptance, then
`cleanup` deletes only that exact synthetic saved identity and verifies its
share cascaded. Retain the private authority until cleanup is confirmed. Do not
replace it with an arbitrary owner/key, point fixture suites at the sandbox, or
print its capability or contents. Recovery operation checks on a disposable
restore use the existing guarded `tests.state_recovery_probe` helper instead.

## Migration and runtime transport

Independent revision `es0001_saved_share` reuses only frozen upgrades
`c7d3e921ab40` then `d4e8f021bc63`. Its version table lives in
`energymap_state_meta`; no public/source stamp or source lineage is installed.
The frozen bootstrap's corresponding global head is `d4e8f021bc63`. Personal v2
appends `es0002_personal_v2` / combined `e5f102acd890`; see the compatibility
section below for its explicit disposable-only existing-target upgrade. Unknown
versions/drift still fail; no in-place adoption or speculative converter is supported.
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

## Personal v2 compatibility (F004.2 local delivery)

Supported isolated profiles are exactly historical `es0001_saved_share` and current
`es0002_personal_v2`; the legacy combined successor is `e5f102acd890` after
`d4e8f021bc63`. Both successors change only the validated state document-version
check to admit 1 or 2. No columns, source relations, rows, grants, identities,
indexes or shares are rewritten. Historical migrations/catalog remain frozen;
`expected_catalog_v2.json` is the independently compared current profile.
Unknown revisions/constraints/drift still fail closed. No PostGIS is added to the
isolated database. Writer column/table grants are unchanged.

Default `/readyz/state` recognizes either supported schema. The explicit
`?personal_schema_version=2` probe requires the actual validated v2 check. On a
healthy historical schema, v1 continues and v2 writes return sanitized 503
`personal_schema_upgrade_required` before mutation. The runtime writer neither
reads isolated migration metadata nor receives migration rights; combined-layout
readiness uses actual table constraints and privileges.

New `apply` provisioning uses the isolated head. Completed existing targets remain
verification-only: `apply` never silently upgrades them. `upgrade-personal-v2`
with the existing private `--config-file` is the explicit migrator operation. In
this feature it accepts **disposable mode only**. It requires a completed bound
managed target, credentials/output readback and exact pre/post schema/ACL audits,
uses the existing advisory lock and a transactional additive migration. The
feature does not authorize executing it against a live target. Later operator
release planning must provide an expressly authorized existing-target path.

Backups record the actual known profile/revision. Old v1 archives remain
restorable into a NEW managed disposable target: restore and audit the old
profile/full row fingerprint first, reconstruct exact grants with writer NOLOGIN,
then run the explicit additive migration, audit the new profile and identical
saved/share data, and only then admit the writer. The intentional metadata-table
revision change is compared separately from complete saved/share fingerprints.
A retry after restore or upgrade commit never replays the dump over populated
data; it validates archive equality and the exact known old/new profile before
continuing. No arbitrary drift/adoption is allowed. Latest mixed v1/v2 archives
preserve complete rows and active capability digests.

A downgrade refuses data-bearing v2 rows. Old binaries cannot read format 2;
migrating the constraint alone does not make a release safe. Load/release a
compatible backend before enabling any v2 writer. Binary rollback after v2 writes
requires a compatible reader or a separately reviewed recovery into a NEW target,
never truncating fields, restamping, or a hidden downgrade. No live migration,
service restart, deployment or sandbox schema upgrade was performed here.

The same additive v2 constraint accommodates count-only v2 and full property v2
JSON, including closed grade/assessment-scale, estimate-as-of year and integer
individual dwelling/share EUR cents/year. No further DDL/grant change is needed
for those optional leaves. Backup/restore must compare full JSON and active
capabilities across mixed v1, count-only-v2 and full-v2 rows; neither migration
nor recovery may drop unknown/null/zero distinctions. Stored structurally valid
estimate years survive current new-input policy changes and unrelated PATCH.

## Personal v3 reader and recovery boundary

The additive isolated head is es0003_personal_hvac_v3 (combined lineage
f6a213bde901). Exact profiles es0001_saved_share and es0002_personal_v2 remain
recognized; no frozen migration is edited. New head only extends the private
recognized format check to1/2/3; tables, JSONB storage and narrow grants stay the
same. `upgrade-personal-v2` still targets exactly2 and refuses a downgrade from3.
New `upgrade-personal-v3` upgrades either historical profile on an explicitly
guarded disposable target only. No live activation is authorized by this tooling.

Backup manifests bind their actual profile. NEW-target restore verifies the
archive's exact historical catalog and complete row fingerprints before upgrading
to3, admitting only explicit1→2/1→3/2→3 metadata transitions. Saved/share data
must remain byte-equivalent. Upgrade/recovery interruption leaves the writer
disabled until exact schema/grants/data verification completes; retry does not
replay an accepted archive. Downgrade refuses v3 rows rather than erasing fields.

Deploy read-capable consumers before enabling v3 saves. Older binaries cannot
serve the new stored format; rollback after v3 writes requires a compatible
reader/NEW compatible recovery target. V2 views of3 intentionally conflict409;
see [the API compatibility contract](../../src/api/personal-hvac-v3.md). This
feature's evidence is disposable/local only; full structured JSONB fields are
covered by the v3 fingerprint/recovery suite. Coefficients are deliberately
deferred. Independent review, actual ordinary-user/deployed/UI acceptance and
normal-runtime activation remain separate. No extra columns/grants are needed
for the structured inputs.

## Completed-renovations v4 candidate (not activated)

The closed candidate is documented in [personal-renovations-v4.md](../../src/api/personal-renovations-v4.md).
The November 10 owner decision defines thickness as the added insulation layer
in mm, including explicit zero. Renewed independent review and manager delivery
remain required; local fixture proof is not rollout approval. Keep the existing sandbox and F007
configuration unchanged. Historical profiles/catalogs/migrations stay intact.

Combined head `a714cf9e320b` and isolated `es0004_renovations_v4` change only the
validated personal-version check to admit 1/2/3/4. No row is promoted until an
explicit conditional v4 write. Readiness rejects requested v4 on older schemas.
Normal `apply` is still verification-only for a completed target. The explicit
`upgrade-personal-v4` command is disposable-only, advisory-locked and audited.
`upgrade-personal-v2` and `upgrade-personal-v3` still target their exact historical
profiles; neither downgrades a newer target. Downgrade locks the saved table
before checking and refuses ANY v4 row, including an empty/reset document.

A full backup admits exact historical v1/v2/v3 and current-v4 profiles. NEW-target
restore audits original schema/data first, then upgrades metadata to v4 while
keeping the writer NOLOGIN until final audits. Only named additive metadata
transitions are permitted; complete saved/share row fingerprints stay exact.
Committed-restore/upgrade retry never replays an archive. Never restore over an
existing origin, adopt an unknown target, remove fields or force a downgrade.

Ordinary repeatable local proof, using existing installed dependencies/service:

```sh
PYTHONPATH="$PWD:$PWD/src" .codex-orch/runtime/envs/avoin-energy-map-service-5iS2udd1-py3.12/bin/python -m tests.run_personal_renovations_checks
```

The checked-in helper uses the fixed protected disposable 55433 bootstrap
reference and creates uniquely marked temporary DB/role pairs; finalizers remove
only positively owned resources on success/failure. It never resets historical
fixtures or starts a service/container. Unknown cleanup retains private retry
files and fails. No secrets or live descriptors belong in commands/reports/Git.

Future activation needs the settled/frozen contract, compatible readers installed
before additive private schema activation and authenticated routing/proxy/CORS
configuration. Once v4 rows exist an old binary is unsafe rollback: retain a
compatible reader or recover to a verified NEW compatible target. Actual
ordinary-user save/reload/reopen, expiry/failure/ownership and report acceptance
remain separate from synthetic local auth/source tests. No deployment, live
private migration or public personal layer is performed by this feature.

## Personal utilities v5 candidate (not activated)

[Personal utilities v5](../../src/api/personal-utilities-v5.md) adds only optional
whole-building annual grid-import kWh and supplied-water m³ observations with
independent optional completed calendar years. Reviewed renovations v4 remains
frozen; v5 preserves all eight categories and conditional added-layer thickness.
No values are source-prefilled or converted to effects.

Latest isolated head is `es0005_utilities_v5`, combined `b825df0a431c` after
`a714cf9e320b`. The only DDL change is the exact validated recognized-version
check admitting 1–5. No rows, grants, columns or sources change. All four old
catalog profiles/migrations remain immutable; `RENOVATIONS_VERSION` explicitly
pins es0004 and latest `VERSION` is es0005. Old upgrade commands still target
exactly es0002/es0003/es0004 and refuse newer-target downgrade.

`python -m src.state_storage upgrade-personal-v5 --config-file <private-path>`
is explicit, audited, advisory-locked and **disposable-only**. It neither
activates normal/live private storage nor grants release authority. Completed
`apply` remains verification-only. Readiness recognizes only the exact validated
version constraint; historical healthy checks return upgrade-required503 for
requested5. Downgrade takes ACCESS EXCLUSIVE before checking and refuses every
v5 row, including empty/reset U; never strip answers or stamp old metadata.

Full dumps bind actual historical1–4/latest5 catalog and complete saved/share
fingerprints. NEW-target recovery restores/audits the exact archive profile,
reconstructs narrow grants, then upgrades only metadata to5. Every saved row's
version/JSON, UUID, timestamps, owner and share digest remains identical. Writer
stays NOLOGIN through final verification. Retrying committed restore/upgrade does
not replay pg_restore; unsafe targets, tampered archives and unknown catalogs
remain refusals. Retain protected 0700/0600 archives/bundles, never in Git.

Ordinary serial checks reuse the installed development Python/dependencies and
existing guarded127.0.0.1:55433 service:

```sh
env -i PATH=/usr/bin:/bin PYTHONPATH="$PWD:$PWD/src" PYTHONDONTWRITEBYTECODE=1 \
  .codex-orch/runtime/envs/avoin-energy-map-service-5iS2udd1-py3.12/bin/python \
  -m tests.run_personal_utilities_checks
```

Coder and functional reviewer run this same checked-in setup/test/teardown;
static review runs no tests or fixture preparation. `state_pair` registers
cleanup before provision and removes only unique marked owned DB/roles and
private directories, after owned clients/pools close. Shared services, historic
fixtures, source probe and uncertain outcomes are preserved. There is no
per-feature container or browser fixture. Unknown cleanup fails and retains
protected retry descriptors. Runtime doctor is for a concrete environment
failure, not routine test execution.

Before future activation, install compatible readers and separately review
private schema/config/routing, protected backup/recovery and rollout. Old binaries
are unsafe rollback writers after v5 exists; use compatible readers or verified
NEW-target recovery. Local synthetic auth/source and real disposable database
proof does not establish real Zitadel login, a deployed private origin, frontend
save/reset/reopen/session/ownership/report acceptance or calculated effects.
Normal6900/6903, sandbox live state and deployed public F007 are unchanged.
