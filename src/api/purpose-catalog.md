# Purpose catalog v1

`GET /energy-data/building-purpose-choices` is a no-store reference endpoint.
Personal-document schema versions and catalog content versions are independent.
Canonical selected-code labels and modeled purposes 05/06 are not a choice list.

## Adopted product catalog and source evidence

The pinned public observation is `src/data/purpose-catalog-v1.snapshot.json`, from
https://koodistot.suomi.fi/codelist-api/api/v1/coderegistries/rytj/codeschemes/avoin_rakennusluokitus/
with `format=json&embedCodes=true&expand=code`. The separate codes request returned
resultCount=7 and totalResults=7, exactly matching codes `01`…`07`. Scheme and every
code report upstream DRAFT. Actual FI/SV/EN labels, retrieval time and scheme
modified time 2025-12-19T11:23:26.245Z are retained. Upstream semantic version,
validity intervals and historical completeness are unknown; no labels are invented.

The owner clarification records a separate 25-feature completed-building API
sample using this exact scheme: `01,02,03,04,05,07` were observed, **not 06**.
All seven exist in the registry. This demonstrates operational use, not national
or historical completeness, label stability or lifecycle promotion. Detailed
`public_main_purpose` is distinct and cannot replace this `main_purpose` scheme.

The owner adopted these exact seven values and their existing labels on
2026-10-30: "Just use the values". The non-secret approval reference is
`.codex-orch/state/owner-purpose-catalog-adoption-2026-10-30.md`. All seven are
product-supported/selectable. Product support and upstream status are separate
facts: DRAFT/unknown remains actual source metadata and does not disable selection.
Codes, labels, scope and status may change before a stable release; the pin can
become stale and saved values can outlive selection eligibility. These remain
catalog maintenance limits, with no outstanding adoption question.

Catalog schema version is 1. The adopted content version is
`sha256:79cbd736677205cb55d016713667c0199738b8030a76183579c954386bd0aac0`.
It supersedes pending observation
`sha256:d86ecda01fb415206d30bacb3ba3c0aca7ff72cbd477945c45530556ec9e4b0e`
by changing only product support, approval reference and selectability. Codes,
labels, source metadata and observation timestamps are preserved.

## Closed adoption and lifecycle contract

`product_support` is pending|adopted, with nullable `approval_reference` identifying
the express non-secret adoption decision. Pending requires no reference and no
selectable choices. Adopted requires a nonblank approval reference and at least
one selectable choice. A hash or flag is not approval: the manager must first
record actual express adoption of the exact content pin, then review the content
update, reference, integrity and tests. The bundled snapshot records the actual
October 30 approval and adopted choices.

Per-code `lifecycle` is active|deprecated|withdrawn|unknown; DRAFT is retained in
`upstream_status`, with lifecycle unknown absent further evidence. Product adoption
may select an upstream DRAFT/unknown choice; it must never relabel it VALID/active.
Deprecated/withdrawn choices cannot be selectable. Actual language labels may be
null. Unknown historical saved codes have no invented label and remain readable.
Provider, source URI, scheme status/version/modified time, retrieval time,
attribution, validity and pinned freshness are exposed with the choices.

The deterministic `catalog_version` is sha256 of UTF-8 canonical JSON (sorted
keys, compact separators, ensure_ascii=false, finite values) of the entire envelope
except catalog_version and retrieved_at. Adoption reference/selection are covered.
A reviewed content version update is required for changes; no live fetch,
auto-refresh, TTL, guessed retirement or scheduled update is implemented. An
intact adopted pin works during upstream outage, with freshness_status=pinned_snapshot;
its retrieval timestamp never promises live freshness. Historical coverage remains
unknown unless later authoritative evidence supplies it.

## Discovery, validation and errors

Discovery and newly supplied non-null purposes in **both** request versions use
one loader/selectable set. Exact code strings/leading zeroes are preserved.
Missing, corrupt or unadopted pin returns sanitized 503 purpose_catalog_unavailable,
never an empty successful catalog. The intact bundled pin supports both v1 and
v2 purpose input, including full v1 PUT resubmission of an existing supported code.

In an adopted intact pin, newly supplied absent/nonselectable codes return 422
unsupported_code at body.overrides.main_purpose or body.set.overrides.main_purpose.
Explicit resubmission is new input. Unrelated PATCH, purpose null/unset and
DELETE/share revoke do not require catalog availability. Old values survive
refresh, reads and unrelated writes without revision changes. Completeness
qualification for an unknown/nonselectable saved value is unavailable, not an
automatic recode/removal. Bundled-pin tests prove actual seven-choice discovery
and v1/v2 persistence/read/reset parity. Separate synthetic fixtures cover missing
labels, deprecated choices, pending adoption and corrupt pins. Approval comes
from the recorded owner decision; tests establish the resulting behavior.
