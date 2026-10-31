# Personal HVAC v3

The opt-in closed v3 contract stores structured heating sources/shares, four
heated quantities, multiple cooling/ventilation choices and three self-reported
equipment answers. Coefficients, numeric defaults, attribution editing and reset
are **deferred**, as approved October31. They are not writable or required. This
is a backend candidate; reviewed delivery and runtime activation are separate.

## Versioned writes

Use `?personal_schema_version=3` and `schema_version:3` on the existing owner URL.
Default view remains1. Initial creation requires `If-None-Match:*`; subsequent
PUT/PATCH/DELETE requires one exact strong `If-Match` ETag from owner read. Exact
issuer+subject ownership, no-store responses, redaction and both wire/merged32KiB
UTF-8 limits remain. State is separate from canonical/source/certificate/model
rows; no recalculation or source mutation occurs.

```http
PUT /energy-data/user/buildings/{whole-encoded-key}?personal_schema_version=3
Authorization: Bearer <access token>
If-None-Match: *
Content-Type: application/json

{"schema_version":3,"scenario_inputs":{"systems":{"defaults_version":"hvac-inputs-2026-10-31.1","heating":{"sources":[{"source_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa","source_code":"ryhti:heating_energy_source:01","share_percent":65.5},{"source_id":"bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb","source_code":"ryhti:heating_energy_source:1101","share_percent":0}],"primary_source_id":"aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},"heated_quantities":{"total_area_m2":120.25,"total_volume_m3":null,"residential_area_m2":0},"cooling":{"kind":"none"},"ventilation":{"kind":"multiple","codes":["0","8"]},"equipment":{"geothermal_installed":false,"solar_panels_installed":true,"energy_storage_ready":null}}}}
```

These are synthetic personal answers, not source facts/defaults. A valid partial
save succeeds without any optional answer; `{"schema_version":3}` and a systems
object containing only its technical pin are valid. Saving does not assert that
the current required inputs are complete or that a frontend section was saved.

Let S mean `scenario_inputs.systems` below. Objects recursively reject unsupported
keys; there is no arbitrary JSON, coefficient or attribution slot.

| S member | Exact shape and meaning |
|---|---|
| defaults_version | Required technical identifier for non-null S: `hvac-inputs-2026-10-31.1` or retained `hvac-defaults-2026-10-30.1`. Neither supplies scientific numeric defaults. |
| heating | Null or `{sources:[...],primary_source_id?:UUID\|null}`. Array0…16 unique choices, optional primary pointer to a present item. |
| heating.sources item | `{source_id:UUID,source_code:string,share_percent?:number\|null}`. UUID is canonical lowercase8-4-4-4-12; source code is an exact namespaced discovery value. |
| heated_quantities | Null or optional independent number/null members `total_area_m2`, `total_volume_m3`, `residential_area_m2`, `residential_volume_m3`. |
| cooling | Null, `{kind:"none"}`, or `{kind:"selected",codes:[...]}` with1…14 unique discovery codes, retaining order. None cannot include codes. |
| ventilation | Null, `{kind:"none"}`, legacy `{kind:"selected",code:"0"}`, or `{kind:"multiple",codes:["0","8"]}` with2…9 unique codes. Code"0" is gravity, not none/unknown. |
| equipment | Null or optional strict boolean/null members `geothermal_installed`, `solar_panels_installed`, `energy_storage_ready`. No quantities or inferred installation. |

Shares mean percentages of **annual useful space-heating output excluding
domestic hot water**. No mandatory year,100% sum/tolerance, remainder,
normalization or automatic transfer between sources. Each value stands alone;
unknown/partial/zero and totals below/above100 remain saveable.

The four quantities mean internally measured heated spaces; residential excludes
common/non-residential spaces. They have exclusive systems ownership. Registry
floor/total/gross area, certificate heated-net area and registry volume are NOT
aliases/prefill. No conversion, residential<=total rule or externally verified
measurement gate is applied. Every new numeric leaf accepts finite binary64
JSON numbers including negatives and unusual magnitudes. No clipping, business
ranges, rounding or decimal-place validator; this is transport precision only.
Booleans, strings, nonfinite/overflow values and integers that would lose
precision in binary64 reject. JSON lexical formatting is not preserved.

## Stable collections, sparse values and reset

`set.scenario_inputs.systems` is one atomic PATCH leaf. Edit the latest complete
subtree and resubmit it with its strong ETag. PATCH omitting S preserves it;
within a replaced S, omitted children reset to absence. PUT removes all omitted
personal fields, including siblings: it is not a section-only write primitive.
Explicit null is user-unknown. Omitted is unanswered, zero/false are real answers,
and none is a positive no-system answer. There is no automatic unknown→none.

Source IDs identify document-local UI choices, not physical equipment inventory.
One entry per source code; order is retained and primary is a pointer. Reorder
or primary switching retains identities/shares. A replacement code requires a
NEW UUID; retained-ID code changes reject. Removal drops only submitted-removed
answers; clear/repoint primary in the same save. Re-add a new UUID with fresh
answers; deleted shares do not resurrect. No tombstone/history service exists.

To clear one answer, omit it from the complete replacement S while retaining
other siblings. To reset S entirely:

```json
{"schema_version":3,"unset":["scenario_inputs.systems"]}
```

Or explicitly mark all systems unknown:

```json
{"schema_version":3,"set":{"scenario_inputs":{"systems":null}}}
```

Descendant unsets, duplicate IDs/codes/choices, dangling primary, extra variant
members and conflicting set/unset reject atomically. Reset is an ordinary explicit
conditional save. GET, login, discovery and selection never write/pin/promote.
Two same-ETag collection writes yield one success and one stale412; reload,
reconcile both edits and explicitly save the whole subtree again.

Legacy `overrides.ventilation_type_id`/`ventilation_description`, `floor_area`,
`heating_method` and `heating_energy_source` remain independent; structured
sources never guess a scalar mapping. Property/v1 siblings survive PATCH.

## Reads, provenance and status

Owner/shared view3 exposes sparse S and identical personal values under
`effective.scenario_inputs.systems`. Fixed `field_provenance` and
`source_availability` keys cover S, heating/sources/primary, quantities and each
quantity leaf, cooling, ventilation, equipment and each equipment leaf.

Per-source provenance is `systems_source_provenance:[{source_id,source_code,
share_percent}]`; both leaf entries are closed FieldProvenanceV1. It follows
presentation order but source_id is the association key. Its `source` path is
S.heating.sources, scoped by source_id, never an unstable array index. A known
personal share remains available even if its source choice is historical or the
catalog is unavailable. All numeric/equipment leaves have independent provenance.

Known personal data: origin=user/status=available/is_override=true. Null:
user/user_unknown. Absent: unavailable/missing/no_personal_default. Explicit null
ancestors propagate user_unknown with source/dependencies pointing to the
ancestor. There is no scientific/canonical fallback for any new answer.

`systems_defaults` preserves defaults_version and technical status
not_requested|pinned_no_scientific_defaults|pin_unavailable. The last denotes an
unavailable bundled choices/manifest dependency, never missing scientific data.
It adds capability_version `hvac-inputs-2026-10-31.1` and closed `coefficients`:
status=deferred/reason=owner_deferred, numeric_defaults=null and editable,
attribution_editable, reset_available, required_for_save/completion all false.
Frontend owns the readable disabled gray TBD section and disclaimer; unsupported
coefficient writes reject422 without mutation. Missing defaults never become0.

`input_status.property` retains `property-required-inputs-2026-10-29`: purpose,
construction_year, registry floor_area, heating_method. Systems status uses the
same policy but only its two existing bindings floor_area/heating_method.
Missing or explicit-unknown required input gives incomplete, unusable required
source evidence gives unavailable, otherwise complete. Valid floor_area0 counts
as present under existing rules. All new HVAC/coefficients are optional and
never appear in required-path lists. Status is current-input evidence, not
section-save history, frontend registration/progress or draft validation.

## Supported discovery and outages

`GET /energy-data/building-hvac-choices` accepts no query parameters, returns
no-store closed **envelope2**, capability version and deterministic sha256 content
version. Maintained `hvac-catalog-v2.schema.json` and
`src/data/hvac-catalog-v2.snapshot.json` describe:

- 16 exact `ryhti:heating_energy_source:<code>` choices from the actual Ryhti
[energy-source catalog](https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/lammitysenergianlahde/),
including district heat, electricity, fuels and heat-pump classifications.
FI/SV/EN labels and VALID status are upstream; this is distinct from lammitystapa.
- 14 `energy:cooling:*` product codes using exact FI type labels from
[RAVA3Pro LVI terminology1.0](https://kirahub.org/wp-content/uploads/2023/11/SANASTO-LVI-JARJESTELMA-Versio-1.0.pdf).
They cover ventilation, radiant panel, beam, fan coil, district, transfer,
water chiller, heat pump, liquid cooler, condensation, air cooler, floor,
ice rink and other. Codes are product-defined, not asserted upstream codes;
SV/EN labels and upstream lifecycle/version are unavailable. Production and
distribution types may combine; no inferred scientific relation or exclusion.
- 9 certificate ventilation codes0…8 with FI/SV labels; no official EN labels.
Mixed4/5 remain mixed. Code3 retains certificate-purpose-class1 caveat without
claiming registry-purpose equivalence. [Source](https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/ilmanvaihtotyyppi).
- 3 equipment controls with exact FI design labels, self-reported installation/
readiness meaning, absent SV/EN labels. This is not device inventory/capacity.
- Explicit deferred coefficients; no writable attribution or placeholder values.

The pinned supported inventory was checked2026-10-31, with source links,
attribution, known lifecycle/label limits, freshness and product scope. It does
not claim live freshness or exhaustive national coverage. Product-defined code
support is an engineering decision from evidenced types, not scientific approval.
The prior ventilation envelope1/snapshot remains unchanged as historical technical
pin evidence. No immutable pin is repurposed or silently updated on reads.

New/changed choices validate against the same selectable set as discovery.
Full PUT revalidates all resubmitted choices. PATCH retaining the same identity/
code allows numerical changes/reorder/historical choices without current lookup.
Missing/corrupt required snapshot returns sanitized503/no mutation, never empty
successful discovery. Numeric/equipment-only/empty drafts, clearing/reset,
unrelated PATCH, GET, delete and revoke remain possible. Upstream outages leave
intact bundled choices usable without HTTP; analytical-source loss never erases
personal state. Historical/deprecated codes remain readable as unsupported.

## Errors and compatibility

Errors expose only loc/type/msg. PUT prefix is body.S; PATCH body.set.S. Source
member locations append heating.sources.<submitted index>.<member>. Map errors
using that frozen submitted snapshot's source_id, not a later reordered draft.
Collection errors use duplicate_source_id/duplicate_source_code/duplicate_choice,
dangling_primary_source or source_identity_reused. Unsupported current choices
use unsupported_hvac_choice. For example422 without mutation:

```json
{"detail":[{"loc":["body","set","scenario_inputs","systems","heating","sources",1,"source_code"],"type":"unsupported_hvac_choice","msg":"Choose a supported HVAC code"}]}
```

Stale412, version409, invalid422, oversized413, missing precondition428 and
required catalog/state503 retain existing semantics. Union tags/input/provider
errors are sanitized; no submitted data is echoed in diagnostic messages.

| Operation | Stored1/2 | Stored3 |
|---|---|---|
| v1 detail/shared/list | Existing view | Safe scalar projection/summary |
| v2 detail/shared/list page | Closed v2 |409 personal_document_version_conflict; request3 |
| v3 detail/shared/list | True document_schema_version | Full v3 view |
| v1/v2 writes/delete | Existing protection |409 after ownership/preconditions |
| v3 writes | Explicit non-lossy promotion | Conditional edit/replacement |
| share rotate/revoke | Version independent | Version independent |

V2 does not widen its enum/drop rows. Only pages containing3 conflict; all lists
are summary-only. Promotion preserves saved UUID/incarnation, creation time and
sharing, bumps revision once and cannot downgrade. Stale412 precedes version
information; unknown formats fail closed. V1/v2 maintained artifacts stay frozen.

Both additive migration lineages change only private recognized-format checks;
JSONB tables/grants are unchanged. Exact v1/v2/v3 profiles and NEW-target recovery
preserve full fields/fingerprints. Writer remains disabled across interrupted
upgrade until exact verification. Downgrade refuses v3. Deploy compatible readers
before activation; older binaries are unsafe rollback after v3 writes. This
feature tests disposable/local targets only and never activates normal6900/6903.

## Copy surface and executable artifacts

F107.6.2 may copy accepted PERSONAL sources/shares, quantities, cooling,
ventilation and equipment with compatible pin, retaining absence/null/zero/false.
Regenerate destination source UUIDs/remap primary. Revalidate destination choices
on explicit conditional save. Separate legacy scalar personal floor_area,
heating_method/heating_energy_source and ventilation code/note may be copied
within frontend systems ownership. Never materialize effective/reference/source
facts. Exclude deferred coefficients, canonical building/source/certificate/model
identity, owner, saved UUID, revision/ETag/incarnation, timestamps, share token/
digest/summary and accepted-save/completion history. No copy API or implicit save.

`personal-hvac-v3.schema.json`, `hvac-catalog-v2.schema.json` and
`personal-hvac-v3.examples.json` contain16 typed examples: full inputs with complete
existing policy, additional-only/empty/numeric partial drafts, unknown/none,
atomic change/reset/promotion and owner/shared/list/discovery reads. Generate with
`python -m src.api.export_building_contract --hvac-only` in a temporary tree.
No active coefficient example exists by deliberate deferral. Backend completion
still requires independent review; frontend UI/controller/disclaimers, actual
provider/ordinary-user/deployed/report/release acceptance retain their own gates.
