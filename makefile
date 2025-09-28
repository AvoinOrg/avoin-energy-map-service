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
# access the bash shell of the running container
shell:
	docker exec -it energy_app bash

# access the shell of the local database container
db:
	docker compose exec db psql -U postgres -d avoin_energy_map_db