# Avoin energy map backend

This project consists of three parts
- ETL (Extract, transform, load) - Retrieve building data from the Finnish Environment Institute [https://paikkatiedot.ymparisto.fi](paikkatiedot.ymparisto.fi), organize and store it in the project's own database
- Energy certificate ETL - Download the public energy certificate CSV from the Energy Certificate Register and join the latest certificate per building to the map table.
- Modeled energy class ETL - Estimate a 2018-scale energy class for supported buildings that do not have an official class.
- API endpoints to return the building data based on the frontend's need.


## Tech stack
- Language: Python
- Package manager: Poetry
- Framework: Fast API
- Database: PostgreSQL (17.7) with PostGIS for spatial data storage.
- Database Migrations: Alembic
- Containerization: Docker & Docker Compose

## Running in local
1. Copy the sample environment file
```
cp .env.example .env
```
2. Run the container
```
make start
```
3. Apply latest migration to local database
```
make migration_upgrade
```
4. Import codes and labels
```
make etl_load_codes
```
5. Load buildings. As there are ~4 million records in the original dataset, let's just test with a limited amount, before we figure out a way to optimize the data extraction mechanism. The limit number is the amount of records to pull to local DB.
```
make etl_load_buildings_test limit=1000
```
6. Download and load energy certificates
```
make etl_download_energy_certificates
make etl_load_energy_certificates
```
7. Merge buildings, addresses, polygons, and the latest energy-certificate details into the map table when the source tables are loaded
```
make etl_merge
```
If you need to update an existing `main` table directly, without rebuilding it from `buildings`, `addresses`, and `polygons`, run:
```
make etl_augment_main_energy_certificates
make etl_augment_main_modeled_energy_classes
```
If the sandbox GeoServer tile layers are already published, update their SQL
view definitions and clear stale MVT tiles after the database update:
```
make geoserver_update_energy_classes
```
Other commands can be found in the `makefile`.

## Energy certificates
The public CSV source is:
```
https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/csv/energiatodistukset.csv
```

The downloaded file is stored at `data/energiatodistukset.csv` by default and is ignored by git. Override the path or URL with:
```
make etl_refresh_energy_certificates ENERGY_CERTIFICATES_CSV=data/energiatodistukset.csv
```

The clean join key is:
- CSV `Perustiedot / Rakennustunnus`
- Ryhti building API / `buildings.permanent_building_identifier`

All certificate rows are stored in `energy_certificates` with `certificate_id` from CSV `Id` as the primary key. A building may have several certificates, so `etl_merge` selects the latest certificate by `signed_at DESC NULLS LAST, certificate_id DESC`. The immediately preceding row in that same order supplies `energy_certificate_previous_class`. Buildings without a matching certificate get `NULL` certificate fields. The full source row is preserved as JSONB in `energy_certificates.source_row`.

The merged table and the building polygon/point GeoServer layers expose:

| Column | Meaning |
| --- | --- |
| `property_identifier` | Finnish property identifier from the building source. |
| `energy_certificate_valid_until` | Latest certificate validity end timestamp. |
| `energy_certificate_previous_class` | Class on the immediately preceding certificate, if one exists. |
| `energy_certificate_recommendations_fi` / `_sv` | Latest certificate recommendations in Finnish and Swedish. |
| `energy_certificate_heated_net_area` | Latest certificate heated net area. |
| `energy_certificate_ventilation_type_id` | Latest certificate ventilation type code. |
| `energy_certificate_ventilation_description_fi` / `_sv` | Latest certificate ventilation descriptions in Finnish and Swedish. |

The latest certificate's current class is in `main.energy_certificate_class`. The official class-scale year in `main.energy_certificate_class_year` comes from CSV `Versio`, whose current values are 2013 and 2018. A certificate with no current class gets a `NULL` current class and class year while its other latest-certificate fields remain available.

`etl_merge` stops if loaded buildings contain duplicate `permanent_building_identifier` values, because that would make the certificate join ambiguous. `etl_augment_main_energy_certificates` performs the same safety check against an existing `main` table by ensuring a `permanent_building_identifier` does not point to multiple `building_key` values before it updates rows in place. It also copies `property_identifier` from a populated `buildings` staging table. The in-place command removes obsolete certificate columns that are not in the published list unless run with `--keep-old-columns`.

## Property identifiers

For an existing `main` table, backfill property identifiers from Syke's daily national Ryhti building CSV package without rebuilding the map or loading unrelated building fields:

```bash
make etl_load_property_identifiers
```

The command downloads `data/ryhti-open-building.csv.gz`, validates the exact UUID `building_key` and 14-digit string `property_identifier`, and COPY-loads only that projection into temporary staging. It refuses malformed populated values, conflicting duplicate keys, and unexpectedly small national snapshots. Missing, blank, and all-zero identifiers remain unavailable. Identical duplicates are collapsed. Validation completes before one transaction replaces the dedicated `ryhti_property_identifiers` snapshot, records its SHA-256 in `ryhti_property_identifier_imports`, and updates only `property_identifier` on exact-key matches in existing `buildings` and `main` rows. No geometry, storey, class, scenario, or unrelated attribute is changed; no new map/building rows are created. Missing source values do not clear existing identifiers. This ETL-owned projection is separate from the deliberately sparse apartment-storey staging table.

Use `make etl_check_property_identifiers` to validate an already downloaded snapshot without committing changes. Override its path or source with `RYHTI_BUILDINGS_SNAPSHOT_PATH` and `RYHTI_BUILDINGS_SNAPSHOT_URL`, or pass `--csv-path` / `--source-url` directly to `python3 -m src.etl.load_property_identifiers`. The command uses `PG*` connection variables; confirm they identify the intended sandbox before applying it. A subsequent certificate augmentation or full merge reapplies the dedicated projection when present. After values change, run `make geoserver_publish_property_identifiers` with `GEOSERVER_TRUNCATE_GWC=true`, then verify representative direct and cached polygon and point tiles. This property-only publisher preserves existing energy attributes, datastore credentials, cache formats, zooms, and metatile settings; it only adds a missing string projection and invalidates the two building-layer MVT caches. Preserve identifiers as strings in clients, including leading zeroes.

Source and field semantics: [Ryhti data packages and open identifiers](https://ryhti.syke.fi/palvelut/palvelut-tiedon-hyodyntajille/). The value identifies the building's registry location property; it is not a building identifier, owner identifier, ownership/tenure claim, or a spatial parcel inference.

Do not run a full merge merely to backfill this field: the current sandbox `buildings` staging may contain only the narrow apartment-storey projection, not the national building inventory. The in-place property importer preserves the existing `main` inventory. The merge hook is for a future normal full ETL with complete building/address staging.

## Modeled energy classes

The model is a fallback and never replaces an official class. It supports
`main_purpose = '05'` (detached/small house) and `main_purpose = '06'`
(apartment building). It selects a default-consumption E-value by completion-year
group and conservative heating-source mapping from
`assets/modeled_energy_class_reference.csv`, rounds that E-value upward, and
applies the 2018 class limits. Detached-house limits use `floor_area`, then
`total_area`, then `gross_floor_area`; apartment limits use
`number_of_storeys`. Unsupported or incomplete records keep a `NULL` modeled
class and receive a reason in `modeled_energy_class_reason`.

The merged table exposes:

| Column | Meaning |
| --- | --- |
| `energy_certificate_class` | Official-only class from the latest certificate. |
| `energy_certificate_class_year` | Official class-scale version from CSV `Versio`. |
| `modeled_energy_class` | Modeled fallback class, only when the official class is absent. |
| `modeled_energy_e_value` | Reference E-value used for modeled classification. |
| `modeled_energy_class_reason` | Reason no fallback could be calculated. |
| `modeled_energy_class_area_source` | Area proxy used for a modeled detached house. |
| `energy_class` | Effective class: official first, otherwise modeled. |
| `is_energy_class_modeled` | `false` for official, `true` for modeled, `NULL` when unavailable. |
| `energy_class_year` | Effective class-scale year: official `Versio` or modeled `2018`. |

`make etl_merge` calculates these fields during a full rebuild. For an existing
`main` table, refresh official values first and the model second:

```
make etl_augment_main_energy_certificates
make etl_augment_main_modeled_energy_classes
```

If the current `buildings` staging table does not contain apartment storey
counts, load only the authoritative Ryhti apartment-storey projection first:

```bash
make etl_load_apartment_storeys
make etl_augment_main_modeled_energy_classes
```

The narrow loader filters Ryhti `open_building` to apartment purpose `06` with
a non-null storey count, upserts only `building_key`, `number_of_storeys`,
purpose, and source modification time, and refuses conflicting duplicate keys.
It does not null or replace unrelated columns in an existing `buildings` row.

Shadow-validate the model against one row per building with an official A-G
certificate:

```bash
make etl_validate_modeled_energy_classes
```

The command deliberately omits the official class from model inputs, limits
accuracy metrics to official certificates on the same 2018 class scale, and
prints reproducible JSON with exact/within-one rates, a confusion matrix,
input exclusions, segment results, reference-data checksum, and large
disagreement examples. Certificates on another class scale are reported as
exclusions rather than compared as if their labels were directly equivalent.

## GeoServer building tiles
The sandbox GeoServer layers `sandbox_energiakartta:energymap_building_polygons`
and `sandbox_energiakartta:energymap_building_points` are SQL-view-backed
feature types. They keep their own virtual-table SQL and explicit attribute
lists, so adding a database column is not enough for WMTS/MVT clients.

Run this after the published `main` columns have been added or refreshed:
```
make geoserver_update_energy_classes
```

The command reads `GEOSERVER_*` values from `.env.sandbox`, checks that the datastore
startup SQL is already `SET jit = off`, adds the property identifier, certificate details,
and official, modeled, and effective energy-class properties to both GeoServer
layer schemas, reloads GeoServer, and keeps
the GeoWebCache layers MVT-only. It also sets the polygon GWC layer to serve
zooms 12-17, cache zooms 12-14, and use 1x1 metatiles. It truncates the MVT cache
when `GEOSERVER_TRUNCATE_GWC=true`. The older
`geoserver_update_energy_certificate_class` make target remains as an alias.

To start the polygon cache seed for the configured cached zooms:
```
make geoserver_seed_polygon_cache
```

## Ventilation publication

Both building SQL views publish `ventilation_category` (`mechanical`, `gravity`, `other`, `no_info`) and `energy_certificate_ventilation_type_label_fi` / `_sv`, alongside the unchanged certificate type ID and FI/SV descriptions. These three new properties are derived SQL-view attributes, **not physical `main` columns**. No new building import, table rewrite, description inference, or certificate re-selection is required. The normal energy-class GeoServer updater retains the same projection on subsequent refreshes.

The reviewed code list in `src/geoserver/ventilation.py` comes from the [public energy-certificate ventilation classification API](https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/ilmanvaihtotyyppi), verified on 2026-09-15. Codes 1, 2, 3, 7, 8 map to mechanical; 0 to gravity; 4, 5, 6 to other. Unknown populated codes and description-only records are other (unclassified); no_info means type and both descriptions are absent/blank. Labels for unknown codes remain null. Codes 4 and 5 are mixed systems, and codes 1 and 2 do not establish absence of heat recovery. The four map groups are Energy Map policy; the detailed labels are the official register's wording.

Run from the repository root with the intended sandbox environment loaded (keep credentials out of command output):

```sh
set -a
. ./.env.sandbox
set +a
python3 -m src.geoserver.publish_ventilation --verify-code-list
python3 -m src.geoserver.publish_ventilation --verify-code-list --apply --snapshot-dir data/publication-backups/ventilation-NEW-RUN-ID
```

The first command is read-only. The apply command requires a new snapshot directory, saves both original feature types and cache configurations, preflights both views, detects intervening edits, adds only the ventilation projection/attributes, reloads and checks the catalog, and clears only the two building caches. The shared cache-clear helper uses the exact-layer `masstruncate` API: the former 0–24 seed request could be accepted with HTTP 200 but abort for the polygon layer's 12–17 range. Layer, zoom, format, style, metatile, datastore, and density definitions remain unchanged. Cleared tiles regenerate on demand.

After publication, check TileJSON and decoded **direct WMS MVT plus cached WMTS PBF** for both building layers; WFS alone does not prove cached availability. Verify all four categories, official labels matching raw codes, original descriptions, and missing values. Re-running the read-only command should report both views already current. To roll back, inspect each saved feature type against intervening changes, restore only this projection and its attributes (or the full snapshot only if nothing else changed), reload, and clear those same caches again. Never overwrite unrelated later catalog changes.

## Density grid overview layers
Low and mid zoom building presence is published through precomputed grid
aggregates instead of full building polygons:

| Table / layer | Cell size | Zoom range | Rows in sandbox |
| --- | ---: | --- | ---: |
| `energymap_density_grid_z5_8` | 25,000 m | 5-8 | 3,159 |
| `energymap_density_grid_z9_12` | 2,500 m | 9-12 | 151,691 |

Build the aggregate tables from `main`:
```
make etl_build_density_grids
```

Publish and seed the GeoServer/GWC MVT layers:
```
make geoserver_publish_density_grids
```

Each cell includes `building_count`, `certified_building_count`,
`certificate_coverage`, `class_a_count` through `class_g_count`,
`class_unknown_count`, and `dominant_energy_certificate_class`. These metrics
remain official-certificate metrics; modeled coverage is not counted as
certificate coverage.

## Sync data to remote (e.g. sandbox)
Running ETL straight to remote DB will take huge amount of time due to connection latency. A workaround is running it in local, dump the local db, and run `pg_restore` it to remote
- run `make etl_load_codes` to populate codes
- run `make etl_load_test_region` to load buildings & addresses in Uusimaa for testing
- run `make etl_refresh_energy_certificates` to download and load energy certificates
- run `make etl_merge` to rebuild `main` with official and modeled classes, or run `make etl_augment_main_energy_certificates` followed by `make etl_augment_main_modeled_energy_classes` if `main` already exists
- run `make etl_build_density_grids` to refresh low and mid zoom density cells
- run `make geoserver_update_energy_classes` after updating the sandbox database so GeoServer and GWC publish the official, modeled, and effective class properties
- run `make geoserver_publish_density_grids` after refreshing density cells
- clone `.env.example` to `.env.sandbox`, and fill in the remote database info
- run `./run/db_dump.sh` to dump local DB
- run `./run/db_sandbox_deploy` to sync remote DB with the dump file

## Publish certificate consumption without rebuilding main

The nullable `main.energy_certificate_consumption` text column contains a versioned JSON record of **certificate-calculated annual purchased energy for standardized use**, not measured consumption. Both building MVT layers expose it as a String. The normal certificate augmentation and complete merge maintain the field; the existing certificate CSV loader already retains all inputs in `energy_certificates.source_row`. Never rerun a complete merge against deliberately sparse building staging just to add this field.

Use the intended sandbox `PG*` and `GEOSERVER_*` environment. The narrow backfill selects the latest certificate by `signed_at DESC NULLS LAST, certificate_id DESC`, exactly matching the existing certificate selection. It updates only this column by exact permanent building identifier, supports multiple address rows for one building key, rejects ambiguous building-key joins, clears stale consumption, and preserves geometry, reference scenarios, property identifiers, areas and classes. Its default invocation stages a temporary projection and rolls back without changing published data.

```sh
python3 -m src.etl.augment_certificate_consumption
mkdir -p data/publication-backups
python3 -m src.etl.augment_certificate_consumption --apply --backup-path data/publication-backups/consumption-NEW-RUN.jsonl.gz
python3 -m src.geoserver.publish_certificate_consumption
python3 -m src.geoserver.publish_certificate_consumption --apply --snapshot-dir data/publication-backups/consumption-geoserver-NEW-RUN
```

Choose new recovery paths for every run. The gzip records old/new values for affected building/address identities before the transaction commits. The publisher snapshots both original feature types and cache configurations, detects intervening catalog edits, adds only this String projection/attribute, reloads/readbacks the catalog, and clears only the two building caches. Verify TileJSON and decoded direct/cached MVTs afterward. Repository publication and frontend deployment are separate actions.

### Consumption payload schema 1

| Key | Meaning |
| --- | --- |
| `schema_version` | Integer `1`; reject unknown versions in consumers |
| `basis` / `unit` | Literal `standardized_calculation` / `kWh/year` |
| `building_identifier` | Exact permanent building identifier; consumers must match their selected building |
| `certificate_id`, `certificate_version` | Source certificate string ID and scale/schema year (`2013` or `2018`) |
| `signed_at`, `valid_until` | ISO source timestamps or null; date context, not a meter-reading year |
| `heated_net_area_m2` | Positive certificate heated area or null, from the same certificate as the quantities |
| `components` | Array of `{carrier, annual_kwh}`; `other` entries also have zero-based `source_slot` and nullable source `name` |
| `completeness` | `complete` or `partial`; missing/invalid standard carriers make a record partial |
| `total_kwh_year` | Sum of all carriers only when complete, otherwise null |

Standard carriers are `electricity`, `district_heat`, `district_cooling`, `fossil_fuel`, and `renewable_fuel`, read respectively from `Sahko`, `Kaukolampo`, `Kaukojaahdytys`, `Fossiilinen-polttoaine`, and `Uusiutuva-polttoaine` under `Tulokset / Kaytettavat-energiamuodot`. Up to three named `Muu` purchased-energy entries become separate `other` components. Blank optional slots are unused; a named slot without a valid quantity prevents a total. Decimal commas are parsed; real zeroes are preserved; missing, negative, boolean, malformed and non-finite values are not quantities. A record with no usable quantities is null. No missing amount is silently replaced with zero, and no older certificate supplies missing fields in the newest one.

Divide an annual quantity by this payload's positive `heated_net_area_m2` to obtain a certificate-based `kWh/m²/year` value. Do not substitute registry area, multiply by an energy weighting factor, reinterpret purchased electricity as appliance electricity, merge cooling into heating, or infer a metered-consumption value from the E-value. Keep certificate provenance distinct from the existing building-type/age reference scenarios. This contract does not add Cost, CO₂, Water or renovation estimates for previously unsupported buildings.

For recovery, reverse only the owned field after checking the current value still equals the backup's `new_payload` for the exact building/address identity; a null old value means it was previously unpublished. Do not restore unrelated main columns. For GeoServer, reverse only the added projection/attribute if another publication has happened since the snapshots, then reload and clear the two caches. Recompute the projection from retained source data when that is preferable to restoring an older certificate snapshot.

Unit/publication tests are in `tests/test_certificate_consumption.py`. `tests/test_certificate_consumption_db.py` requires `RUN_CONSUMPTION_DB_TESTS=1` and refuses to run fixtures unless `PGDATABASE=energy_consumption_test`; use a disposable PostgreSQL instance. The example building `1036581819` should expose certificate `318510`: electricity 1,196,074, district heating 2,994,881, district cooling 197,293 kWh/year, zero fossil/renewable fuels, total 4,388,248 kWh/year and heated area 22,707.2 m².


## Postal-code, municipality and region summaries

The area pipeline builds one analytical record per distinct `main.building_key`, independently assigns full statistical postal areas and municipalities, derives regions from the official 2026 classification, and stores precomputed counts/distributions in immutable releases. It leaves the existing building map tables unchanged. The new Alembic revision is `3e719a2c804f` after density grids; bundled `assets/municipality-region-2026.csv` contains 308 municipalities and 19 regions, with source/checksum evidence beside it.

[Refresh and publication instructions](https://silverbullet.h.garbachio.top/energymap/operations/refresh-area-aggregates) cover the complete command sequence, input requirements and rollback. `make etl_area_download` retrieves all 3,018 Paavo 2026 postal areas and 19 region polygons. Supply the corrected NLS 2026 **1:10,000** municipality GeoPackage separately, including its layer/column names and source version; generalized municipality outlines are unsuitable for assignment verification. National loaders/builds enforce minimum population sizes. Fixtures may lower those bounds explicitly.

The map uses stable GeoServer layers `energymap_postal_code_areas`, `energymap_municipality_areas` and `energymap_region_areas`. Latest details are `/energy-data/areas/{level}/{code}` and latest native MVTs are `/energy-data/areas/{level}/tiles/{z}/{x}/{y}.pbf`. No release discovery or UUID is required for normal map use. Optional `/energy-data/areas/metadata` reports stable descriptors and provenance with `latest: true`. Keep codes as strings to preserve zeroes. Postal details accept `municipality_code` for precomputed intersection statistics; the displayed geometry remains the full postal area. Arbitrary building filters and annual Energy/Cost/CO₂/Water quantities are outside metric version `building-area-summary-v1`.

`src.geoserver.publish_area_aggregates` verifies internal candidates through versioned API and immutable GeoServer layers before activation. `src.etl.area_releases` atomically activates a verified release or restores a previous one. After either operation, run the publisher with `--latest --api-url API_URL --apply`: it publishes the three fixed views/layers, disables persistent client caching, truncates their GeoWebCache entries and compares decoded direct/cached tiles against latest API summaries. Migration `9b812a37ef01` adds the stable active views after the area schema. Configure `ENERGY_MAP_GEOSERVER_URL`, `ENERGY_MAP_AREA_WORKSPACE` and `ENERGY_MAP_API_ORIGINS` on the serving process.

The Dokploy sandbox definition is `docker-compose.dokploy.yml`, serving `https://energy-service-test.avoin.org` on container port 8000. Build the image from this repository, set `ENERGY_MAP_IMAGE` and the sandbox `PG*` values in Dokploy, attach the domain to service `api`, and use the external `dokploy-network`. The default entrypoint runs Uvicorn; migrations require an explicit operator step or `MIGRATE_ON_START=true`. `/healthz` checks liveness and `/readyz` checks active area data. The Compose service sets PostgreSQL transactions read-only and does not mount ETL data or credentials into the image.

Install test dependencies with `poetry install --with test`. Area tests run with `python3 -m unittest tests.test_area_aggregates tests.test_area_publication -v`; the real PostGIS cases require `RUN_AREA_DB_TESTS=1` and refuse to run unless `PGDATABASE=energymap_areas_test`. Use a disposable PostGIS database, run migrations first, and never point those fixtures at the application database. They cover deduplication, conflicting records, holes/enclaves, shared edges, incomplete locations, certificate scales/validity, denominator conservation, intersection summaries, decoded MVTs, release coexistence, activation/failure/rollback and candidate-only catalog publication.
