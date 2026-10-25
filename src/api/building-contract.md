# Building contract v1

Status: locally implemented and fixture-verified; the field matrix was fixed before product code. This child does not complete the parent or frontend.

## V1 field matrix — completed planning input

The names below are the v1 API names. `details` denotes a common displayed-value shape used in `base.details` and `effective.details`. PUT `overrides` accepts only the editable rows below, using the unprefixed leaf name. Official fields live separately under `base.certificate`/`base.model`, even when a detail displays the same source value.

Null conventions: all editable scalar leaves are optional and nullable. Omitted means no personal override; explicit null means personal unknown and blocks source fallback; zero is a supplied value where permitted. All DTOs forbid extra keys recursively. Source absence is null plus a field/source availability reason, not a fabricated zero. Source values outside a user-input bound are handled as source data with validity metadata; do not reject an entire public read merely because imported data is malformed.

Text policy: Unicode plain text; no HTML rendering or markup interpretation. Reject NUL and control characters except newline/tab in the ventilation note; reject blank/whitespace-only supplied text (use null to clear/mark unknown). Preserve supplied meaningful text rather than silently rewriting it. Consumers must escape it. Bounds below are Unicode character counts; enforce a separate 32 KiB UTF-8 personal payload cap before later writes, including chunked bodies in the owning HTTP feature. A pure validation/serialization helper in this child enforces the same cap for constructed payloads. Public source text uses a 16,384-character per-field output bound; oversize source text becomes unavailable with `source_value_too_large`, never silently truncated official prose.

For user numeric quantities, accept JSON integers/reals as numbers but explicitly exclude booleans and numeric strings; require finite values. Counts/years accept integer tokens only. Practical quantity caps are application abuse/accident limits, not claims about physical laws. Do not enforce invented inequalities between differently defined floor/total/gross/heated-net areas.

### Editable building details and saved-name contract

| API leaf / envelope field | Canonical source property or base derivation | JSON type / unit | Null/zero behavior | Validation / cross-field rules | Classification |
| --- | --- | --- | --- | --- | --- |
| `display_name` | No canonical building-name column; future saved-record label, not certificate name | string / none | PUT omission and null both clear name; PATCH omission preserves, null clears | 1–120 chars, plain text; top-level only; never a source identifier | Personal saved metadata; no persistence here |
| `address_fin` | Selected `main.address_fin`; if main absent, selected `addresses.address_fin` | string / none | Null marks address unknown; missing postal parts do not hide a street | 1–300 chars | Editable personal address; source address retained |
| `postal_code` | Same selected address's `postal_code` | string / none | Null unknown; `"00100"` stays text | Exactly 5 ASCII digits; no numeric coercion or padding | Editable personal postal information |
| `postal_office_fin` | Same selected address's `postal_office_fin` | string / none | Null unknown | 1–150 chars | Editable personal postal information |
| `construction_year` | Calendar year of Ryhti `completion_date`; never certificate `completion_year` | integer / year | Null unknown; zero invalid | 1 through current UTC year + 5, clock injected in tests; full dates/strings/bools rejected | Editable personal year; raw completion date read-only |
| `main_purpose` | `main.main_purpose`, otherwise `buildings.main_purpose` | string / code | Null unknown; leading zeroes retained | Exact membership in read-only `code_maps` type `main_purpose`, length 1–16; no trimming/coercion or restriction to model-covered `05`/`06` | Editable Ryhti-code personal choice |
| `heating_method` | `main.heating_method`, otherwise `buildings.heating_method` | string / code | Null unknown | Exact `code_maps` type `heating_method`, length 1–16 | Editable personal choice; no assertion of certified installation |
| `heating_energy_source` | `main.heating_energy_source`, otherwise `buildings.heating_energy_source` | string / code | Null unknown | Exact `code_maps` type `heating_energy_source`, length 1–16 | Editable personal choice |
| `number_of_storeys` | `main.number_of_storeys`, otherwise `buildings.number_of_storeys` | integer / storeys | Zero retained as supplied count, with no positive-storey model eligibility; null unknown | 0–1,000; no float/string/bool | Editable personal count |
| `apartment_count` | `buildings.apartment_count` by exact key; not currently projected by main | integer / apartments | Zero valid; null unknown | 0–1,000,000 | Editable personal count; source availability independent of main |
| `floor_area` | `main.floor_area`, otherwise `buildings.floor_area` | number / m² | Zero retained, unusable as positive calculation denominator; null unknown | 0–10,000,000 | Editable personal registry-area estimate |
| `total_area` | `main.total_area`, otherwise `buildings.total_area` | number / m² | Same zero/null distinction | 0–10,000,000 | Editable personal estimate, distinct area definition |
| `gross_floor_area` | `main.gross_floor_area`, otherwise `buildings.gross_floor_area` | number / m² | Same | 0–10,000,000 | Editable personal estimate, distinct area definition |
| `heated_net_area` | Selected certificate `heated_net_area` / matched `main.energy_certificate_heated_net_area` | number / m² | Same; personal area never changes the denominator of official certificate consumption | 0–10,000,000 | Editable personal estimate only in details; certificate area immutable |
| `volume` | `main.volume`, otherwise `buildings.volume` | number / m³ | Zero retained; null unknown | 0–1,000,000,000 | Editable personal estimate |
| `ventilation_type_id` | Selected certificate `ventilation_type_id` / corresponding main field | string / code | `"0"` is valid gravity; null personally unknown | Exactly one of `"0"`…`"8"` from pinned `VENTILATION_TYPES`; numeric 0 rejected | Personal system selection; official code/labels remain separate |
| `ventilation_description` | First nonblank selected-certificate FI then SV description, with actual language in provenance | string / none | Null blocks display fallback; no invented translation | 1–4,000 chars; plain text, newline/tab permitted | Personal note; original FI/SV certificate prose immutable |

Code-catalog interface: pass a read-only `CodeCatalog` into semantic validation, separate from structural Pydantic validation. Fetch the three type sets/labels in one bounded query; fixture tests inject them. Unknown supplied code yields field-level 422. A missing/unavailable required catalog is infrastructure unavailability, not proof that every valid code is invalid: expose a typed dependency failure for later routes to map to 503. Existing saved valid codes must remain displayable as unknown/deprecated after catalog refresh; do not erase or rewrite them on read. Revalidate new set values; document a later explicit edit may need replacing a now-unsupported value, while reset/deletion remains possible.

### Scenario inputs and actual calculation dependencies

| API field or inventory item | Actual source/input | Type / unit | Null, limits and combinations | Editable status / treatment |
| --- | --- | --- | --- | --- |
| `scenario_inputs.resident_count` | Current Water control's committed user count; default is frontend floor-area heuristic | integer / residents | Optional/null; supplied count 1–10,000, matching `CURRENT_REFERENCE_DATA`; zero rejected, no bool/float/string | The v1 adjustable scenario input. Omission/unset restores existing default behavior; null means unknown and must not silently reapply heuristic. No resident-register claim. |
| Calculation `floorAreaSquareMeters` | Effective `details.floor_area` | number / m² | Positive needed for current annual/reference computations; zero/null remains representable but calculation unavailable | Reuse the detail override; no duplicate scenario-area field |
| Calculation `mainPurpose` | Effective `details.main_purpose` | string / code | Existing cost/reference building classes are only `05`/`06`; other valid building codes remain savable/displayable | Reuse the detail override; unsupported calculation is not invalid building data |
| Calculation `scenarioPrefix` | Effective heating source/method mapping: `01→distr`, `02→oil`, `07→wood`, `09→gshp`, `04+01→elecb`, `04+03→delec` | enum / none | No unambiguous current Ryhti mapping to `awhp`; absent/unsupported combination remains unavailable | Derived input; no independent editable prefix that can contradict heating details |
| Calculation default electricity/heating intensity | Existing `<prefix>_default_elec` / `_heat` in main, when actually present | number / kWh/m²/year | Finite nonnegative including 0; missing column/blank/invalid value distinguished; no approved generation job here | Read-only reference outputs, not editable assumptions merely because functions accept them |
| `base.model.consumption_scenarios[prefix][measure][quantity]` | Only the closed main-column family `<prefix>_<measure>_<quantity>` | number / kWh/m²/year | Prefixes `awhp,delec,distr,elecb,gshp,oil,wood`; measures `default,aahp,solar,windows`; quantities `total,heat,elec`. Closed schema, not arbitrary column names. Known exclusions: other purposes; apartment `wood/elecb`; apartment AAHP; AAHP with `awhp/gshp`. Preserve missing/unsupported status and any actually published finite zero. | Allowlisted reference outputs. Do not create absent columns or calculate values. Generate field definitions from these fixed enums, not untrusted identifiers. |
| Water default count and annual Water | Existing frontend positive floor-area ÷ 41.6, half-up/clamp 1–10,000; count × 43.8 m³/year | integer; number / m³/year | For existing residential scope; explicit user null is unknown. Missing source area does not prevent a valid manual count. Nonresidential/unknown applicability is explicit. | Existing frontend derivation only; document dependency/provenance, do not port engine here |
| Annual energy, cost and CO₂; renovation annual/intensity/savings | Existing frontend model-column/area/factor computations | kWh/year, kWh/m²/year, EUR/year, kg CO₂/year, ratio | Keep missing operands, nonpositive area/baseline, unsupported reference combinations and pellet price gap explicit. Certificate/registry denominators never silently interchanged. | Derived outputs remain frontend responsibilities. No writable `annual_cost`, `co2`, savings or total-energy fields |
| Reference factors/version | `CURRENT_REFERENCE_DATA.version = F075-resident-water-2026-08-02`; fixed cost/carrier/emission/Water factors | metadata / existing declared units | Pellet fossil-emissions factor 0 is valid; apartment pellet price not supplied | Read-only reference context, not new server configuration or user inputs |
| Map class/heating/ventilation filters, decade filters, primary/submetric selection, panel state | `EnergyHomePage.tsx` and building presentation types | presentation state | No physical building meaning | Excluded from saved scenario contract |

Scenario `resident_count` is structurally valid without current source availability. Applicability follows the effective purpose and is metadata, not a reason to lose old overrides after a refresh. This child supplies the typed value/default-reset semantics and provenance; it does not add water calculations. Invalid combinations rejected here include set/unset conflicts, container/leaf path conflicts, wrong schema versions, illegal namespaces and read-only field assignment. Do not invent heating-code compatibility or area-order rules absent from the current product contract.

### Read-only identity, official/model facts and provenance

| API path / field family | Source property | Type / unit / null behavior | Rules and classification |
| --- | --- | --- | --- |
| `building_key` | Exact main/buildings key | opaque string; required | 1–256 UTF-8 bytes, no control characters, no normalization/coercion; not UUID-typed. It is never accepted inside override payloads. |
| `base.identity.permanent_building_identifier` | Main plus exact Building identity check | string/null | Preserve opaque identifier, leading zeroes and identity; never substitute property/address/tile ID |
| `base.identity.property_identifier` | `main.property_identifier`, otherwise exact Building source | string/null | Valid product display is 14 ASCII digits and not all zeroes. Invalid supplied source gets explicit invalid status, not padding/numeric conversion. No tenure/ownership inference. |
| `base.identity.address_key` | Selected main/address row | string/null | Read-only source-selection identifier; not building identity |
| `base.details.completion_date` | Main/Building Ryhti completion date | ISO `YYYY-MM-DD`/null | Strict real calendar date; invalid source becomes unavailable with reason. Never fill with certificate completion year or invent Jan 1 from personal year. `construction_year` is separately derived/overridden. |
| `base.geometry` | Selected main polygon/point, native 3857; raw Building/Polygon source only on documented main-absent fallback | typed geometry object/null | Explicit SRID and source; no accepting geometry in overrides. Emit original selected geometry coordinates with SRID, plus a derived WGS84 summary location. Preserve storage, never update/repair/union geometries in requests. Invalid/oversized geometry is an availability gap, not a fabricated shape. |
| `base.details.ventilation_category` and effective counterpart | Pinned code/description policy in ventilation.py | `mechanical,gravity,other,no_info` | Derived read-only; type 0 gravity; 1/2/3/7/8 mechanical; 4/5/6 other; unknown code or description-only other; all absent no_info. Never infer from age/prose. Recompute effective category from effective type/description if personally changed; mark derived-from-user provenance. |
| `base.certificate.certificate_id`, `version`, `building_identifier` | Selected `energy_certificates` row | string/null; integer/null; string/null | Same exact identifier and newest-row order as ETL; preserve lexical certificate-ID tie-break, version distinct from schema/class scale |
| `base.certificate.signed_at`, `valid_until`, `inspection_date` | Selected certificate columns | calendar date/null for public date semantics | DB signed/expiry timestamps are timezone-naive: use calendar dates for displayed certificate facts, not fabricated UTC instants. Ordering uses the DB signed_at value unchanged. Expired remains available history. Invalid dates are explicit gaps. |
| `base.certificate.energy_class`, `previous_class`, `class_scale_year` | Selected/next-older exact-ID certificate class and version; matching main publication fields where source match unavailable | A–G/null; A–G/null; 2013/2018/null | Read-only certificate facts; previous means next older imported row, never next older nonblank value. Unknown classes/scales explicit; expose history count only when actually queried. |
| `base.certificate.heated_net_area` | Same selected certificate | number/null / m² | Positive for certificate intensity; zero/invalid area yields unavailable denominator. Personal `heated_net_area` cannot replace this field. |
| `base.certificate.ventilation_type_id`, labels FI/SV, descriptions FI/SV | Selected certificate type/description; pinned label lookup | strings/null | Official labels only for supported codes; unknown source code is retained with unsupported-code status; no invented English prose or source-language guarantee |
| `base.certificate.recommendations_fi/sv` | Selected certificate recommendation columns | strings/null | Allowlisted public certificate text; no new general recommendation; output text bounds and actual language metadata |
| `base.certificate.consumption` | Existing schema-1 consumption payload from same selected certificate, or re-normalized with pure consumption_payload | typed object/null / kWh/year | Standardized calculation, never metered; five standard carriers plus up to three other slots, complete/partial, annual quantities, total only when complete, own certificate area/IDs/dates. No raw source_row. Validate schema/basis/unit/ID/carrier uniqueness/totals; preserve zero. Reuse normalizer semantics, do not write augmentation. |
| `base.model.modeled_energy_class`, `modeled_energy_e_value`, `reason`, `area_source` | Main `modeled_energy_*` | A–G/null; finite nonnegative number/null; strings/null | Read-only estimates/reasons; unknown model version/freshness remains unknown. No recalculation from user overrides. |
| `base.model.energy_class`, `is_energy_class_modeled`, `energy_class_year` | Main effective class/origin/scale | A–G/null; boolean/null; integer/null | Preserve publication origin; missing origin is unknown, never assume official. Do not change these because a user changed purpose/year/area. |
| `source_observed_at`, `resolved_at`, `sources.*` timestamps | Exact source timestamp evidence as described below | UTC ISO timestamp/null, except certificate calendar facts | Read-only. No request-time masquerading as dataset freshness. Source timestamp strings must parse strictly. |
| `field_provenance`, `source_availability`, schema/version fields | Resolver/normalization/overlay | typed objects/enums | Read-only allowlists; no owner, email, token metadata, arbitrary SQL or raw DB row |

All source-backed detail leaves retain their source values under `base` even when overridden in `effective`. A registry value, an official certificate fact, a published model result and a user input are different provenance origins. Displaying a personal ventilation choice or area does not modify or relabel certificate facts.

### Unsupported fields and reasons

| Requested/product concept | V1 treatment and reason |
| --- | --- |
| Water-heating share | Unsupported: no approved split source/formula |
| General renovation/energy recommendations and class-improvement measures | Unsupported: no general dataset or measure-to-official-class contract. Preserve matched certificate recommendations and existing reference scenario outputs separately. |
| Planned works, plot tenure | Unsupported: no linked project/ownership/lease data; property ID is insufficient |
| Installed solar/other-energy equipment | No inference from solar scenario columns; fuller source dataset has not been obtained |
| Personal energy-class or annual-consumption estimates | Not accepted in v1: they are optional in the parent, are not the actual current adjustable control, and need distinct future user-estimate fields/units. Reject attempts to set `energy_class`, certificate consumption or model columns. |
| User prices, CO₂ factors, efficiencies, renovation dates, arbitrary source columns | Unsupported: not current adjustable inputs; accepting helper parameters as arbitrary editable fields would expand product scope |
| Canonical building name | No registry-name property in current projection. Summary `name` is null rather than a certificate name silently promoted to a building name; later `display_name` is separate. |
| Reliable main refresh/model-generation timestamp and exact scenario-generation failure | Unavailable in current schema/job lineage; report unknown. Do not add ETL work or invent a timestamp/diagnosis. |


## Implemented schema and public HTTP interface

Schema version is **1**. [building-contract-v1.schema.json](building-contract-v1.schema.json)
contains the closed DTO definitions, including the future request envelopes;
[buildings.py](buildings.py) registers only the public GET. The API's `/openapi.json`
contains a complete, schema-valid success example and the expected error bodies.

`GET /energy-data/buildings/000-fixture-opaque-key` is anonymous. Its response is a
`CanonicalBuildingV1`, with `building_key`, `base_status: "available"`, `identity`,
`details`, selected native `geometry`, WGS84 `location`, `selection`, `certificate`,
`published_certificate`, `model`, `code_labels`, `sources`, `field_provenance`,
`source_availability`, nullable `source_observed_at`, `timestamp_status: "unknown"`
and a separate UTC `resolved_at`. There are no owner or personal-state fields.

A compact **excerpt** of a fixture response (the full OpenAPI example includes
all response properties):

```json
{
  "schema_version": 1,
  "building_key": "000-fixture-opaque-key",
  "base_status": "available",
  "identity": {"permanent_building_identifier": null, "property_identifier": null, "address_key": null},
  "details": {"address_fin": "Example street 1", "postal_code": "00100", "floor_area": 0},
  "source_observed_at": null,
  "timestamp_status": "unknown",
  "resolved_at": "2026-10-23T00:00:00Z"
}
```

The successful response and expected data errors have `Cache-Control: no-cache`.
Exact error JSON:

| Status | Body |
| --- | --- |
| 404 | `{"detail":{"code":"building_not_found","message":"Building not found"}}` |
| 409 | `{"detail":{"code":"building_ambiguous","message":"Building identity is ambiguous"}}` |
| 503 | `{"detail":{"code":"building_source_unavailable","message":"Building source is temporarily unavailable"}}` |

Invalid keys return sanitized field-level 422 entries with only `loc`, `type` and
`msg`. Keys are exact UTF-8 strings of 1–256 bytes without controls. The route uses
a path converter to preserve slash-containing opaque keys, including URL-encoded
slashes; intermediaries must preserve that encoding. There is no trimming,
case folding, UUID conversion, identifier alias, nearest-building lookup, `env`
selector or historical-snapshot fallback. Query parameters do not select a
source database. Requests never perform a personal-state lookup or write.

### Clarification of the read-only field matrix

`certificate` is built entirely from the current selected imported row.
`published_certificate` separately preserves main's certificate-derived snapshot
(class/history/scale, calendar expiry, area, ventilation and recommendations),
without assigning it an unverified current certificate ID. Both objects have
per-field availability. This additive object prevents a stale publication from
being mislabeled with a newer certificate's identity. It remains available if
certificate enrichment is unavailable. `certificate.publication_comparison`
compares main's available class, area and ventilation-code fields; it is a
partial comparison, **not** proof that every main field came from that certificate.
Main's consumption JSON is never passed through; selected-row consumption is
re-normalized by the existing pure normalizer. No raw certificate row is fetched:
the SQL extracts only the five carrier fields and three optional carrier slots.

`code_labels` contains FI/SV/EN labels for the three current registry choices,
loaded with their code sets in one query (10,000-row safety ceiling). An unknown
source code remains visible with `unsupported` status. Missing label/catalog
information degrades enrichment without changing source identity. Ventilation
labels use the pinned `VENTILATION_TYPES`, not this dynamic catalog.

## Resolver, snapshot and bounded summary interfaces

Exports in `src/building_resolver.py`:

```python
resolve_building(session, building_key: str) -> Resolution[CanonicalBuildingV1]
resolve_building_summaries(session, keys: Sequence[str]) -> dict[str, SummaryResolutionV1]
load_code_catalog(session) -> CodeCatalog
```

`Resolution.status` is `available`, `missing` or `ambiguous`; `value` is populated
only for `available`. `BuildingSourceUnavailable` is an independent sanitized
dependency exception. Missing/unreadable required main or Building infrastructure
is 503, even when a key could otherwise look missing. Missing optional relations
or columns are explicit source/field gaps. A connection, permission, timeout or
concurrent-DDL SQL failure is sanitized as 503 and never disguised as missing.
A missing or empty required code catalog raises `CatalogUnavailable` for new-code
validation; public detail enrichment can remain usable without it.

Supply a **fresh clean analytical SQLAlchemy Session**, or a clean existing
read-only repeatable-read/serializable transaction. The resolver sets repeatable
read and read only before querying, with transaction-local 10-second statement
and 2-second lock timeouts. Close/rollback afterwards; it never flushes or commits.
Later state writes must use their separate narrow writer and must not reuse an
active write transaction as the canonical-source session. Public reads retain
the deployment's existing read-only protection.

Rules shared by details and summaries:

1. Main or Building anchors exact-key identity; orphan addresses/polygons do not.
   Main-only and Building-only records are supported. Conflicting permanent IDs,
   IDs associated with distinct canonical keys, and conflicting main invariant
   facts are ambiguous. Address/geometry multiplicity is ordinary data.
2. Main owns its projected scalar values, including null. Building supplies
   apartment count and its own source metadata; it supplies other registry facts
   only when that key has no main row. Non-identity main/staging differences are
   source-version differences. No opportunistic null filling occurs.
3. Invariant comparison covers the closed registry, certificate-publication,
   class/model and scenario-column families. SQL normalizes blank text to null,
   trims non-identifier scalar text and uppercases class codes. Fixed-typed main
   numerics/dates use PostgreSQL's textual representation; a null/populated
   disagreement is a conflict. Opaque identifiers retain exact meaningful text.
4. Select a usable street before postal-only before missing; then address key
   and street/postal fields under explicit `COLLATE "C"`, nulls last. Keep every
   address part from the same row. Main's selected address is never filled from
   a different staging row. Oversized street text is not a usable street.
5. Select usable polygon before usable point, ordered by full native EWKB bytes.
   Byte-identical source variants collapse before expensive geometry validation
   and in multiplicity counts. Source name and exact
   source modification string break equivalent-source ties. Report representative
   and variant counts; no union, geometry repair or stored-coordinate mutation.
   Require expected SRID (main 3857, fallback sources 4326), nonempty valid point/
   polygon/multipolygon, finite earth-range bounds and at most 1 MiB serialized
   GeoJSON. Reject unusable geometry independently of building facts. Native main
   bounds are ±20037508.35 easting and ±20048966.11 northing; fallback bounds are
   WGS84 longitude/latitude. Location is WGS84 `ST_PointOnSurface` of the chosen
   polygon, otherwise the chosen point. Summaries return location, never polygons.
6. Certificates use the exact permanent identifier and order by
   `signed_at DESC NULLS LAST, certificate_id COLLATE "C" DESC`, including expired
   and blank newest certificates. Previous class means the next older imported
   row, including blank; no older-nonblank fill. History count is actually queried.
   Certificate display dates are calendar dates, with no fabricated UTC midnight.
7. `resolved_at` is this resolution's UTC read time. Current main has no supported
   ingestion/refresh lineage: `source_observed_at` remains null for details and
   summaries. Generic Building/Address `updated_at` is never used as freshness.
   `sources.ryhti_*` UTC modification timestamps refer only to their exact source
   rows and distinguish known/missing/invalid. Naive strings are treated as UTC
   only because those source columns explicitly declare UTC. Certificate's
   maintained aware `updated_at` is separately `imported_at`. Main geometry has
   no source polygon key, so its raw polygon modification is `not_projected`,
   not an invented absence or timestamp.

The batch resolver accepts 0–100 **distinct** valid keys, collapses duplicates and
returns an outcome per key. Empty input queries nothing. It uses at most six
source SELECTs plus one metadata SELECT budget (observed six total, including
metadata, for the 100-key fixture): filtered main scalar selection, Building,
global identity-conflict check, address selection and geometry selection.
There is no certificate/source-row load or per-key query. The summary main query
returns only identity/address, not bulk model/certificate facts, after checking
invariants in SQL. Predicates precede geometry processing in a materialized CTE.

Fixture EXPLAIN evidence is in
`.codex-orch/features/F001.1-building-contract/query-evidence.json`. Main has no
building-key index in the existing merge, so filtered scans remain. The fixed
query count removes N+1 access; tiny fixtures do not establish national latency.
No source index/ETL change or unbounded cache is introduced. Production-scale
performance/index work requires a separately scoped follow-up.

`BuildingSummaryV1` has exactly the public summary matrix fields.
`LastKnownBuildingSummaryV1` adds only aware UTC `captured_at`. Later persistence
captures it during explicit save/verified refresh. Use `model_validate_json` to
load serialized date/timestamp DTOs under strict validation. No snapshots are
persisted here.

## Typed replacement, PATCH and effective projection

Exports in `src/building_contract.py`:

```python
replace_personal(value: PersonalBuildingPutV1, catalog: CodeCatalog | None) -> PersonalBuildingPutV1
apply_personal_patch(existing: PersonalBuildingPutV1, patch: PersonalBuildingPatchV1,
                     catalog: CodeCatalog | None) -> PersonalBuildingPutV1
personal_document(value: PersonalBuildingPutV1) -> dict
project_building(base: CanonicalBuildingV1 | None, overrides: BuildingOverridesV1,
                 scenario_inputs: ScenarioInputsV1, *,
                 last_known_summary: LastKnownBuildingSummaryV1 | None = None,
                 base_unavailable_reason="missing", building_key: str | None = None
                 ) -> PersonalBuildingProjectionV1
```

These are DTOs/pure functions for later owner/share features. No PUT/PATCH owner
route, optimistic revision, request-body middleware or private state exists yet.
The owning HTTP feature must enforce the same 32 KiB limit **before parsing**, also
for chunked bodies, as well as preconditions and ownership. The helpers enforce
size for constructed/serialized documents. Use `personal_document()` to persist
sparse leaves; default `model_dump()` materializes null defaults, while
`exclude_none=True` loses explicit personal unknowns. The projection's serializers
preserve sparse override/scenario leaves automatically.

Future PUT full replacement:

```json
{"schema_version":1,"display_name":"Our building","overrides":{"main_purpose":"05","floor_area":0,"heated_net_area":null,"ventilation_type_id":"0"},"scenario_inputs":{"resident_count":3}}
```

Omitted display name clears it; omitted containers become empty and omitted
leaves are removed. Containers cannot be null. Explicit null on a supported leaf
means personal unknown and prevents source fallback. Empty overrides remain valid
for a later explicitly saved record.

Future PATCH merge and reset:

```json
{"schema_version":1,"display_name":null,"set":{"overrides":{"floor_area":120,"ventilation_description":null},"scenario_inputs":{"resident_count":4}},"unset":["overrides.heated_net_area"]}
```

PATCH omission preserves existing name/leaves. `display_name: null` clears the
name. Nested set merges only supplied leaves, including explicit nulls. `unset`
removes exact allowed leaves to restore **current** source fallback/default
behavior. Resetting an absent leaf is idempotent:

```json
{"schema_version":1,"unset":["scenario_inputs.resident_count","overrides.floor_area"]}
```

A conflict such as setting and unsetting `scenario_inputs.resident_count` fails
atomically with 422 entries at **both** `set.scenario_inputs.resident_count` and the
corresponding `unset` index. Unknown paths, duplicates, container paths, wildcards,
JSON pointers and read-only assignments are invalid. No source arguments or
existing personal document are mutated. Structural validation is strict at every
level. `validate_new_codes()`/the helpers additionally validate newly supplied
registry codes against the read-only catalog. Old deprecated codes remain
readable/resettable; newly setting them requires current membership.

`validation_errors(error)` and `src.api.buildings.ContractRoute` provide sanitized
field errors (`loc`, `type`, `msg`, without input/exception context). Example:

```json
{"detail":[{"loc":["body","set","overrides","floor_area"],"type":"finite_number","msg":"Expected a finite JSON number (booleans and strings are not numbers)"}]}
```

Per-field projection provenance differentiates `ryhti`, `certificate`, `model`,
`user`, `derived` and `unavailable`, with availability and nullable source times.
A null override has user origin and `user_unknown`; zero remains available where
allowed. Ventilation grouping is derived from effective inputs, with dependency
paths and `is_override` when personally changed. `effective.certificate`,
`effective.published_certificate` and `effective.model` retain their original
read-only facts. `model_input_divergence` lists personally changed reference
context; model outputs always remain `source_based_not_recalculated`.
`scenario_prefix` follows the documented heating-code mapping.
`resident_count_applicability` is residential/unsupported/unknown based on effective
purpose. No Water/annual-energy/cost/CO₂ engine or new defaults are run here.
Omission/reset of resident count permits the existing frontend heuristic; explicit
null must remain unknown. The existing factors/version are inventoried above.

If current source is missing, ambiguous or unavailable, call `project_building`
with `base=None`, the explicit reason and a last-known summary or explicit key.
It returns `base_status: "unavailable"`, `base: null`, retained overrides, only
personal effective leaves, and unavailable current-source fallbacks. Historical
address/location remains exclusively under `last_known_summary`. With no current
base and no usable personal ventilation choice/note, effective ventilation category
is null with unavailable/user-unknown provenance. Keys must agree;
an ambiguous source never reattaches to another identity. Future new saves consume
404/409 outcomes; existing records can still be read/deleted/revoked by siblings.

## Evidence and later-feature handoff

Local fixture verification covers strict values/units/codes/null/zero/reset,
read-only PostGIS resolution, lexical certificate history, source timestamps,
duplicate/ambiguous/missing identities, summary selection/query budget, malformed
and oversized source geometry/text, source isolation, coherent snapshot refresh,
public errors/OpenAPI and existing building/area APIs. Exact totals/commands and
limitations are in the feature coder report and owning Syncthing task.

Authentication, durable records, narrow writer/migrations, owner CRUD/pagination,
last-known persistence, ETags/preconditions, private caching/CORS headers and
share-token lifecycle/log redaction remain assigned to the ordered siblings.
Consume these exact resolver/validation/projection contracts. No user overrides
may flow into official tables, tiles or aggregates. Frontend integration and
real-token/deployment acceptance remain subsequent observed work.

Runtime diagnosis, setup, service lifecycle, disposable test-environment changes
and recovery are delegated to `runtime_doctor`; the retained older runtime wording
means delegation. Reuse the feature-specific doctor command/report and wait for
the synchronous run/exit receipt before retrying checks after `RETRY_READY`.


## Shared-view exposure extension (F001.4)

The editable matrix, units, codes, bounds and unsupported fields above remain
unchanged. Every supported personal display/address/detail leaf and resident_count
is intentionally visible to a capability recipient as user input. Read-only
canonical identifiers, public geometry, source/certificate/model facts and
provenance stay read-only. Shared identity omits internal address_key; saved UUID,
owner identity and share lifecycle/digest metadata never enter the public DTO.
The exact nested allowlist, filtered provenance/availability maps and API/frontend/
operator handoff are in [building-shares.md](building-shares.md). Existing owner
persistence and sharing now consume these pure contracts; the earlier future-work
paragraphs describe the original F001.1 delivery boundary.
