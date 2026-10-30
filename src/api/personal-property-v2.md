# Personal property v2 (F004.2 implementation boundary)

This versioned matrix follows the owner's 2026-10-29 decisions: A–G with an
explicit 2013/2018 scale, an estimate-as-of year, individual dwelling/share annual
maintenance charge, and saveable drafts. The conservative future-year restriction
below is a proposal, not an owner decision. The owner adopted the seven existing
purpose choices on 2026-10-30; see [catalog metadata and selection](purpose-catalog.md).

`S` means `scenario_inputs`. All personal reads use the exact write paths below;
effective scenario reads use `effective.S.<leaf>` and provenance/availability use
`S.<leaf>`. Override effective/provenance paths are shown separately.

| F107.5 control | Write/personal read path | Type, units and bounds | Effective / provenance; source/default |
|---|---|---|---|
| Label | `display_name` | Nonblank string 1–120 or null | `display_name`; private label, no official company identity/default |
| Purpose | `overrides.main_purpose` | Exact string 1–16/null; preserve leading zeroes; adopted selectable set required for new non-null input | `effective.details.main_purpose` / `details.main_purpose`; current source fallback on unset |
| Construction year | `overrides.construction_year` | Strict integer 1…current UTC year+5/null | `effective.details.construction_year` / `details.construction_year`; source completion year |
| N1 personal class | `S.personal_energy_class` | Null or closed atomic `{"grade":"C","scale":2018}`; grade A–G, strict scale 2013 or 2018; both required | `effective.S.personal_energy_class` / `S.personal_energy_class`; personal estimate only, no official/model fallback |
| N2 estimate time | `S.personal_energy_class_date` | Null or closed atomic `{"precision":"year","year":2020}`; strict integer 1…9999 structurally; proposed new-input bound current UTC year | `effective.S.personal_energy_class_date` / `S.personal_energy_class_date`; estimate-as-of year, no default |
| Residents | `S.resident_count` | Existing strict integer 1…10,000/null | `effective.S.resident_count` / `S.resident_count`; no backend default, null blocks frontend heuristic |
| N3 studios/one-room | `S.apartment_studio_count` | Strict integer 0…1,000,000/null; apartments | `effective.S.apartment_studio_count` / `S.apartment_studio_count`; no source/default |
| N4 two-room | `S.apartment_two_room_count` | Same independent count | `effective.S.apartment_two_room_count` / `S.apartment_two_room_count`; no source/default |
| N5 three-room | `S.apartment_three_room_count` | Same independent count | `effective.S.apartment_three_room_count` / `S.apartment_three_room_count`; no source/default |
| N6 four-plus-room | `S.apartment_four_plus_room_count` | Same independent count | `effective.S.apartment_four_plus_room_count` / `S.apartment_four_plus_room_count`; no source/default |
| N7 annual maintenance charge | `S.annual_maintenance_charge_eur_cents` | Strict integer 0…99,999,999,999/null; EUR cents/year; 123456 = EUR 1234.56/year for the individual dwelling/share | `effective.S.annual_maintenance_charge_eur_cents` / `S.annual_maintenance_charge_eur_cents`; no source/default |

Counts describe owner-reported room categories, without asserting kitchen
inclusion or an official census algorithm. Independent `overrides.apartment_count`
retains its 0…1,000,000/null contract. No forced sums, allocation, applicability
restriction, inequalities, inferred zero, company fee, monthly conversion,
accounting or tax meaning is introduced. The count/money ceilings are technical
input bounds, not recommended physical/economic values. Money accepts no float,
decimal/exponent string, boolean, rounding or conversion.

PUT omission removes answers; PATCH omission preserves siblings. Explicit null
retains intentional unknown; count/money zero is known. `unset` removes an exact
leaf and restores current source fallback for overrides, or absence for personal
inputs. No new field has a source/default. Label null or PUT omission clears it;
PATCH omission preserves it and label has no unset path. Partial class objects
are invalid; absent/null class and independently saved year are valid drafts.
PATCH replaces the entire class/year object; nested unset paths are forbidden.
Resetting/changing class never erases year or siblings. No certificate date,
month/day or save time is manufactured.

Proposed future-year policy: reject newly supplied estimate years after the
current UTC year (injectable clock). Alternatives current year+5 or unrestricted
planning years were not selected by the owner. Structural stored/read validation
retains 1…9999 and unrelated PATCH preserves historical years without reapplying
new-input limits. Explicit resubmission, including PUT, is new input.

New non-null personal values have origin=user, source=exact personal path,
is_override=true and status=available. Null is user_unknown. Omitted new fields
are origin=unavailable, status=missing, reason=no_personal_default. No observation
time is invented. Official certificate, published certificate and modeled facts
remain separate and immutable; matching assessment scale implies no provenance
relationship.

422 locations are `body.<write path>` for PUT and `body.set.<write path>` for
PATCH. Nested errors append the member (e.g. `.personal_energy_class.scale` or
`.personal_energy_class_date.year`); unset errors use `body.unset.<index>`.
Duplicate/conflicting unsets and unknown/read-only fields reject without echoing
input/context/SQL. Raw and merged private documents retain the 32 KiB UTF-8 bound.

## Saveability and current-input completeness

`input_status.property` is a closed derived object with policy_version
`property-required-inputs-2026-10-29`, saveable=true for a validated saved document,
completeness=complete|incomplete|unavailable, missing_paths and unavailable_paths.
Its lists contain only `details.main_purpose`, `details.construction_year`,
`details.floor_area`, `details.heating_method`, in that order. They describe the
current projection, never unsaved-buffer validation, section saves or durable
completion history. Any unavailable dependency yields unavailable; otherwise
known missing values yield incomplete; only four usable values yield complete.

Source-prefilled effective values count without confirmation. Explicit null is
known missing and suppresses fallback; existing valid floor-area zero counts as
present. Source outage without a personal answer is unavailable. Present purpose
must be selectable in the adopted pin: unadopted/unavailable catalog, unknown or
nonselectable historical code makes its qualification unavailable, without
changing the saved value/provenance. Known missing purpose remains missing even
when the catalog is unavailable. Class/year, residents, counts and charge are
optional for this policy. Incomplete/unavailable status never rejects a valid
partial save.

Property owns purpose/year edits. Existing `floor_area` (m², registry/override
meaning) and `heating_method` are systems-owned cross-section dependencies.
`floor_area` is not any of the four new heated quantities, net/gross area or
volume; heating method is not energy source. F004.3/F107 must record this exact
binding before calling B2/B6 resolved, or reconcile any changed heating-type
meaning explicitly. All four new heated quantities remain F004.3-owned. Frontend
owns section registration/progress and the saveability-versus-completion interface.

## Explicit version protocol

`personal_schema_version=1|2` selects owner list/detail/PUT/PATCH/DELETE and shared
detail. Default is 1; duplicate/malformed/unknown query keys fail 422. V2 writes
require explicit integer schema_version=2 agreeing with selector. Share management
is version independent and accepts no query parameters. Canonical/base stays v1.
V2 responses include document_schema_version=1|2. GET never upgrades stored rows.
V1 reads of v2 rows are closed projections; collection never loads personal JSON.

| Operation | Stored v1 | Stored v2 |
|---|---|---|
| v1 GET/list/shared | Existing view | v1 projection |
| v2 GET/list/shared | v2 view, stored version 1 | full v2 view |
| v1 PUT/PATCH/DELETE | Existing semantics | 409 personal_document_version_conflict |
| v2 PUT | Create/explicit full replacement to 2 | Full replacement |
| v2 PATCH | Explicit non-lossy promotion to 2 | Sparse merge |
| v2 DELETE | Whole conditional deletion | Whole conditional deletion |
| Share rotate/revoke | Unchanged | Unchanged |

Empty v2 PATCH promotes once without changing UUID, creation time, values or
active digest. PUT omission resets answers; reset remains format 2. No downgrade.
Ownership and conditional preconditions precede version-conflict disclosure and
are rechecked under lock after source work. Existing ub1 UUID/revision ETags
apply across representations; they do not claim source freshness.

Old supported schema permits v1 operations; v2 writes/readiness report sanitized
personal_schema_upgrade_required until the additive state migration. Schema-only
migration does not make old deployed binaries safe for v2 rows. No live migration
or deployment belongs to this feature. Roll back binaries only before v2 writes,
or restore into a new compatible target; downgrade with v2 data is refused.

## HTTP examples and runtime limits

Typed full examples are in `personal-property-v2.examples.json`; closed schemas
are in `personal-property-v2.schema.json`. Every write uses the existing Bearer
access-token and conditional headers. Example sequence (opaque key wholly encoded):

```
PUT /energy-data/user/buildings/KEY?personal_schema_version=2
If-None-Match: *
{"schema_version":2,"scenario_inputs":{"apartment_studio_count":0,"apartment_two_room_count":null}}

PATCH /energy-data/user/buildings/KEY?personal_schema_version=2
If-Match: "ub1:SAVED_UUID:REVISION"
{"schema_version":2,"set":{"scenario_inputs":{"apartment_three_room_count":12}}}

PATCH /energy-data/user/buildings/KEY?personal_schema_version=2
If-Match: "ub1:SAVED_UUID:REVISION"
{"schema_version":2,"unset":["scenario_inputs.apartment_studio_count"]}
```

Initial success is 201, subsequent success 200 with new ETag. Empty v2 PATCH
promotes existing v1. GET/list/shared default to v1 projections unless explicitly
selected. Share management uses the existing version-independent closed requests;
share reads accept the selector, and existing active tokens survive promotion.
V2 share allowlists include only delivered property values, provenance and current
input status, excluding owner identity, saved UUID, address_key and token digest.

Older writes on v2 return 409 personal_document_version_conflict after valid
ownership and preconditions. Missing/stale conditional headers retain 428/412;
PUT with If-Match against an absent owned record remains 412 (including a foreign
owner's inaccessible record). Invalid field type returns sanitized 422, e.g.
`{"detail":[{"loc":["body","set","scenario_inputs","apartment_studio_count"],"type":"int_type","msg":"Input should be a valid integer"}]}`.
No values, validation context or SQL are echoed. Invalid selector/body mismatch is
422 before mutation. Unsupported future stored formats fail closed.

This is source/local disposable delivery only. No live schema upgrade or serving-code load was verified here. No live migration, service load/restart,
deployment, frontend adaptation or UI acceptance is established by these files.
