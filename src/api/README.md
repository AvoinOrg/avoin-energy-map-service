# Running the API

1. Start the database server
```
make start
```
2. Start the api server
```
poetry run uvicorn src.api.main:app --reload
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
