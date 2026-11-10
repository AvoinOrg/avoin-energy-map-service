# Personal completed renovations v4 — candidate

**Complete-scope candidate; renewed independent review and manager delivery pending.**
The November 10 owner decision settles added-layer thickness. No deployed/private
endpoint is established. F008.2 must inherit the reviewed v4 and add a successor.

Focused Figma context was retrieved during planning 2026-11-10 for file
`Vjf62EF7vUP3cbtSb0D09R`, nodes `2663:51626`, `2663:52231`, `2663:52598`.
It establishes these eight toggles and top-floor thickness/year labels. API names,
sparse mechanics and validation below are engineering decisions in the reviewed
plan; the November 10 owner decision supplies the added-layer meaning and
nonnegative/zero policy. Report examples establish no editable details for other categories.

Let R = `scenario_inputs.renovations`. All inputs are optional personal reports;
there is no requirement to visit/fill this section to save or complete the existing
property/HVAC policy. No source fallback, scientific default, savings factor,
recommendation, certificate/model mutation or report effect is implied.

| Supported leaf below R | Meaning/evidence text node | Type/units | Applicability |
| --- | --- | --- | --- |
| `facade.completed` | Completed facade work / `1807:82870` | boolean/null, no unit | independent optional |
| `windows.completed` | Completed window renovation / `1807:82880` | boolean/null | independent optional |
| `plumbing.completed` | Completed plumbing renovation / `1807:82890` | boolean/null | independent optional |
| `roof.completed` | Completed roof renovation / `1807:82900` | boolean/null | independent optional, separate from insulation |
| `top_floor_insulation.completed` | Completed top-floor insulation work / `1807:82910`, `1807:83555` | boolean/null | true permits optional detail |
| `drainage.completed` | Completed drainage work / `1807:82920` | boolean/null | independent optional |
| `energy_smart_solutions.completed` | Completed energy smart installation / `1807:82930` | boolean/null | independent of HVAC equipment |
| `energy_storage.completed` | Completed storage installation / `1807:82940` | boolean/null | separate from HVAC storage readiness |
| `top_floor_insulation.execution_year` | Calendar year this work was executed / `1807:83568`, `1807:83570` | strict integer calendar year 1…9999 | requires completed=true; optional even then; new input must be <= current UTC year |
| `top_floor_insulation.added_insulation_thickness_mm` | Insulation layer added by this completed renovation / `1807:83567`, `1807:83569` labels and November 10 owner decision | finite nonnegative JSON number, mm; fractions permitted | requires completed=true, even when null; optional even then |

For every completed flag: true=reported work, false=explicit no-work answer,
null=explicit unknown, omission=unanswered. Numeric zero/one and strings reject.
An off/unvisited UI toggle must not be automatically submitted as false. Year 0,
negative/fraction/string/bool reject; null=unknown; omission=unanswered. Stored
reads/unrelated PATCH retain unchanged years. PUT revalidates submitted values.
No other category has a supported year/detail. Empty category `{}` is a draft;
null category/section is explicitly unknown and missing category is unanswered.

Thickness 0 is an explicit answer; null is unknown and omission is unanswered.
For example, adding 100 mm to existing 200 mm records 100 mm. Values use finite
binary64 transport, preserving rejection of precision-losing integer input; no
business maximum, rounding, clipping, total-insulation conversion or lexical JSON
precision guarantee. Negative values, booleans, strings, nonfinite/overflow and
integers not exactly representable as binary64 reject. The illustrated 230 mm and
2025 are examples, never defaults or bounds. Thickness alone does not require a
year and year alone does not require thickness. There are no unresolved owner
choices in this candidate. No other category supports year/thickness/details.

JSON Schema expresses structural bounds and either-detail applicability in both
request and read definitions. The request-time UTC-year limit is documented and
validated at runtime; it is not frozen to a particular year in the static schema.
Stored structurally valid years remain readable during unrelated PATCH operations.

R and every supported category/leaf have exact-path provenance. Present answers
(including false and thickness 0) are user/available/is_override; explicit null is user_unknown;
absence is unavailable/missing/no_personal_default. Null ancestors propagate
unknown with that ancestor as source/dependency. No editable provenance.

R is one atomic PATCH leaf. Omitted R preserves it; supplied R replaces all its
descendants. Submit the latest complete subtree and current strong ETag. Omitting
a category/leaf in replacement resets it. `unset:["scenario_inputs.renovations"]`
resets only this section; setting R:null records section unknown instead.
Descendant unset and duplicate/conflicting set/unset reject. Details (even null)
with false/null/absent completed reject atomically; explicitly omit details to
clear/reselect without resurrection. Whole PUT replaces the document and clears
all omitted siblings. No save timestamps, event history or completion flags added.

Use query `personal_schema_version=4`, body integer `schema_version:4`, existing
owner routes/permissions and exact quoted ETags `"ub1:<saved_id>:<revision>"`.
Initial PUT needs If-None-Match:*; update/delete needs current If-Match. Default
view remains 1. Sparse v4 PATCH explicitly promotes old rows without dropping
siblings/shares/incarnation; GET/login never promotes. Revision increments once.

| View/operation | Stored 1/2/3 | Stored 4 |
| --- | --- | --- |
| view1 owner/shared/list | existing scalar/summary read | scalar/summary read without renovations |
| view2 | existing behavior (3 conflicts409) | typed409 |
| view3 | existing behavior | typed409, reload view4 |
| view4 detail/shared | full personal read, schema_version4, actual stored document_schema_version, R absent | full personal read with supported R |
| view4 list | summary, actual stored version | summary, stored4; no inputs |
| old writes/deletes | existing protections | 409 even after R reset, under row lock |
| v4 PUT | explicit replacement/promotion | replacement |
| v4 PATCH | non-lossy sparse promotion | sparse mutation |

Existing 400 malformed/428 missing/412 stale/409 version/422 sanitized validation/
413 size/503 unavailable responses, 32 KiB request+merged bounds, exact issuer+
subject ownership and no-store remain. Explicit capability shared view4 exposes
supported personal answers but strips owner IDs/saved UUID/digest/internal keys.

Example PATCH (all other personal sections retained):
```json
{"schema_version":4,"set":{"scenario_inputs":{"renovations":{"facade":{"completed":false},"windows":null,"top_floor_insulation":{"completed":true,"execution_year":2025,"added_insulation_thickness_mm":0}}}}}
```
Example section reset:
```json
{"schema_version":4,"unset":["scenario_inputs.renovations"]}
```

Additive combined head `a714cf9e320b`, isolated `es0004_renovations_v4` admit
1–4; no rows converted, tables/grants/source changed. Locked downgrade refuses
any v4 row. Guarded upgrades/recovery are disposable-only here. Install compatible
readers before any future private activation. Old binaries cannot safely read v4;
retain compatible readers or recover to a verified NEW target; never strip data.
Real auth/private routing/CORS/proxy/schema activation, ordinary-user save/reopen,
session expiry/ownership/failure and report acceptance remain separately required.
