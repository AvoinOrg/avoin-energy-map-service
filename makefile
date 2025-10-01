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

# ETL tasks
etl_load_codes:
	docker compose exec app python3 -m src.etl.load_codes
etl_load_buildings:
	docker compose exec app python3 -m src.etl.load_buildings
# usage: make etl_load_buildings_test limit="number of buildings to load"
.PHONY: etl_load_buildings_test
etl_load_buildings_test:
	docker compose exec app python3 -m src.etl.load_buildings --limit $(limit)