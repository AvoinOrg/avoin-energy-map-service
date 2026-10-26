# Running the API

1. Start the database server
```
make start
```
2. Start the api server
```
poetry run uvicorn src.api.main:app --log-config src/api/logging.json --reload
```
3. Run simple map api to debug the api at `src/debug/map.html`
Pass `env=sandbox` parameter (`src/debug/map.html?env=sandbox`) to load data from sandbox DB instead of local

The vector tile endpoint reads from the merged `main` table. After
`make etl_load_energy_certificates` and `make etl_merge`, each feature can
include the official-only `energy_certificate_class`, modeled fallback
`modeled_energy_class`, and effective `energy_class`. Use
`is_energy_class_modeled` to distinguish official and modeled values and
`energy_class_year` for the class-scale version. Official years come from CSV
`Versio`; modeled values use the 2018 scale.

Sandbox GeoServer publishes separate SQL-view-backed tile layers. After the
sandbox database is updated, run `make geoserver_update_energy_classes` so the
published layer schemas and cached MVT tiles include the official, modeled, and
effective class properties.

For low and mid zooms, use the sandbox GeoServer density grid layers instead of
building polygons:

- `sandbox_energiakartta:energymap_density_grid_z5_8` for zooms 5-8
- `sandbox_energiakartta:energymap_density_grid_z9_12` for zooms 9-12

Both are MVT-only GWC layers with aggregate properties such as
`building_count`, `certificate_coverage`, and
`dominant_energy_certificate_class`.


## Building area summaries

The three area levels are `postal_code`, `municipality` and `region`. See the [refresh guide](https://silverbullet.h.garbachio.top/energymap/operations/refresh-area-aggregates) for inputs, migrations, candidate builds, delivery verification, activation and rollback. Set `ENERGY_MAP_API_ORIGINS` on the API process to allow the intended frontend origins.

The map can use fixed GeoServer layers `energymap_postal_code_areas`, `energymap_municipality_areas` and `energymap_region_areas` directly. They resolve the latest published summaries and omit internal release IDs. Their GeoWebCache URLs use `scheme: tms`; native latest tiles at `/energy-data/areas/{level}/tiles/{z}/{x}/{y}.pbf` use `scheme: xyz` and source layer `areas`. Both support zooms 0–14. Codes are strings and `area_id` is the stable identity.

Clicked-area details at `/energy-data/areas/{level}/{code}` use the current active summary without a release query parameter. Metadata at `/energy-data/areas/metadata` is optional reference information with `latest: true`, stable layer descriptors, provenance and assignment diagnostics. Default details, metadata and native tiles use `Cache-Control: no-cache`. A municipality-scoped postal detail adds `municipality_code=091`, counts only the intersection's buildings, and labels its geometry as `full_postal_area`. Area-details and cross-border drill-down remain supported.

For operator candidate verification and rollback, explicit `?release_id=UUID` metadata/details and `/energy-data/areas/{release_id}/{level}/tiles/{z}/{x}/{y}.pbf` remain available. These versioned requests are outside the normal frontend contract. Internal releases are validated before activation; after promotion or rollback, publish `--latest --apply` to refresh the stable GeoServer cache.

`/healthz` checks process liveness; `/readyz` returns HTTP 200 only with validated active area data, otherwise HTTP 503. The sandbox API is served at `https://energy-service-test.avoin.org` through Dokploy; `docker-compose.dokploy.yml` configures read-only database transactions and skips startup migrations.

`metrics.official_class.valid_counts_by_scale` contains valid official A–G counts separately for 2013 and 2018. `eligible_building_count` and `valid_official_class_count` expose the same denominator; `certificate_coverage` divides it by all area buildings. The accompanying `counts` diagnostic includes expiry, missing validity, unknown scale and missing/invalid classes. Each category and floor-area sum has its own known/unknown counts. Empty areas remain addressable, with zero counts and null coverage/area sums. Modeled class distributions are diagnostics with an unknown source model version; arbitrary building filters and annual quantity aggregation are outside this release contract.

## Canonical building details and reusable personal inputs

`GET /energy-data/buildings/{building_key}` returns schema version 1 public details,
source selection, certificate/model provenance and nullable source freshness. It
is anonymous and read-only. The [building contract](building-contract.md) contains
the field matrix, deterministic resolver/summary interfaces, source gaps,
errors and typed PUT/PATCH/reset examples for later owner/share features.
The [JSON Schema bundle](building-contract-v1.schema.json) includes those DTOs.
The public GET remains independent of personal storage and authentication.

## Zitadel resource-server dependency

The reusable [authentication contract](authentication.md) defines
`src.api.auth.require_principal`, the immutable issuer/subject owner identity,
Basic introspection settings, errors, deadlines and core token forwarding.
Authentication configuration is lazy: public building/area/tile reads and health
remain independent of Zitadel. No protected demonstration/login route is added.
Dedicated Energy API
application/audience provisioning and a real-token smoke test remain unverified.

## Personal saved buildings

The [owner API and operations contract](user-buildings.md) defines authenticated
PUT/GET/PATCH/DELETE and paginated summaries at `/energy-data/user/buildings`,
strong saved-state ETags, isolated durable state, last-known identity, conditional
headers, CORS/proxy usage, migration/least-privilege grants and backup/restore.
State is explicitly disabled by default and has separate `/readyz/state`
readiness. Active share links report `share_enabled: true`; create, rotate, and
revoke them through the sharing contract below.

## Revocable building sharing

[Sharing v1 and complete frontend/operations handoff](building-shares.md) extends
the owner API with conditional rotation/revocation and anonymous allowlisted live
reads. Use the documented logging configuration for every direct launch and
custom handler; deployment proxy/APM privacy checks remain operator work.
