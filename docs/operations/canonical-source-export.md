# Canonical identity/address export

Energy's offline producer emits exactly `manifest.json`, `main.ndjson`,
`building.ndjson`, and `address.ndjson`. It has no API, import, upload or activation
operation. Infra F009.2 owns derived generations and their retention; F009.3 owns
HTTP/Pelias and sandbox operation. Frontend F107.4.2 consumes actual delivery.
Independent canonical confirmation remains required.

The pinned contract is infra commit
`305dfc61867e4602d2156dc1e547a80481f5028a`. Exact offline schemas and their hashes
are maintained in `src/canonical_source_export/schemas/PROVENANCE.md`. The versions
are `canonical-input-v1`, `energy-identity-address-v1`, `canonical-match-v1`,
`canonical-coverage-v1`, and consumer `canonical-search-v1`. Python validation
implements mandatory UTF-8 byte, Unicode Cc/Cs and nonblank `x-text` rules. Schema
identifiers never trigger network resolution.

## Protected configuration

Use an existing authorized narrow analytical reader. Do not reuse application,
personal-state, admin, dotenv, libpq service or implicit local credentials.
The producer refuses ambient `PG*` variables and has no provisioning/grant path.
Source transport requires `verify-full` and an explicit trusted CA file. Only the
exact marked doctor fixture permits insecure loopback transport; current fixture
proof uses verified TLS too. Connection host is a single hostname; sockets,
host lists and alternate hostaddr/service/options are unsupported.

Create the private root under the effectively Git-ignored
`.codex-orch/runtime/f005-canonical-source-export-private/`, mode0700. Put each
configuration/receipt/stream file in mode0600 and private directories in0700.
No symlinks or hard-linked secret files are accepted. Project/OS ancestor
permissions are inherited trust boundaries; private directories inside that
boundary must be owner-only. Never put a password/DSN in argv, shell history,
reports or Git. Credentials are read only into memory from the protected JSON.

A descriptor is a closed object with exactly these fields (this is a shape,
not usable credentials; create the real descriptor privately):

```json
{
  "version": 1,
  "connection": {
    "host": "SOURCE_HOST",
    "port": 5432,
    "dbname": "SOURCE_DATABASE",
    "user": "EXISTING_NARROW_READER",
    "password": "PRIVATE_VALUE",
    "sslmode": "verify-full",
    "sslrootcert": "/existing/trusted/ca.pem"
  },
  "output_root": "/absolute/checkout/.codex-orch/runtime/f005-canonical-source-export-private/exports",
  "publisher_role": "APPROVED_NONSECRET_PUBLISHER_REFERENCE",
  "license_references": ["DATED_PRIVATE_LICENSE_AND_ATTRIBUTION_EVIDENCE_ID"],
  "fixture": null,
  "limits": {
    "rows": 1000000,
    "total_bytes": 4294967296,
    "record": 8192,
    "manifest": 65536,
    "buffer_bytes": 1048576,
    "overall_seconds": 900,
    "statement_ms": 30000,
    "lock_ms": 1000,
    "connect_seconds": 5,
    "disk_bytes": 17179869184,
    "scratch_bytes": 8589934592,
    "reserve_bytes": 2147483648
  }
}
```

Publisher/license references must be nonempty. A string reference is not evidence
that the underlying license or redistribution terms were reviewed. Tests use
explicit synthetic-license references; do not carry those into actual source
intake. Current projection intentionally cannot assert provider generation,
upstream snapshot/deletion completeness or national completeness.

Fixture descriptors are separate from exporter descriptors. The required test
launcher validates the doctor's exact endpoint/database/server marker before
any fixture DDL. It creates only the synthetic Main/Building/Address tables and
uses separate maintenance/reader credentials. The producer verifies configured
reader/database, UTF-8, effective isolation/read-only state, relation/type shape,
privileges and fixture OID/server version. Source relations must be ordinary
non-RLS tables. A reader cannot own them, write columns/tables, create schemas,
create temporary objects, or SELECT unrelated user relations. PostGIS's
extension-owned `spatial_ref_sys` metadata privilege is allowed but never read.
Do not relax these checks to make broad existing credentials pass.

## Commands and publication

Run from the repository root with the project's approved Python environment.
Poetry is the declared interface. On this host, use the doctor's private launcher:

```bash
F005_PROJECT_VENV="$PWD/.codex-orch/runtime/envs/avoin-energy-map-service-5iS2udd1-py3.12"
F005_POETRY="$PWD/.codex-orch/runtime/f005-canonical-source-export-private/tooling/bin/poetry"
export VIRTUAL_ENV="$F005_PROJECT_VENV"
export PATH="$F005_PROJECT_VENV/bin:$(dirname "$F005_POETRY"):$PATH"
```

Only code-only fixture retries are authorized. Doctor owns new runtime provisioning,
service lifecycle or fixture identity recovery. The required launcher rebuilds
only guarded synthetic source tables; do not call other features' reset helpers.

```bash
poetry check --lock
poetry run python -m unittest tests.test_canonical_source_export -v
poetry run python -m tests.run_canonical_source_export_checks --fixture-config .codex-orch/runtime/f005-canonical-source-export-private/fixture.json --infra-root /home/xbexbex/git/avoin-server-infra/secondary/avoin-map-geocoding
poetry run python -m src.canonical_source_export preflight --config-file .codex-orch/runtime/f005-canonical-source-export-private/source.json
```

`preflight` performs catalog checks and at most one bounded projected row per
relation, never a full COUNT, EXPLAIN ANALYZE, full export or source write. Row
estimates are catalog estimates and are labelled accordingly. Diagnostics contain
constant field/error codes and resource/count figures, never source values,
SQL/parameters, credentials or endpoint details. One failure diagnostic is emitted.

The following real-source command is **prepared only** in F005. It needs the
actual-source readiness evidence below and separate operational authorization:

```bash
poetry run python -m src.canonical_source_export export --config-file .codex-orch/runtime/f005-canonical-source-export-private/source.json
```

Without `--generation-id`, export allocates a random new identity. Explicit IDs
must match `[A-Za-z0-9][A-Za-z0-9_-]{0,95}`. Every attempt durably claims its ID
before reading source rows. Failed/capped/interrupted identities cannot be reused.
Restart with a new ID and a new snapshot; there is no delta or snapshot resume.

The output root contains complete generations as named directories, durable
`.claim-ID` tombstones, `.owner-ID` staging ownership records, protected
`.receipt-ID` manifest hashes, and `.ack-ID` successful-invocation acknowledgments.
Only the four contracted files belong inside a finalized bundle. No active
pointer is created. Old complete directories are never overwritten.

```bash
poetry run python -m src.canonical_source_export verify --bundle-dir .codex-orch/runtime/f005-canonical-source-export-private/exports/GENERATION_ID
poetry run python -m src.canonical_source_export cleanup --config-file .codex-orch/runtime/f005-canonical-source-export-private/source.json --generation-id FAILED_GENERATION_ID
```

Verify pins the protected manifest hash, independently parses exact delivered
bytes and recomputes statistics/coverage/counts/hashes. The stream-only bundle
checksum does not authenticate manifest metadata. Keep the private receipt during
recovery. A completed directory after a crash across publication can verify even
without `.ack-ID`; this proves a complete bundle, not an acknowledged prior
invocation. Cleanup refuses complete generations, substituted directories,
symlinks, missing ownership/claim evidence and a live writer. It deletes only
positively owned stale staging, retaining claims. A root lock serializes local
writers and cleanup. Never manually reuse/remove claims to retry an ID.

All streams and independent expected counts use one READ ONLY REPEATABLE READ
transaction. No joins, UUID/geometry/address-existence predicates, row skipping,
source sort, source DDL, source function side effects or new indexes are used.
Forward cursors fetch one bounded projected row at a time. SQL checks field byte
limits before fetching; oversize values abort rather than becoming truncated rows.
The source transaction closes before independent local validation/publication.

SIGINT/SIGTERM/deadline failure aborts. A separate timer cancels blocked source
work; statement/lock/idle timeouts backstop cancellation. Explicit connection
acquisition and TCP failure timeouts bound transport handling; cancellation and
cleanup are not a hard source SLA. SIGKILL leaves private staging/claims and the
server detects its closed socket. Linux `renameat2(RENAME_NOREPLACE)` plus file and
directory fsync provides publication on supported local ext4/XFS/Btrfs/overlay
filesystems; unsupported filesystems fail. No overwrite-capable fallback exists.
Use durable local storage, not NFS, object storage or temporary RAM filesystems.

## Projection and source gaps

| Stream | Preserved | Explicit gaps |
| --- | --- | --- |
| Main | Every exact key/fragment, raw permanent/property facts, independent display address_key/FI/postal tuple, point/polygon presence | Independent source row ID null; modified and display municipality not_projected; SV not_projected |
| Building | Every exact key, raw permanent/property/modified facts, point presence | Source row ID is analytical key only if actual single-column PK verified; no provider-generation assertion; no display address |
| Address | Every exact edge including duplicates/secondary/multibuilding/orphans, original address_key/FI/postal/municipality/modified facts, point presence | Stable row ID only if actual Address PK verified; SV not_projected; no raw Ryhti-id equivalence |

Opaque keys preserve leading zeroes, whitespace-only/surrounding whitespace,
Unicode normalization variants and reserved-looking/path characters. Main and
Building separately anchor the union. Main display never creates an Address
edge. Null known facts are missing; unsupported projection is not_projected.
Representable bad property/postal/municipality/timestamp strings stay invalid
with their original value. Blank/control/overlong values outside v1's raw field
domain abort; the exporter does not silently null, trim or sanitize them.
Unknown source columns, Swedish-looking future columns, private/user/share fields,
energy/HVAC/certificate payloads, geometry bytes and coordinates never enter output.

`src/etl/load_addresses.py` retains combined `address_fin`, `postal_office_fin`,
postal/municipality and modified text, but drops documented upstream Swedish
labels/postal office, street/number components and raw provider id. Building
loader's incremental upsert and `base_loader.py` mapping/batch failure behavior do
not establish a provider snapshot or reconciled deletions. Main's merge rebuild
and address/geometry multiplicity do not establish current ancestry/completeness.
No loader widening, provider fetch, source reload, translation or new crosswalk
is performed here. Swedish/raw retention needs separate approved acquisition and
version qualification. FI coverage is derived conservatively from emitted states.

Statistics use exact UTF-8 BLOB equality and full semantic values in bounded
private SQLite scratch. Duplicate identity semantics ignore row ID/modified only.
Conflicts include distinct nonblank raw permanent/property facts across both
components, including invalid facts. Every Address occurrence counts, including
orphans; missing-address ignores Main display; geometry considers identity and
Address facts. Relationship reuse is a sanitized aggregate receipt diagnostic.

## Budgets and actual-source readiness

Hard maxima are25million rows per stream,80GiB aggregate stream bytes,8KiB per
record including LF,64KiB manifest,16MiB streaming buffers,8h overall,60s source
statement and1s lock wait. Operators may lower limits; reaching a cap is failure,
never a successful sample. Default candidate limits above are lower guardrails,
not national sizing evidence or operator-approved load limits.

The buffer minimum is256KiB. One fetch, a bounded raw row/projected row/serialized
line and validation state are live at once; SQLite page cache is at most one
quarter of configured buffer, mmap disabled. Verification uses one fixed 8 KiB
read-ahead buffer, charged explicitly to the tracked streaming budget alongside
the current bounded line and parsed row. Exact statistics never load a whole
key population/hot-key fact list. Audit operations use indexed point lookups and
single scans, without data-sized temporary sort/group B-trees. Scratch's max pages
and actual file/sidecar checks share an explicit byte budget; source/file/audit
work checks the overall deadline. Interpreter/library total RSS is a different
measurement and is not claimed equal to the streaming cap.

Disk checks account delivered bundle bytes, scratch/index amplification, sidecars
and128KiB bounded control/manifest allowance, with a free-byte/inode reserve.
Previous outputs/other projects consume measured free space; they are never
removed for headroom. For the4GiB candidate stream cap plus8GiB scratch and2GiB
reserve, plan at least14GiB additional local headroom, plus retained outputs,
separate transfer copy/compression and operational margin. Verification rereads
bytes and rebuilds scratch after extraction scratch is removed; it does not
require two simultaneous audit databases. File-count reserve is32 inodes. No
compression ratio, national count or index amplification is assumed.

On2026-10-31 the candidate private destination measured277,872,005,120 free bytes
and43,907,120 free inodes before fixture/tests. This is a dated local observation,
not a capacity reservation; rerun preflight immediately before actual operation.
The2026-10-31 doctor verified TLS hostname validation, READ ONLY REPEATABLE READ,
required-table SELECT and bounded LIMIT1 projection access. Its catalog audit
observed114 Main/25 Building/10 Address columns, without exporting values. The
existing non-owner, non-superuser login has no observed source DML/schema-create
capability, but can SELECT29 non-system relations. It is broader than the
three-relation export reader and remains probe-only. No account/grant was changed.
Real export is
not operationally ready until all the following have specific evidence:

- Existing narrow reader with verified TLS and the required exact table/column/
  type/SELECT boundary. A broad source/application/admin account is insufficient.
- Numeric permitted concurrent scan/I/O load and maximum MVCC snapshot duration,
  intended scheduling window, and an identified cancellation operator. The900s/
 30s/1s candidate thresholds above are abort limits, not approval or measurements.
- Dated publisher identity, endpoint/source lineage, current license/attribution
  and allowed derived redistribution evidence. Historical Ryhti CC BY references
  are leads, not current terms verification. No unnamed external source is used
  to close this gap.
- Current full-population count/byte/duration estimates, available scratch
  amplification/headroom, intended refresh cadence/operational owner, and private
  transfer/receiver retention headroom. Bounded catalog estimates do not prove
  ingestion completeness. Provider generation/deletions/national coverage remain
  unknown unless separately evidenced; do not invent them from transaction IDs,
  timestamps or completed paging/upserts.

Transfer remains prepared only: use an existing approved authenticated private
channel, authenticate the publisher, pin the entire manifest SHA256 in a protected
receiver receipt, read back all stream hashes/counts before intake and retain
language/lineage gaps. Bare SHA values establish integrity, not publisher identity
or redistribution permission. No real transfer/channel readiness is claimed.
Every refresh is a new coherent full export/ID. Producer scratch cleanup belongs
here; F009.2/F009.3 own generation activation, retained reader windows/cadence and
operational capacity/retention. Never delete a retained generation here to make
space for another owner.

## Acceptance boundary

Required checks fail on missing fixture/dependency/infra validator and run DB
mutation cases serially. Emitted fixture files pass the actual infra
`readManifest`/`consumeBundle`; current uncommitted validator hashes are recorded
without claiming reviewed infra delivery. Its92-test pinned baseline alone is
not emitted-output proof. Fixtures contain synthetic data even though PostgreSQL
is real. No main6900/6903 runtime or browser is needed/operated.

Local producer delivery does not prove or perform national/full-source or bilingual
coverage, actual-source export/transfer, operational capacity/cadence/retention,
public index/API, Pelias integration, generation activation, deployment, consumer
or ordinary-user acceptance, source reload, or HVAC completion. Review and exactly
one manager-owned scoped LOCAL feature commit remain required before the manager
publishes this checkout's stable delivery receipt. No push or extra notification.
