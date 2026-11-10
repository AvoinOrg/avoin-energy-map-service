# Personal electricity and water — closed format 5

Version 5 succeeds reviewed renovations v4. Select `personal_schema_version=5`
on the existing `/energy-data/user/buildings` collection and detail routes and
`/energy-data/shared-buildings/{token}`. PUT/PATCH require integer
`schema_version:5`; the default remains v1. See the maintained
`personal-utilities-v5.schema.json` and `personal-utilities-v5.examples.json`.
Earlier schemas/examples, catalogs and migrations remain frozen.

These are delegated MVP meanings (November 10), not quantities specified by the
Sähkövesi Figma frames: those repeat property controls. This is self-report, not
certification of physical coverage or measurement accuracy. Each utility stores
one latest annual observation for the whole selected building, not a time series.

Let U = `scenario_inputs.utilities`.

| Path below U | Exact meaning / unit | Validation and optionality |
| --- | --- | --- |
| `electricity.grid_import_kwh` | Measured grid import for the whole building over one complete January–December year, kWh total. All uses supplied by those imports, including heating/cooling/hot-water equipment where metered there. Excludes directly consumed on-site generation and exports. Not gross demand, net balance, a meter index, power or HVAC useful heat. | Optional finite nonnegative JSON number; fractions and explicit zero retained. Null unknown; omission unanswered. No amount/year dependency. |
| `electricity.calendar_year` | The complete calendar year of that electricity observation. | Optional strict integer 1–9999; newly submitted known years must be less than the current UTC year. Null unknown; omission unanswered; year-only drafts valid. No current/last-year default. |
| `water.supplied_volume_m3` | Measured total water supplied for the whole building's consumption over one complete calendar year, m³ counted once before hot/cold distribution. Hot-water volume is already included. Excludes recirculation, wastewater and water-heating energy. | Same nonnegative number/null/omission rules. No resident, electricity or HVAC dependency. |
| `water.calendar_year` | The complete calendar year of that water observation, independent of electricity. | Same year rules. Different utility years are allowed. |
| U, `electricity`, `water` | Closed optional grouping objects, each only with the listed children. | Object, null or omitted; `{}` is valid. Null ancestors mark descendant values unknown. No section-completion flag or required pair. |

Number transport is the existing finite binary64 transport: reject strings,
booleans, negative/nonfinite/overflow and precision-losing integers. There is no
business maximum, rounding, clipping, conversion or lexical precision guarantee.
Year float/string/bool/zero/current/future inputs reject. Stored reads and
unrelated PATCH use structural validation, so advancing or changed submission
clocks do not invalidate old drafts. A known amount without a year is an annual
observation of unidentified year; it does not establish a dated/comparable report
baseline. No inference from timestamps, certificate dates or the other utility.

If coverage is only a dwelling, incomplete, shared across buildings or otherwise
unknown, leave the quantity unknown/unanswered. Do not multiply by residents,
allocate bills, extrapolate partial years or sum guessed meters. No prefill or
fallback from source/certificate/model/reference estimates. No writable scope,
units, meter, billing, period, hot-water split or provenance members are accepted.
No calculations, savings or scientific defaults follow from these inputs.

## Conditional section writes

Initial PUT uses `If-None-Match: *`; every subsequent PUT/PATCH/DELETE/reset uses
the current quoted strong `If-Match: "ub1:<saved_id>:<revision>"`. URL keys are
opaque and encoded as a whole. Login, selection and GET never save or promote.

PATCH omitting U preserves it. Supplying U replaces the entire U subtree:
omitted children are cleared. To edit one utility, retain the other's latest
sparse answer under the latest ETag. This preserves property, residents, HVAC and
all completed renovations without resubmitting them:

```json
{"schema_version":5,"set":{"scenario_inputs":{"utilities":{"electricity":{"grid_import_kwh":0,"calendar_year":2025},"water":{"supplied_volume_m3":456.25,"calendar_year":2024}}}}}
```

These values are illustrative, never defaults. Full section reset:

```json
{"schema_version":5,"unset":["scenario_inputs.utilities"]}
```

Explicit unknown is `{"schema_version":5,"set":{"scenario_inputs":{"utilities":null}}}`.
`{}` retains an empty answered group; reset removes the override. Descendant,
duplicate and conflicting unsets reject atomically. To clear only electricity,
replace U with the latest water object; to clear only water, retain electricity.
No descendant-unset API is added. PUT remains **whole-document replacement**:
omitting property/residents/HVAC/renovations/utilities clears those answers.
Use PATCH for section saves. Stale writes require reload and reconciliation.

## Reads, attribution and compatibility

Owner/shared v5 exposes sparse U at `scenario_inputs.utilities` and
`effective.scenario_inputs.utilities`. Computed `field_provenance` and
`source_availability` include U, both groups and all four leaves. Known values,
including zero, have user/available/is_override=true and exact personal source
path. Null is user_unknown; null ancestors supply their own exact source and
dependency. Missing is unavailable/missing/no_personal_default. Reset removes
user attribution. Utilities have no source fallback, input-status/completion
requirement, HVAC pin/default, canonical, aggregate, tile or calculation effect.

| Operation | Stored 1–4 | Stored 5, even empty/reset |
| --- | --- | --- |
| Default/v1 owner/shared | Existing scalar projection | Same scalar projection without systems/renovations/utilities |
| Owner/shared v2/v3/v4 | Existing unsupported-newer-row conflicts | Typed 409 `personal_document_version_conflict`; reload v5 |
| Owner/shared v5 | Supported projection, actual `document_schema_version`; no synthesized U or mutation | Full sparse supported inputs/provenance |
| List v5 | Summary only, actual stored version | Same; no personal inputs in summaries/cursors |
| List v2/v3/v4 | Existing rules | Page containing unsupported 5 conflicts atomically |
| v1–v4 writes/delete | Existing old-row policy | Reject after ownership/exact precondition checks; no downgrade |
| v5 PATCH | Explicit non-lossy promotion, preserving omitted siblings/shares | Atomic sparse edits |
| v5 PUT | Explicit whole replacement/promotion | Same replacement semantics |
| Share rotation/revoke | Existing version-independent lifecycle | Same lifecycle and row lock/revision rules |

Promotion preserves UUID/incarnation, created time and capabilities, and bumps
revision once. Stale 412 precedes version information at both preflight and
under-lock checks. Unknown stored versions fail closed. Explicit bearer shares
include supported personal answers under the existing capability policy; DTOs
strip owner IDs, saved UUID, digest and internal identifiers. No public list,
layer or new sharing UI. Every private response is no-store. Validation errors
have only loc/type/msg: PUT `body.scenario_inputs.utilities...`, PATCH
`body.set.scenario_inputs.utilities...`; no rejected values/union internals.
Existing 400/401/403/404/409/412/413/422/428/503 privacy and size limits remain.

## Storage and rollout limits

Additive combined head `b825df0a431c` and isolated `es0005_utilities_v5` change
only the recognized-version constraint to 1–5. No row conversion, table/column,
source relation or grant change. Readiness requires the exact validated check;
healthy old constraints yield upgrade-required 503 on requested5. Downgrade locks
before checking and refuses every v5 row, including empty/reset documents.

`upgrade-personal-v5` is explicit and disposable-only. Older commands remain
pinned to es0002/es0003/es0004. Completed-target apply is verification-only.
Real historical1–4/mixed1–5 archives restore first to their exact profile on a
NEW guarded target, audit rows/grants, then receive an additive metadata upgrade
to5. Row formats stay unchanged. Writer remains NOLOGIN during recovery audits;
committed retries must not replay pg_restore. Old binaries are unsafe rollback
writers after v5 appears; never strip U or stamp old metadata to bypass refusal.

This contract is not an activated private origin, live migration, real login,
frontend journey or report acceptance. Keep normal6900/6903, deployed F007 and
live private storage unchanged. F107.7 owns the UI and labels/layout; F107.8
consumes reviewed renovations; F107.9 may display supported inputs/provenance but
has no approved utility-to-heat, water-heating, emissions/savings or energy-class
calculation. Its actual authenticated private deployment and ordinary-user
save/reload/reopen/session-failure/owner/report acceptance remain required.
