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
7. Merge buildings, addresses, polygons, and the latest energy certificate class into the map table when the source tables are loaded
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

All certificate rows are stored in `energy_certificates` with `certificate_id` from CSV `Id` as the primary key. A building may have several certificates, so `etl_merge` adds the latest signed certificate's class to `main.energy_certificate_class`. The official class-scale year in `main.energy_certificate_class_year` comes from CSV `Versio`, whose current values are 2013 and 2018. Buildings without a matching certificate, or with a certificate that has no class, get `NULL`. The full source row is preserved as JSONB in `energy_certificates.source_row`.

`etl_merge` stops if loaded buildings contain duplicate `permanent_building_identifier` values, because that would make the certificate join ambiguous. `etl_augment_main_energy_certificates` performs the same safety check against an existing `main` table by ensuring a `permanent_building_identifier` does not point to multiple `building_key` values before it updates rows in place. The in-place command also removes older multi-field `energy_certificate_*` columns from `main` unless run with `--keep-old-columns`.

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

## GeoServer energy certificate tiles
The sandbox GeoServer layers `sandbox_energiakartta:energymap_building_polygons`
and `sandbox_energiakartta:energymap_building_points` are SQL-view-backed
feature types. They keep their own virtual-table SQL and explicit attribute
lists, so adding a database column is not enough for WMTS/MVT clients.

Run this after the `main` energy class columns have been added or refreshed:
```
make geoserver_update_energy_classes
```

The command reads `GEOSERVER_*` values from `.env.sandbox`, sets the datastore
startup SQL to `SET jit = off`, adds the official, modeled, and effective energy
class properties to both GeoServer layer schemas, reloads GeoServer, and keeps
the GeoWebCache layers MVT-only. It also sets the polygon GWC layer to serve
zooms 12-17, cache zooms 12-14, and use 1x1 metatiles. It truncates the MVT cache
when `GEOSERVER_TRUNCATE_GWC=true`. The older
`geoserver_update_energy_certificate_class` make target remains as an alias.

To start the polygon cache seed for the configured cached zooms:
```
make geoserver_seed_polygon_cache
```

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
