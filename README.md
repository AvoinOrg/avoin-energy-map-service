Start container

`docker compose up --build`

Run commands in container

`docker exec -it energy_app bash`

Inspect database

`docker compose exec db psql -U ${PGUSER:-postgres} -d ${PGDATABASE:-avoin_energy_map_db}`

Create alembic migration in container

`docker compose exec app alembic revision --autogenerate -m "version_message"`

Clean docker container to start a fresh one

`docker compose down --rmi all -v`
