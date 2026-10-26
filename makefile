# start container
start:
	docker compose up --build
# remove and delete all related containers to build project from scratch
destroy:
	docker compose down --rmi all -v
# create new migration file
# usage: make migration_create msg="your message"
.PHONY: migration_create
migration_create:
	docker compose exec app alembic revision --autogenerate -m "$(msg)"
# upgrade database to the latest migration
migration_upgrade:
	docker compose exec app alembic upgrade head
# revert the latest migration from the database
migration_revert:
	docker compose exec app alembic downgrade -1

# access the bash shell of the running container
shell:
	docker exec -it energy_app bash
# access the shell of the local database container
db:
	docker compose exec db psql -U postgres -d avoin_energy_map_db

ENERGY_CERTIFICATES_CSV ?= data/energiatodistukset.csv
ENERGY_CERTIFICATES_URL ?= https://www.energiatodistusrekisteri.fi/api/public/energiatodistukset/csv/energiatodistukset.csv

# ETL tasks
etl_load_codes:
	docker compose exec app python3 -m src.etl.load_codes
etl_load_buildings:
	docker compose exec app python3 -m src.etl.load_buildings
etl_load_buildings_test:
	docker compose exec app python3 -m src.etl.load_buildings --limit $(limit)
etl_load_property_identifiers:
	docker compose exec app python3 -m src.etl.load_property_identifiers --download
etl_check_property_identifiers:
	docker compose exec app python3 -m src.etl.load_property_identifiers --dry-run
geoserver_publish_property_identifiers:
	docker compose exec app python3 -m src.geoserver.publish_property_identifiers
etl_load_apartment_storeys:
	docker compose exec app python3 -m src.etl.load_apartment_storeys
etl_load_apartment_storeys_test:
	docker compose exec app python3 -m src.etl.load_apartment_storeys --limit $(limit)
etl_load_addresses:
	docker compose exec app python3 -m src.etl.load_addresses
etl_load_test_municipality:
	docker compose exec app python3 -m src.etl --test=municipality
etl_load_test_region:
	docker compose exec app python3 -m src.etl --test=region
etl_load_polygons:
	docker compose exec app python3 -m src.etl.load_polygons
etl_download_energy_certificates:
	docker compose exec app python3 -m src.etl.load_energy_certificates --download --no-load --csv-path $(ENERGY_CERTIFICATES_CSV) --source-url $(ENERGY_CERTIFICATES_URL)
etl_load_energy_certificates:
	docker compose exec app python3 -m src.etl.load_energy_certificates --csv-path $(ENERGY_CERTIFICATES_CSV)
etl_refresh_energy_certificates:
	docker compose exec app python3 -m src.etl.load_energy_certificates --download --csv-path $(ENERGY_CERTIFICATES_CSV) --source-url $(ENERGY_CERTIFICATES_URL)
etl_augment_main_energy_certificates:
	docker compose exec app python3 -m src.etl.augment_energy_certificates
etl_check_certificate_consumption:
	docker compose exec app python3 -m src.etl.augment_certificate_consumption
# Apply with an explicit new recovery path; see README for the narrow flow.
geoserver_check_certificate_consumption:
	docker compose exec app python3 -m src.geoserver.publish_certificate_consumption
etl_augment_main_modeled_energy_classes:
	docker compose exec app python3 -m src.etl.augment_modeled_energy_classes
etl_validate_modeled_energy_classes:
	docker compose exec app python3 -m src.etl.validate_modeled_energy_classes
etl_build_density_grids:
	docker compose exec app python3 -m src.etl.build_density_grids
geoserver_update_energy_classes:
	docker compose exec app sh -lc 'set -a; [ -f /app/.env.sandbox ] && . /app/.env.sandbox; set +a; python3 -m src.geoserver.update_energy_certificate_class'
geoserver_update_energy_certificate_class:
	docker compose exec app sh -lc 'set -a; [ -f /app/.env.sandbox ] && . /app/.env.sandbox; set +a; python3 -m src.geoserver.update_energy_certificate_class'
geoserver_seed_polygon_cache:
	docker compose exec app sh -lc 'set -a; [ -f /app/.env.sandbox ] && . /app/.env.sandbox; set +a; python3 -m src.geoserver.seed_polygon_cache --threads $${GEOSERVER_SEED_THREADS:-4}'
geoserver_publish_density_grids:
	docker compose exec app sh -lc 'set -a; [ -f /app/.env.sandbox ] && . /app/.env.sandbox; set +a; python3 -m src.geoserver.publish_density_grids --truncate --seed --seed-threads $${GEOSERVER_SEED_THREADS:-2}'
etl_load: # load buildings and addresses
	docker compose exec app python3 -m src.etl
etl_merge: # merge buildings, addresses, polygons, and latest energy certificates into one main table
	docker compose exec app python3 -m src.etl.merge

# Versioned area analytics. Pass operation-specific flags in AREA_ARGS.
AREA_ARGS ?=
.PHONY: etl_area_download etl_area_boundaries etl_area_geography etl_area_build area_publish area_promote area_tests
etl_area_download:
	docker compose exec app python3 -m src.etl.area_inputs download $(AREA_ARGS)
etl_area_boundaries:
	docker compose exec app python3 -m src.etl.area_inputs boundaries $(AREA_ARGS)
etl_area_geography:
	docker compose exec app python3 -m src.etl.area_inputs geography $(AREA_ARGS)
etl_area_build:
	docker compose exec app python3 -m src.etl.build_area_aggregates $(AREA_ARGS)
# Publisher defaults to a dry description; --apply verifies/records delivery.
area_publish:
	docker compose exec app python3 -m src.geoserver.publish_area_aggregates $(AREA_ARGS)
# Promotes only an explicitly named release with verified delivery.
area_promote:
	docker compose exec app python3 -m src.etl.area_releases $(AREA_ARGS)
area_tests:
	docker compose exec app python3 -m unittest tests.test_area_aggregates tests.test_area_publication -v
